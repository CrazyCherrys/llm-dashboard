"""Template selection, immutable scoring, migration, and genuine 24h summaries."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from database import Database, SCHEMA
from repositories import Repository
from metrics import Metrics
from workers import Scheduler
from prompt_templates import evaluate_logic, render_prompt
from checks import inspect_svg
from visual_review import review_pelican


class PromptTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(self.temp.name)
        self.repo = Repository(self.db)
        self.metrics = Metrics(self.db)
        self.repo.changed = self.metrics.invalidate
        self.mid = self.repo.save_config(dict(model='mock',display_name='Example',base_url='https://fixture.invalid/v1',api_key='test-key'))['id']

    def logic(self, **kwargs):
        values=dict(name='Custom logic',kind='candy',body='How much is 2 + 2? Answer only the number.',rule_type='exact',expected_answer='4')
        values.update(kwargs)
        return self.repo.save_template(values)['id']

    def mock(self, config, prompt, on_first=None):
        return '4',dict(status='success',finished=time.time()),'mock'

    def test_selected_template_is_frozen_at_enqueue_even_after_edit_and_reselection(self):
        tid = self.logic()
        self.repo.save_config({'schedules':{'candy':{'template_id':tid}}},self.mid)
        scheduler = Scheduler(self.db,self.metrics,model_call=self.mock)
        rid = scheduler.enqueue(self.mid,['candy'])[0]
        self.repo.save_template({'body':'Answer 5 instead.','expected_answer':'5','name':'Changed'},tid)
        other = self.logic(expected_answer='6')
        self.repo.save_config({'schedules':{'candy':{'template_id':other}}},self.mid)
        self.repo.archive_template(tid)
        task = scheduler.claim()
        self.assertIn('2 + 2',task[0]['prompt'])
        self.assertEqual(task[1]['prompt_template']['revision'],1)
        scheduler.execute(*task)
        result = self.repo.run(rid)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['template']['expected_answer'],'4')
        self.assertEqual(result['template']['name'],'Custom logic')
        new_rid = scheduler.enqueue(self.mid,['candy'])[0]
        scheduler.execute(*scheduler.claim())
        self.assertEqual(self.repo.run(new_rid)['status'],'invalid')
        self.assertEqual(self.repo.run(new_rid)['template_id'],other)

    def test_templates_cannot_cross_types_or_archive_while_in_use(self):
        templates = self.repo.templates()
        candy = next(t for t in templates if t['kind']=='candy')
        graphic = next(t for t in templates if t['kind']=='pelican')
        with self.assertRaises(ValueError):
            self.repo.save_config({'schedules':{'candy':{'template_id':graphic['id']}}},self.mid)
        with self.assertRaises(ValueError):
            self.repo.archive_template(candy['id'])
        with self.assertRaises(ValueError):
            self.repo.save_template({'kind':'pelican'},candy['id'])
        tid = self.logic()
        self.repo.archive_template(tid)
        with self.assertRaises(ValueError):
            self.repo.save_config({'schedules':{'candy':{'template_id':tid}}},self.mid)
        self.assertEqual(self.repo.models()[0]['schedules'][0]['template_id'],candy['id'])

    def test_copy_preserves_template_selection_and_default_migration_is_idempotent(self):
        tid = self.logic()
        self.repo.save_config({'schedules':{'candy':{'template_id':tid}}},self.mid)
        copy = self.repo.save_config({'copy_from':self.mid})['id']
        model = next(m for m in self.repo.models() if m['id']==copy)
        self.assertEqual(next(s for s in model['schedules'] if s['kind']=='candy')['template_id'],tid)
        Database(self.temp.name)
        self.assertEqual(len(self.repo.templates()),3)

    def test_rule_semantics_and_ungraded_are_not_success_scores(self):
        template=dict(rule_type='number',expected_answer='21')
        for text,expected in [('答案是21。','passed'),('121','invalid'),('21.5','invalid'),('-21','invalid'),('x21','invalid')]:
            self.assertEqual(evaluate_logic(text,template)[0],expected)
        self.assertEqual(evaluate_logic('  BLUE\n sky ',dict(rule_type='exact',expected_answer='blue sky'))[0],'passed')
        self.assertEqual(evaluate_logic('blue sky because...',dict(rule_type='exact',expected_answer='blue sky'))[0],'invalid')
        tid = self.logic(rule_type='none',expected_answer='')
        self.repo.save_config({'schedules':{'candy':{'template_id':tid}}},self.mid)
        scheduler = Scheduler(self.db,self.metrics,model_call=self.mock)
        rid = scheduler.enqueue(self.mid,['candy'])[0]
        scheduler.execute(*scheduler.claim())
        self.assertEqual(self.repo.run(rid)['status'],'ungraded')
        card = self.metrics.dashboard()['models'][0]
        self.assertIsNone(card['candy_rate'])
        self.assertIsNone(card['logic_24h']['rate'])
        self.assertEqual(card['logic_24h']['ungraded'],1)
        self.assertEqual(card['metrics']['success_rate'],100)

    def test_24h_summary_uses_all_finished_logic_not_last50_or_request_window(self):
        now = time.time()
        with self.db.connect() as db:
            cid = db.execute('SELECT channel_id FROM model_configs WHERE id=?',(self.mid,)).fetchone()[0]
            # 70 in-window completed answers, with only the most recent 50 on the card.
            for i in range(70):
                db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished,template_id,template_revision) VALUES(?,?,'candy',?,?,?,1,?)",
                           (self.mid,cid,'passed' if i<60 else 'invalid',now-5000+i,now-5000+i,1 if i<30 else 2))
            for kind,status,finished in [('candy','passed',now-90000),('pelican','passed',now-10),('candy','error',now-10),('candy','ungraded',now-10)]:
                db.execute('INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished) VALUES(?,?,?,?,?,?)',(self.mid,cid,kind,status,finished,finished))
        result = self.metrics.dashboard(1)
        card = result['models'][0]
        stats = result['overview_24h']
        self.assertEqual(stats['participating_models'],1)
        self.assertEqual(stats['completed'],72)
        self.assertEqual((stats['passed'],stats['wrong'],stats['failed'],stats['ungraded']),(60,10,1,1))
        self.assertEqual(stats['anomalies'],11)
        self.assertLessEqual(len([r for r in card['runs'] if r['kind']=='candy']),50)
        self.assertEqual(card['logic_24h']['graded'],70)
        self.assertEqual(card['logic_24h']['passed'],60)
        self.assertEqual(card['logic_24h']['ungraded'],1)
        self.assertEqual(card['logic_24h']['template_count'],2)
        self.assertAlmostEqual(card['logic_24h']['rate'],6000/70)
        self.repo.save_config({'public':False},self.mid)
        self.assertEqual(self.metrics.dashboard()['models'],[])
        self.assertTrue(all(value == 0 for value in self.metrics.dashboard()['overview_24h'].values()))

    def test_overview_counts_rounds_terminal_states_and_active_models(self):
        now = time.time()
        other = self.repo.save_config(dict(model='pending',display_name='Pending',base_url='https://fixture.invalid/v1',api_key='test-key'))['id']
        with self.db.connect() as db:
            cid = db.execute('SELECT channel_id FROM model_configs WHERE id=?',(self.mid,)).fetchone()[0]
            other_cid = db.execute('SELECT channel_id FROM model_configs WHERE id=?',(other,)).fetchone()[0]
            db.execute('UPDATE channels SET enabled=1')
            db.execute('UPDATE model_configs SET enabled=1')
            db.execute("UPDATE check_schedules SET enabled=(kind='candy')")
            for status in ['passed','invalid','error','ungraded','interrupted','cancelled','legacy']:
                rid = db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished) VALUES(?,?,'candy',?,?,?)",(self.mid,cid,status,now-90000,now-10)).lastrowid
                if status == 'passed':
                    for attempt_status in ['failed','success']:
                        db.execute("INSERT INTO request_attempts(run_id,channel_id,model_id,role,attempt,status,started,finished) VALUES(?,?,?,'primary',1,?,?,?)",(rid,cid,self.mid,attempt_status,now-20,now-10))
            db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created) VALUES(?,?,'candy','running',?)",(other,other_cid,now-20))
            db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished) VALUES(?,?,'candy','passed',?,?)",(self.mid,cid,now-100000,now-90000))
            db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished) VALUES(?,?,'pelican','error',?,?)",(other,other_cid,now-20,now-10))
        stats = self.metrics.dashboard()['overview_24h']
        self.assertEqual(stats,dict(participating_models=2,monitoring_models=2,completed=7,passed=1,wrong=1,failed=1,ungraded=1,interrupted=1,cancelled=1,other=1,anomalies=2))
        self.repo.save_config({'enabled':False},other)
        stats = self.metrics.dashboard(6)['overview_24h']
        self.assertEqual((stats['participating_models'],stats['monitoring_models'],stats['completed']),(2,1,7))

    def test_overview_rolling_boundaries_and_empty_state(self):
        self.assertEqual(self.metrics.dashboard()['overview_24h']['completed'],0)
        now = 1791684000
        with self.db.connect() as db:
            cid = db.execute('SELECT channel_id FROM model_configs WHERE id=?',(self.mid,)).fetchone()[0]
            for finished in [now-86400,now,now-86400-1,now+1]:
                db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished) VALUES(?,?,'candy','passed',?,?)",(self.mid,cid,finished,finished))
        self.metrics.invalidate()
        with patch('metrics.time.time',return_value=now):
            stats = self.metrics.dashboard()['overview_24h']
        self.assertEqual((stats['participating_models'],stats['completed'],stats['anomalies']),(1,2,0))

    def test_custom_graphic_criteria_and_literal_css_braces(self):
        tid = self.repo.save_template(dict(name='Static cat',kind='pelican',body='Draw a cat. CSS .cat { fill: red; } Scene {scene}',rule_type='visual',review_criteria='A visible cat with two ears.',require_animation=False))['id']
        template = next(t for t in self.repo.templates() if t['id']==tid)
        prompt = render_prompt(template,'Beach','ABCD')
        self.assertIn('.cat { fill: red; }',prompt)
        self.assertIn('Beach',prompt)
        self.assertIn('ABCD',prompt)
        svg='<svg xmlns="http://www.w3.org/2000/svg"><text>ABCD</text></svg>'
        self.assertTrue(all(inspect_svg(svg,'ABCD',require_animation=False)[1].values()))
        self.assertFalse(inspect_svg(svg,'ABCD')[1]['animation'])
        observed=[]
        def call(config,content):
            observed.append(content[0]['text'])
            return '{"checks":{"requirements":true}}',{},'mock'
        result=review_pelican(dict(prompt_template=template,protocol='responses',timeout_seconds=300,max_output_tokens=16000),svg,call,frames=[dict(time=i,png='eA==') for i in range(4)])
        self.assertEqual(result['status'],'passed')
        self.assertIn('A visible cat with two ears.',observed[0])
        self.assertNotIn('鹈鹕',observed[0])

    def test_invalid_rules_and_disabled_missing_template(self):
        for values in ({'rule_type':'regex'}, {'rule_type':'number','expected_answer':'text'}, {'body':''}, {'kind':'pelican','rule_type':'visual','review_criteria':''}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.logic(**values)
        with self.assertRaises(ValueError):
            self.repo.save_config({'schedules':{'candy':{'template_id':None,'enabled':True}}},self.mid)
        self.repo.save_config({'schedules':{'candy':{'template_id':None,'enabled':False}}},self.mid)
        with self.assertRaises(ValueError):
            Scheduler(self.db,self.metrics).enqueue(self.mid,['candy'])
        self.assertEqual(self.repo.tasks(),[])


class PromptTemplateMigrationTests(unittest.TestCase):
    def test_existing_schedules_receive_defaults_without_regrading_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            # Build a version 3 database using the actual pre-template schema.
            with sqlite3.connect(Path(directory)/'monitor.sqlite3') as db:
                db.executescript(SCHEMA)
                db.execute('ALTER TABLE model_configs ADD COLUMN category_id INTEGER REFERENCES model_categories(id)')
                db.execute('CREATE UNIQUE INDEX model_private_connection ON model_configs(channel_id)')
                db.executemany('INSERT INTO schema_migrations VALUES(?,0)',[(2,),(3,)])
                db.execute("INSERT INTO channels(id,title,base_url,created,updated) VALUES(1,'Old','https://example.invalid',0,0)")
                db.execute("INSERT INTO model_configs(id,channel_id,display_name,model,created,updated) VALUES(1,1,'Old','mock',0,0)")
                db.execute("INSERT INTO check_schedules(model_id,kind,enabled,interval_minutes,next_run) VALUES(1,'candy',1,17,99999)")
                db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished,prompt,checks) VALUES(1,1,'candy','passed',0,1,'Original prompt','{\"answer_21\":true}')")
            database=Database(directory)
            with database.connect() as db:
                schedule=db.execute('SELECT * FROM check_schedules').fetchone()
                self.assertEqual(schedule['interval_minutes'],17)
                self.assertEqual(schedule['next_run'],99999)
                self.assertIsNotNone(schedule['template_id'])
                run=db.execute('SELECT * FROM check_runs').fetchone()
                self.assertEqual(run['prompt'],'Original prompt')
                self.assertEqual(run['status'],'passed')
                self.assertIsNone(run['template_id'])
            Database(directory)
            self.assertEqual(len(Repository(database).templates()),2)


if __name__=='__main__':
    unittest.main()
