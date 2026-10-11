"""Shared-card credentials, independent checks, aggregate metrics and image ownership."""
import json
import tempfile
import time
import unittest

from database import Database
from repositories import Repository
from metrics import Metrics
from workers import Scheduler


class MultiModelCardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Database(self.temp.name)
        self.repo=Repository(self.db)
        self.metrics=Metrics(self.db)
        self.repo.changed=self.metrics.invalidate
        self.scheduler=Scheduler(self.db,self.metrics)
        self.card=self.repo.save_channel(dict(title='CodeX-Pro',base_url='https://example.invalid/v1',api_key='shared-private-key',enabled=True,category_name='My series'))['id']

    def add(self, **values):
        return self.repo.save_card_model(dict(model='same-model',display_name='Child',**values),channel_id=self.card)['id']

    def insert_run(self,mid,kind='candy',status='passed',when=None,graphic=False):
        when=when or time.time()-10
        with self.db.connect() as db:
            m=db.execute('SELECT * FROM model_configs WHERE id=?',(mid,)).fetchone()
            cid=m['channel_id']
            snapshot=dict(title='CodeX-Pro',display_name=m['display_name'],model=m['model'])
            return db.execute('''INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished,config,legacy_svg)
                VALUES(?,?,?,?,?,?,?,?)''',(mid,cid,kind,status,when,when,json.dumps(snapshot),'<svg xmlns="http://www.w3.org/2000/svg"><text>demo</text></svg>' if graphic else '')).lastrowid

    def test_five_children_share_one_card_and_start_paused(self):
        self.assertEqual(self.metrics.dashboard()['cards'],[])
        mids=[self.add(position=5-i) for i in range(5)]
        card=self.repo.channels()[0]
        self.assertEqual([m['id'] for m in card['models']],list(reversed(mids)))
        self.assertTrue(all(not m['enabled'] for m in card['models']))
        self.assertTrue(all(m['card_id']==self.card for m in card['models']))
        public=self.metrics.dashboard()
        self.assertEqual(len(public['cards']),1)
        self.assertEqual(len(public['cards'][0]['models']),5)
        self.assertEqual(public['categories'][0]['card_count'],1)
        self.assertEqual(public['categories'][0]['model_count'],5)
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM check_schedules').fetchone()[0],10)
        self.assertNotIn('shared-private-key',json.dumps(public)+json.dumps(card))

    def test_child_edit_copy_cannot_change_parent_credentials_or_switches(self):
        mid=self.add(enabled=True)
        tid=self.repo.save_template(dict(name='Four',kind='candy',body='2+2?',rule_type='exact',expected_answer='4'))['id']
        self.repo.save_card_model({'schedules':{'candy':{'template_id':tid,'interval_minutes':17}}},model_id=mid)
        self.insert_run(mid)
        clone=self.repo.save_card_model({'copy_from':mid,'display_name':'Copy'},channel_id=self.card)['id']
        models={m['id']:m for m in self.repo.channels()[0]['models']}
        self.assertFalse(models[clone]['enabled'])
        self.assertEqual(models[mid]['schedules'],[{**s,'id':o['id'],'model_id':mid,'next_run':o['next_run']} for s,o in zip(models[clone]['schedules'],models[mid]['schedules'])])
        self.assertEqual(self.repo.history(model_id=clone)['items'],[])
        for values in ({'api_key':'different'}, {'public':False}, {'category_id':None}, {'channel_id':999}):
            with self.assertRaises(ValueError):
                self.repo.save_card_model(values,model_id=mid)
        self.repo.save_card_model({'display_name':'Renamed','enabled':False},model_id=mid)
        self.assertTrue(self.repo.channels()[0]['enabled'])
        self.assertEqual(self.repo.channels()[0]['title'],'CodeX-Pro')
        with self.assertRaises(ValueError):
            self.repo.save_config({'enabled':False},mid)
        self.repo.save_channel({'api_key':'rotated-shared-key'},self.card)
        for child in (mid,clone):
            rid=self.scheduler.enqueue(child,['candy'])[0]
            task=self.scheduler.claim()
            self.assertEqual(task[0]['id'],rid)
            self.assertEqual(task[1]['api_key'],'rotated-shared-key')
            with self.db.connect() as db:
                db.execute("UPDATE check_runs SET status='passed',finished=? WHERE id=?",(time.time(),rid))

    def test_card_pause_preserves_child_choices_and_running_work(self):
        first=self.add(enabled=True)
        second=self.add(enabled=True)
        paused=self.add()
        self.scheduler.tick(time.time()+1)
        task=self.scheduler.claim()
        self.repo.save_channel({'enabled':False},self.card)
        with self.db.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT enabled FROM model_configs ORDER BY id')],[1,1,0])
            self.assertEqual(db.execute('SELECT status FROM check_runs WHERE id=?',(task[0]['id'],)).fetchone()[0],'running')
            self.assertFalse(db.execute("SELECT 1 FROM check_runs WHERE status='queued'").fetchone())
        self.repo.save_channel({'enabled':True},self.card)
        self.scheduler.tick(time.time()+1)
        self.assertEqual({t['model_id'] for t in self.repo.tasks()},{first,second})
        self.assertNotIn(paused,{t['model_id'] for t in self.repo.tasks()})

    def test_pause_one_child_leaves_sibling_queue_and_card_enabled(self):
        first=self.add(enabled=True); second=self.add(enabled=True)
        self.scheduler.tick(time.time()+1)
        self.repo.save_card_model({'enabled':False},model_id=first)
        self.assertEqual([t['model_id'] for t in self.repo.tasks()],[second])
        self.assertTrue(self.repo.channels()[0]['enabled'])

    def test_metrics_weight_requests_not_model_percentages(self):
        mids=[self.add() for _ in range(3)]
        now=time.time()-5
        for mid,status,count,input_tokens,cached,first in [(mids[0],'success',1,100,100,10),(mids[1],'failed',3,100,0,None),(mids[2],'success',1,600,300,30)]:
            rid=self.insert_run(mid,status='passed' if status=='success' else 'error')
            with self.db.connect() as db:
                for n in range(count):
                    db.execute('''INSERT INTO request_attempts(run_id,channel_id,model_id,attempt,started,finished,status,input_tokens,cached_tokens,first_token_seconds)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''',(rid,self.card,mid,n+1,now,now,status,input_tokens,cached,first))
                db.execute("INSERT INTO request_attempts(run_id,channel_id,model_id,role,attempt,started,finished,status,input_tokens,cached_tokens) VALUES(?,?,?,'review',1,?,?,'success',99999,99999)",(rid,self.card,mid,now,now))
        card=self.metrics.dashboard()['cards'][0]
        self.assertEqual(card['metrics']['attempts'],5)
        self.assertEqual(card['metrics']['success_rate'],40)
        self.assertEqual(card['metrics']['cache_rate'],40)
        self.assertEqual(card['metrics']['first_token_seconds'],20)
        self.assertEqual([m['metrics']['success_rate'] for m in card['models']],[100,0,100])

    def test_each_model_has_fifty_logic_results_and_card_fifty_mixed_images(self):
        mids=[self.add() for _ in range(3)]
        now=time.time()-1000
        for i in range(55):
            for j,mid in enumerate(mids):
                when=now+i*3+j
                self.insert_run(mid,status='passed' if j!=1 else 'invalid',when=when)
                self.insert_run(mid,kind='pelican',when=when,graphic=True)
        card=self.metrics.dashboard()['cards'][0]
        self.assertEqual([len([r for r in m['runs'] if r['kind']=='candy']) for m in card['models']],[50]*3)
        self.assertEqual([m['candy_rate'] for m in card['models']],[100,0,100])
        self.assertEqual([m['logic_24h']['graded'] for m in card['models']],[55]*3)
        self.assertEqual(len(card['images']),50)
        self.assertEqual({i['model_id'] for i in card['images']},set(mids))
        page=self.repo.history(channel_id=self.card,kind='pelican')
        older=self.repo.history(channel_id=self.card,kind='pelican',before=page['next_before'])
        self.assertEqual(len(older['items']),50)
        self.assertFalse({r['id'] for r in older['items']} & {r['id'] for r in page['items']})
        self.assertTrue(all(r['has_graphic'] and r['display_name']=='Child' for r in page['items']))

    def test_archive_child_and_hide_card_remove_all_public_projections(self):
        first=self.add(); second=self.add()
        first_run=self.insert_run(first,kind='pelican',graphic=True)
        second_run=self.insert_run(second,kind='pelican',graphic=True)
        self.repo.save_card_model({'archived':True},model_id=first)
        card=self.metrics.dashboard()['cards'][0]
        self.assertEqual([m['id'] for m in card['models']],[second])
        self.assertEqual([i['run_id'] for i in card['images']],[second_run])
        self.assertIsNone(self.repo.run(first_run))
        self.repo.save_channel({'public':False},self.card)
        self.assertEqual(self.metrics.dashboard()['cards'],[])
        self.assertEqual(self.metrics.dashboard()['models'],[])
        self.assertEqual(self.repo.history(channel_id=self.card)['items'],[])
        self.assertIsNone(self.repo.run(second_run))
        self.assertIsNotNone(self.repo.run(second_run,admin=True))

    def test_failed_card_and_child_saves_leave_no_partial_config(self):
        with self.assertRaises(ValueError):
            self.repo.save_channel({'category_name':'Should rollback','base_url':'https://different.invalid'},self.card)
        self.assertEqual([c['name'] for c in self.repo.categories()],['My series'])
        mid=self.add()
        with self.assertRaises(ValueError):
            self.repo.save_card_model({'copy_from':mid,'schedules':{'candy':{'interval_minutes':0}}},channel_id=self.card)
        self.assertEqual(len(self.repo.channels()[0]['models']),1)
        other=self.repo.save_channel(dict(title='Other',base_url='https://other.invalid',api_key='other-key'))['id']
        with self.assertRaises(ValueError):
            self.repo.save_card_model({'copy_from':mid},channel_id=other)
        self.repo.save_channel({'archived':True},self.card)
        with self.assertRaises(ValueError): self.add()

    def test_graphic_labels_keep_generation_name_after_model_rename(self):
        mid=self.add()
        rid=self.insert_run(mid,kind='pelican',graphic=True)
        self.repo.save_card_model({'display_name':'New name'},model_id=mid)
        public=self.metrics.dashboard()
        self.assertEqual(public['cards'][0]['models'][0]['name'],'New name')
        self.assertEqual(public['cards'][0]['images'][0]['display_name'],'Child')
        self.assertEqual(self.repo.history(channel_id=self.card,kind='pelican')['items'][0]['display_name'],'Child')
        self.assertEqual(self.repo.run(rid)['config']['display_name'],'Child')

    def test_schema_restart_never_regroups_or_duplicates_existing_cards(self):
        mids=[self.add() for _ in range(3)]
        Database(self.temp.name)
        with self.db.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT id,channel_id FROM model_configs ORDER BY id')],[(mid,self.card) for mid in mids])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0],5)


if __name__=='__main__': unittest.main()
