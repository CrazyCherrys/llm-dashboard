"""Unified configuration, category lifecycle and v2 data migration regressions."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from database import Database, SCHEMA
from repositories import Repository
from metrics import Metrics
from workers import Scheduler


class ModelManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(self.temp.name)
        self.repo = Repository(self.database)
        self.metrics = Metrics(self.database)
        self.repo.changed = self.metrics.invalidate

    def add(self, **values):
        return self.repo.save_config(dict(model='same-model',display_name='Primary',base_url='https://example.invalid/v1',
                                         api_key='private-first-key',**values))['id']

    def test_atomic_creation_and_failed_edit_leave_no_partial_changes(self):
        with self.assertRaises(ValueError):
            self.add(category_name='Custom', schedules={'pelican':{'interval_minutes':0}})
        with self.database.connect() as db:
            for table in ('channels','model_configs','model_categories','check_schedules'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)
        mid = self.add(category_name='Custom')
        with self.assertRaises(ValueError):
            self.repo.save_config({'display_name':'Changed','api_key':'replacement-key','category_name':'Rolled back',
                                   'schedules':{'candy':{'interval_minutes':0}}},mid)
        self.assertEqual([c['name'] for c in self.repo.categories()],['Custom'])
        self.assertEqual(self.repo.models()[0]['display_name'],'Primary')
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT api_key FROM channels').fetchone()[0],'private-first-key')

    def test_category_rename_delete_preserve_models_and_runs(self):
        self.assertEqual(self.repo.categories(),[])
        mid = self.add(category_name='My GPT')
        category = self.repo.categories()[0]['id']
        scheduler = Scheduler(self.database,self.metrics)
        rid = scheduler.enqueue(mid,['candy'])[0]
        self.repo.save_category({'name':'Personal Series','sort_order':8},category)
        with self.assertRaises(ValueError):
            self.repo.save_category({'name':' personal series '})
        self.assertEqual(self.metrics.dashboard()['categories'][0]['name'],'Personal Series')
        self.repo.delete_category(category)
        self.assertEqual(self.repo.models()[0]['category_id'],None)
        self.assertEqual(self.repo.run(rid)['model_id'],mid)
        self.assertEqual(self.metrics.dashboard()['categories'],[])
        self.assertEqual(len(self.metrics.dashboard()['models']),1)

    def test_copy_is_private_paused_and_keeps_no_history(self):
        mid = self.add(enabled=True,category_name='Custom')
        scheduler = Scheduler(self.database,self.metrics)
        rid = scheduler.enqueue(mid,['candy'])[0]
        copy = self.repo.save_config({'copy_from':mid,'display_name':'Backup'})['id']
        with self.database.connect() as db:
            rows = db.execute('SELECT m.id,m.channel_id,c.api_key FROM model_configs m JOIN channels c ON c.id=m.channel_id ORDER BY m.id').fetchall()
            self.assertNotEqual(rows[0]['channel_id'],rows[1]['channel_id'])
            self.assertEqual(rows[0]['api_key'],rows[1]['api_key'])
        self.assertFalse(self.repo.models()[1]['enabled'])
        self.assertEqual(self.repo.history(model_id=copy)['items'],[])
        self.repo.save_config({'api_key':'private-copy-key','public':False},copy)
        self.assertIsNotNone(self.repo.run(rid))
        self.assertEqual([m['id'] for m in self.metrics.dashboard()['models']],[mid])
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT api_key FROM channels WHERE id=?',(rows[0]['channel_id'],)).fetchone()[0],'private-first-key')
        rendered = json.dumps(self.repo.models())+json.dumps(self.metrics.dashboard())
        self.assertNotIn('private-first-key',rendered)
        self.assertNotIn('private-copy-key',rendered)
        with self.assertRaises(ValueError):
            self.repo.save_config({'copy_from':mid,'base_url':'https://other.invalid/v1'})

    def test_same_model_distinct_metrics_graphics_and_visibility(self):
        first = self.add(category_name='Visible')
        second = self.repo.save_config({'copy_from':first,'display_name':'Other'})['id']
        now = time.time()
        with self.database.connect() as db:
            ids = {r['id']:r['channel_id'] for r in db.execute('SELECT id,channel_id FROM model_configs')}
            for mid,status in ((first,'success'),(second,'failed')):
                for i in range(52):
                    rid = db.execute("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished,legacy_svg) VALUES(?,?,'pelican','passed',?,?,'<svg/>')",(mid,ids[mid],now+i,now+i)).lastrowid
                    db.execute('INSERT INTO request_attempts(run_id,channel_id,model_id,attempt,started,status) VALUES(?,?,?,1,?,?)',(rid,ids[mid],mid,now,status))
        cards = {m['id']:m for m in self.metrics.dashboard()['models']}
        self.assertEqual(cards[first]['metrics']['success_rate'],100)
        self.assertEqual(cards[second]['metrics']['success_rate'],0)
        for mid in (first,second):
            self.assertEqual(len(cards[mid]['images']),50)
            self.assertTrue(all(i['model_id']==mid for i in cards[mid]['images']))
        self.repo.save_config({'public':False},second)
        self.assertEqual(self.repo.history(model_id=second)['items'],[])
        self.assertEqual(self.metrics.dashboard()['categories'][0]['model_count'],1)
        self.repo.save_config({'archived':True,'enabled':False},first)
        self.assertEqual(self.metrics.dashboard()['categories'],[])
        self.assertEqual(self.metrics.dashboard()['models'],[])
        self.assertIsNotNone(self.repo.run(cards[first]['images'][0]['run_id'],admin=True))

    def test_pause_hides_no_history_and_schedule_changes_are_independent(self):
        mid = self.add(enabled=True)
        copy = self.repo.save_config({'copy_from':mid,'enabled':True})['id']
        scheduler = Scheduler(self.database,self.metrics)
        scheduler.tick(time.time()+1)
        self.repo.save_config({'enabled':False},mid)
        self.assertEqual({t['display_name'] for t in self.repo.tasks()},{'Primary'})
        with self.database.connect() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT model_id FROM check_runs WHERE status='queued'")],[copy])
        self.assertEqual(len(self.metrics.dashboard()['models']),2)

    def test_organizing_paused_model_keeps_queued_manual_checks(self):
        mid = self.add()
        scheduler = Scheduler(self.database,self.metrics)
        rid = scheduler.enqueue(mid,['pelican'])[0]
        self.repo.save_config({'display_name':'Renamed','category_name':'Moved',
                              'schedules':{'pelican':{'enabled':False,'interval_minutes':45}}},mid)
        self.assertEqual(self.repo.run(rid)['status'],'queued')
        task = scheduler.claim()
        self.assertEqual(task[0]['id'],rid)

    def test_copy_rejects_nontext_credentials_without_writing(self):
        mid = self.add()
        for field in ('api_key','base_url'):
            for value in (None,False,[],{}):
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    self.repo.save_config({'copy_from':mid,field:value})
        self.assertEqual(len(self.repo.models()),1)


class ModelMigrationTests(unittest.TestCase):
    def test_v2_shared_connection_split_is_idempotent_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'monitor.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA)
                db.execute('INSERT INTO schema_migrations VALUES(2,?)',(time.time(),))
                for cid,enabled,public,archived in ((1,1,1,0),(2,0,0,0),(3,1,1,1)):
                    db.execute('INSERT INTO channels(id,title,base_url,api_key,enabled,public,archived,sort_order,created,updated) VALUES(?,?,?,?,?,?,?,?,0,0)',
                               (cid,f'Route {cid}','https://example.invalid/v1',f'secret-{cid}',enabled,public,archived,cid))
                for mid,cid in ((11,1),(12,1),(13,2),(14,3)):
                    db.execute('INSERT INTO model_configs(id,channel_id,display_name,model,created,updated) VALUES(?,?,?,\'same-model\',0,0)',(mid,cid,f'Model {mid}'))
                    for kind in ('candy','pelican'):
                        db.execute('INSERT INTO check_schedules(model_id,kind,enabled,interval_minutes,next_run) VALUES(?,?,1,17,12345)',(mid,kind))
                    db.execute("INSERT INTO check_runs(id,model_id,channel_id,kind,status,created,config,output) VALUES(?,?,?,'candy','passed',0,?,'21')",(mid,mid,cid,json.dumps({'title':f'Route {cid}','display_name':f'Model {mid}'})))
                    db.execute("INSERT INTO request_attempts(run_id,channel_id,model_id,attempt,started,status) VALUES(?,?,?,1,0,'success')",(mid,cid,mid))
                    db.execute("INSERT INTO artifacts(run_id,svg_path,generated) VALUES(?,'preserved.svg',0)",(mid,))
            database = Database(directory)
            repo = Repository(database)
            self.assertEqual(repo.categories(),[])
            with database.connect() as db:
                models = db.execute('SELECT * FROM model_configs ORDER BY id').fetchall()
                self.assertEqual([m['id'] for m in models],[11,12,13,14])
                self.assertEqual(len({m['channel_id'] for m in models}),4)
                self.assertEqual([m['display_name'] for m in models[:2]],['Route 1 · Model 11','Route 1 · Model 12'])
                self.assertEqual(db.execute('SELECT DISTINCT next_run FROM check_schedules').fetchall()[0][0],12345)
                for model in models:
                    self.assertEqual(db.execute('SELECT channel_id FROM check_runs WHERE model_id=?',(model['id'],)).fetchone()[0],model['channel_id'])
                    self.assertEqual(db.execute('SELECT channel_id FROM request_attempts WHERE model_id=?',(model['id'],)).fetchone()[0],model['channel_id'])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],4)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual([m['id'] for m in Metrics(database).dashboard()['models']],[11,12])
            self.assertFalse(next(m for m in repo.models() if m['id']==13)['enabled'])
            self.assertIsNone(repo.run(13))
            self.assertEqual(repo.run(12)['config']['display_name'],'Model 12')
            Database(directory)
            with database.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0],4)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs').fetchone()[0],4)
            repo.save_config({'api_key':'new-model-key'},12)
            with database.connect() as db:
                self.assertEqual(db.execute('SELECT api_key FROM channels WHERE id=1').fetchone()[0],'secret-1')


if __name__ == '__main__':
    unittest.main()
