import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from database import Database
from checks import inspect_svg
from metrics import Metrics, aggregate
from repositories import Repository
from server import Application, Auth, Handler, Server
from storage import Storage
from upstream import UpstreamError, call_model
from workers import Scheduler


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    mode = 'responses'

    def log_message(self, *_):
        pass

    def do_POST(self):
        json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        mode = self.server.mode
        if mode == 'unauthorized':
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"secret"}'); return
        if mode == 'json':
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'21'}}],
                                        'usage':{'prompt_tokens':10,'completion_tokens':1},'model':'mock'}).encode()); return
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
        if mode in ('sse-framing', 'responses-keepalive', 'chat-keepalive', 'bad-usage'):
            completed = dict(type='response.completed',response={'status':'completed','model':'mock',
                             'output':[{'type':'message','content':[{'type':'output_text','text':'鹈鹕21'}]}],
                             'usage':{'input_tokens':100,'output_tokens':3,'input_tokens_details':{'cached_tokens':80}}})
            if mode == 'sse-framing':
                # SSE permits CR, CRLF, comments, BOM, and several data lines per event.
                raw = ('\ufeff: heartbeat\r\r'
                       'event: response.output_text.delta\r'
                       'data: {"type":"response.output_text.delta",\r'
                       'data: "delta":"鹈鹕21"}\r\r'
                       'data: '+json.dumps(completed,ensure_ascii=False)+'\r\r').encode()
                for part in (raw[:1],raw[1:2],raw[2:7],raw[7:95],raw[95:96],raw[96:]):
                    self.wfile.write(part); self.wfile.flush(); time.sleep(.005)
                return
            if mode == 'bad-usage':
                completed['response']['usage']['input_tokens_details'] = 'unsupported-detail-shape'
            if mode == 'chat-keepalive':
                events = [{'model':'mock','choices':[{'delta':{'content':'鹈鹕21'},'finish_reason':None}]},
                          {'choices':[{'delta':{},'finish_reason':'stop'}]},
                          {'choices':[],'usage':{'prompt_tokens':100,'completion_tokens':3,'prompt_tokens_details':{'cached_tokens':80}}}]
            else:
                events = [{'type':'response.output_text.delta','delta':'鹈鹕21'},completed]
            for event in events:
                self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode()); self.wfile.flush()
            if mode.endswith('keepalive'):
                time.sleep(.6)
            return
        if mode == 'chat':
            events = [dict(choices=[dict(index=0,delta={'role':'assistant'},finish_reason=None)]),
                      dict(model='mock',choices=[dict(index=0,delta={'content':'21'},finish_reason=None)]),
                      dict(choices=[dict(index=0,delta={},finish_reason='stop')]),
                      dict(choices=[],usage={'prompt_tokens':100,'completion_tokens':1,'prompt_tokens_details':{'cached_tokens':80}})]
        else:
            events = [dict(type='response.created',response={'status':'in_progress'}),
                      dict(type='response.output_text.delta',delta='21'),
                      dict(type='response.completed',response={'status':'completed','model':'mock','output':[{'type':'message','content':[{'type':'output_text','text':'21'}]}],
                                                               'usage':{'input_tokens':100,'output_tokens':1,'input_tokens_details':{'cached_tokens':80}}})]
        if mode=='incomplete':
            events = events[:-1]
        for index,event in enumerate(events):
            raw = ('data: '+json.dumps(event)+'\r\n\r\n').encode()
            # Split event framing across network reads; delay final completion.
            if index==1:
                self.wfile.write(raw[:9]); self.wfile.flush(); time.sleep(.03); self.wfile.write(raw[9:]); self.wfile.flush()
                time.sleep(.15)
            else:
                self.wfile.write(raw); self.wfile.flush()


class UpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1',0),UpstreamHandler)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def config(self,protocol):
        return dict(base_url=f'http://127.0.0.1:{self.server.server_port}/v1',api_key='never-publish',model='mock',protocol=protocol,
                    effort='medium',timeout_seconds=5,first_token_timeout=2,idle_timeout=2,max_output_tokens=1024)

    def test_both_streams_measure_content_before_completion(self):
        for protocol in ('responses','chat'):
            self.server.mode=protocol
            seen=[]
            output,metrics,model=call_model(self.config(protocol), 'question',on_first=seen.append)
            self.assertEqual(output,'21'); self.assertEqual(model,'mock')
            self.assertEqual(metrics['cached_tokens'],80)
            self.assertLess(metrics['first_token_seconds'],metrics['duration_seconds']-.1)
            self.assertEqual(len(seen),1)

    def test_nonstream_does_not_fabricate_first_token_or_cache(self):
        self.server.mode='json'
        output,metrics,_=call_model(self.config('chat'),'question')
        self.assertEqual(output,'21'); self.assertIsNone(metrics['first_token_seconds'])
        self.assertNotIn('cached_tokens',metrics)

    def test_auth_failure_is_not_retryable_or_body_leaking(self):
        self.server.mode='unauthorized'
        with self.assertRaises(UpstreamError) as caught:
            call_model(self.config('responses'),'question')
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(caught.exception.metrics['http_status'],401)

    def test_incomplete_stream_is_not_success(self):
        self.server.mode='incomplete'
        with self.assertRaises(UpstreamError) as caught:
            call_model(self.config('responses'),'question')
        self.assertEqual(caught.exception.code,'incomplete_stream')
        self.assertEqual(caught.exception.metrics['status'],'failed')

    def test_sse_framing_and_utf8_are_independent_of_network_chunks(self):
        self.server.mode='sse-framing'
        output,metrics,_=call_model(self.config('responses'),'question')
        self.assertEqual(output,'鹈鹕21')
        self.assertEqual(metrics['cached_tokens'],80)
        self.assertIsNotNone(metrics['first_token_seconds'])

    def test_completed_stream_does_not_wait_for_upstream_disconnect(self):
        for protocol in ('responses','chat'):
            self.server.mode=protocol+'-keepalive'
            began=time.monotonic()
            output,metrics,_=call_model(self.config(protocol),'question')
            self.assertLess(time.monotonic()-began,.5)
            self.assertEqual(output,'鹈鹕21')
            self.assertEqual(metrics['cached_tokens'],80)

    def test_unknown_usage_detail_does_not_discard_valid_answer(self):
        self.server.mode='bad-usage'
        output,metrics,_=call_model(self.config('responses'),'question')
        self.assertEqual(output,'鹈鹕21')
        self.assertEqual(metrics['input_tokens'],100)
        self.assertNotIn('cached_tokens',metrics)

    def test_wrapped_connection_timeout_is_recorded_as_timeout(self):
        from urllib import error
        with patch('upstream.request.build_opener') as opener:
            opener.return_value.open.side_effect=error.URLError(TimeoutError('internal timeout detail'))
            with self.assertRaises(UpstreamError) as caught:
                call_model(self.config('responses'),'question')
        self.assertEqual(caught.exception.code,'timeout')
        self.assertEqual(caught.exception.metrics['status'],'timeout')
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn('internal timeout detail',str(caught.exception))

    def controlled_request(self, config, header_delay, body_delay=0, preparation_delay=0):
        """Advance a virtual clock at I/O boundaries, without sleeping or calling API."""
        from urllib import request
        original_request=request.Request
        clock=[100.0]
        if config['protocol']=='responses':
            events=[{'type':'response.output_text.delta','delta':'21'},
                    {'type':'response.completed','response':{'status':'completed','model':'mock',
                     'output':[{'type':'message','content':[{'type':'output_text','text':'21'}]}]}}]
        else:
            events=[{'model':'mock','choices':[{'delta':{'content':'21'},'finish_reason':None}]},
                    {'choices':[{'delta':{},'finish_reason':'stop'}]},
                    {'choices':[],'usage':{'prompt_tokens':100,'completion_tokens':1}}]
        body=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
        class Response:
            status=200
            headers={'Content-Type':'text/event-stream'}
            def __init__(self):
                self.closed=False
                self.timeout=None
                self.fp=SimpleNamespace(raw=SimpleNamespace(_sock=self))
            def __enter__(self):return self
            def __exit__(self,*_):self.closed=True
            def isclosed(self):return self.closed
            def settimeout(self,timeout):self.timeout=timeout
            def read1(self,_):
                if body_delay>self.timeout:
                    clock[0]+=self.timeout
                    raise TimeoutError('simulated body wait exceeded remaining budget')
                clock[0]+=body_delay
                return body
        response=Response()
        def open_request(req,timeout):
            if header_delay>timeout:
                clock[0]+=timeout
                raise TimeoutError('simulated header wait exceeded configured budget')
            clock[0]+=header_delay
            return response
        def prepare_request(*args,**kwargs):
            clock[0]+=preparation_delay
            return original_request(*args,**kwargs)
        opener=SimpleNamespace(open=open_request)
        with patch('upstream.request.build_opener',return_value=opener),\
             patch('upstream.request.Request',side_effect=prepare_request),\
             patch('upstream.time.monotonic',side_effect=lambda:clock[0]):
            return call_model(config,'question')

    def test_slow_headers_obey_configured_first_return_budget(self):
        for protocol in ('responses','chat'):
            with self.subTest(protocol=protocol):
                config=dict(self.config(protocol),timeout_seconds=300,first_token_timeout=120,idle_timeout=60)
                output,metrics,_=self.controlled_request(config,header_delay=24,body_delay=2)
                self.assertEqual(output,'21')
                self.assertEqual(metrics['status'],'success')
                self.assertAlmostEqual(metrics['first_token_seconds'],26)
                self.assertAlmostEqual(metrics['duration_seconds'],26)

    def test_headers_are_limited_by_first_return_and_remaining_total_budget(self):
        cases=[dict(total=300,first=20,header=24,body=0,prepare=0,elapsed=20),
               dict(total=9,first=120,header=10,body=0,prepare=0,elapsed=9),
               dict(total=50,first=8,header=6,body=0,prepare=3,elapsed=8)]
        for protocol in ('responses','chat'):
            for case in cases:
                with self.subTest(protocol=protocol,case=case):
                    config=dict(self.config(protocol),timeout_seconds=case['total'],first_token_timeout=case['first'],idle_timeout=60)
                    with self.assertRaises(UpstreamError) as caught:
                        self.controlled_request(config,case['header'],case['body'],case['prepare'])
                    self.assertEqual(caught.exception.code,'timeout')
                    self.assertEqual(caught.exception.metrics['status'],'timeout')
                    self.assertIsNone(caught.exception.metrics['http_status'])
                    self.assertIsNone(caught.exception.metrics['first_token_seconds'])
                    self.assertAlmostEqual(caught.exception.metrics['duration_seconds'],case['elapsed'])

    def test_header_wait_consumes_first_return_and_total_body_budgets(self):
        cases=[dict(total=50,first=12,header=10,body=3,elapsed=12),
               dict(total=12,first=120,header=10,body=3,elapsed=12),
               dict(total=15,first=120,header=10,body=3,elapsed=12,idle=2)]
        for protocol in ('responses','chat'):
            for case in cases:
                with self.subTest(protocol=protocol,case=case):
                    config=dict(self.config(protocol),timeout_seconds=case['total'],first_token_timeout=case['first'],idle_timeout=case.get('idle',60))
                    with self.assertRaises(UpstreamError) as caught:
                        self.controlled_request(config,case['header'],case['body'])
                    self.assertEqual(caught.exception.code,'timeout')
                    self.assertEqual(caught.exception.metrics['http_status'],200)
                    self.assertIsNone(caught.exception.metrics['first_token_seconds'])
                    self.assertAlmostEqual(caught.exception.metrics['duration_seconds'],case['elapsed'])


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.database=Database(self.temp.name)
        self.repository=Repository(self.database)
        self.metrics=Metrics(self.database)
        self.repository.changed=self.metrics.invalidate
        self.cid=self.repository.save_channel(dict(title='Main',base_url='http://127.0.0.1:9999/v1',api_key='private-key-123',enabled=True))['id']
        self.mid=self.repository.save_model(dict(model='mock',schedules={'candy':{'enabled':True,'interval_minutes':10},'pelican':{'enabled':True,'interval_minutes':45}}),channel_id=self.cid)['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_schedules_and_no_duplicate_tasks(self):
        scheduler=Scheduler(self.database,self.metrics)
        now=time.time()+1
        scheduler.tick(now); scheduler.tick(now+2)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM check_runs WHERE status='queued'").fetchone()[0],2)
            schedules={r['kind']:r for r in db.execute('SELECT * FROM check_schedules')}
        self.assertAlmostEqual(schedules['pelican']['next_run']-now,45*60,delta=3)
        self.assertAlmostEqual(schedules['candy']['next_run']-now,10*60,delta=3)
        self.repository.save_model({'enabled':False},model_id=self.mid)
        self.assertFalse(scheduler.claim())
        self.assertEqual(self.repository.tasks(),[])

    def test_running_checks_are_not_cancelled_when_paused(self):
        scheduler=Scheduler(self.database,self.metrics)
        scheduler.enqueue(self.mid,['candy'])
        run,config=scheduler.claim()
        self.repository.save_channel({'enabled':False},self.cid)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM check_runs WHERE id=?',(run['id'],)).fetchone()[0],'running')
        self.assertEqual(config['api_key'],'private-key-123')

    def test_partial_edits_preserve_key_and_validate_fields(self):
        self.repository.save_channel({'title':'Renamed'},self.cid)
        self.repository.save_model({'enabled':False},model_id=self.mid)
        self.repository.save_model({'enabled':True},model_id=self.mid)
        with self.assertRaises(ValueError):
            self.repository.save_channel({'base_url':'https://other.example/v1'},self.cid)
        with self.assertRaises(ValueError):
            self.repository.save_model({'retry_count':True},model_id=self.mid)
        self.assertTrue(self.repository.channels()[0]['has_key'])

    def test_retry_failures_count_and_wrong_answer_is_channel_success(self):
        calls=[]
        def mock(config,prompt,on_first=None):
            calls.append(1)
            metrics=dict(started=time.time(),finished=time.time(),duration_seconds=.05,first_token_seconds=.02,
                         input_tokens=100,cached_tokens=0,output_tokens=10,status='success',http_status=200)
            if len(calls)==1:
                metrics.update(status='failed',http_status=503,first_token_seconds=None)
                metrics.pop('input_tokens'); metrics.pop('cached_tokens')
                raise UpstreamError('temporary','http_503',True,metrics)
            return '22',metrics,'mock'
        scheduler=Scheduler(self.database,self.metrics,model_call=mock)
        rid=scheduler.enqueue(self.mid,['candy'])[0]
        scheduler.execute(*scheduler.claim())
        detail=self.repository.run(rid)
        self.assertEqual(detail['status'],'invalid')
        self.assertEqual(len(detail['attempts']),2)
        card=self.metrics.dashboard()['models'][0]
        self.assertEqual(card['metrics']['success_rate'],50)
        self.assertEqual(card['metrics']['cache_rate'],0)
        self.assertEqual(card['metrics']['cache_coverage'],50)
        self.assertEqual(card['candy_rate'],0)
        self.assertNotIn('private-key-123',json.dumps(card))

    def test_hidden_channel_cannot_be_read_by_guessed_run_id(self):
        scheduler=Scheduler(self.database,self.metrics)
        rid=scheduler.enqueue(self.mid,['candy'])[0]
        self.repository.save_channel({'public':False},self.cid)
        self.assertIsNone(self.repository.run(rid))
        self.assertEqual(self.repository.history(channel_id=self.cid)['items'],[])
        self.assertEqual(self.metrics.dashboard()['models'],[])
        self.assertIsNotNone(self.repository.run(rid,admin=True))

    def test_recovery_does_not_requeue_billed_attempt(self):
        scheduler=Scheduler(self.database,self.metrics)
        rid=scheduler.enqueue(self.mid,['candy'])[0]
        scheduler.claim()
        with self.database.connect() as db:
            db.execute('INSERT INTO request_attempts(run_id,channel_id,model_id,attempt,started) VALUES(?,?,?,?,?)',
                       (rid,self.cid,self.mid,1,time.time()))
        self.database.recover()
        self.assertEqual(self.repository.run(rid)['status'],'interrupted')
        self.assertFalse(scheduler.claim())
        card=self.metrics.dashboard()['models'][0]
        self.assertEqual(card['metrics']['attempts'],1)
        self.assertEqual(card['metrics']['success_rate'],0)
        self.assertIsNone(card['candy_rate'])

    def test_artifact_retention_does_not_delete_running_task(self):
        scheduler=Scheduler(self.database,self.metrics)
        rid=scheduler.enqueue(self.mid,['pelican'])[0]
        scheduler.claim()
        aid,_=scheduler.storage.save_svg(rid,'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        scheduler.storage.cleanup(time.time()+40*86400)
        with self.database.connect() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM artifacts WHERE id=?',(aid,)).fetchone())

    def test_missing_cache_and_first_token_are_null(self):
        values=aggregate([dict(status='success',input_tokens=100,cached_tokens=None,first_token_seconds=None)])
        self.assertEqual(values['success_rate'],100)
        self.assertIsNone(values['cache_rate']); self.assertIsNone(values['first_token_seconds'])

    def test_multiple_channels_and_models_keep_their_own_due_times(self):
        other_cid=self.repository.save_channel(dict(title='Private backup',base_url='https://backup.example/v1',
                         api_key='backup-key',enabled=True,public=False))['id']
        other_mid=self.repository.save_model(dict(model='backup',schedules={'candy':{'enabled':True,'interval_minutes':20}}),
                                            channel_id=other_cid)['id']
        sibling=self.repository.save_model(dict(model='sibling',schedules={'candy':{'enabled':True,'interval_minutes':5}}),
                                           channel_id=self.cid)['id']
        scheduler=Scheduler(self.database,self.metrics)
        now=time.time()+1
        scheduler.tick(now)
        # Pause just one model; the sibling and hidden channel still get scheduled.
        self.repository.save_model({'enabled':False},model_id=self.mid)
        claimed=[]
        while task:=scheduler.claim():
            run,_=task; claimed.append(run['model_id'])
        self.assertCountEqual(claimed,[other_mid,sibling])
        with self.database.connect() as db:
            rows={r['model_id']:r for r in db.execute("SELECT * FROM check_schedules WHERE kind='candy'")}
        self.assertAlmostEqual(rows[other_mid]['next_run']-now,1200,delta=1)
        self.assertAlmostEqual(rows[sibling]['next_run']-now,300,delta=1)
        self.assertNotIn(other_cid,[c['id'] for c in self.metrics.dashboard()['models']])

    def test_concurrent_due_ticks_create_only_one_billable_task(self):
        calls=[]
        def mock(config,prompt,on_first=None):
            calls.append(config['model'])
            return '21',dict(status='success',finished=time.time(),http_status=200,duration_seconds=.01),config['model']
        scheduler=Scheduler(self.database,self.metrics,model_call=mock)
        now=time.time()+1
        failures=[]
        barrier=threading.Barrier(6)
        def tick():
            try:
                barrier.wait();scheduler.enqueue(self.mid,['candy'],source='scheduled',now=now)
            except Exception as exc:
                failures.append(exc)
        threads=[threading.Thread(target=tick) for _ in range(6)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(failures,[])
        while task:=scheduler.claim():scheduler.execute(*task)
        self.assertEqual(calls,['mock'])
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM request_attempts').fetchone()[0],1)

    def test_queue_capacity_keeps_unqueued_schedule_due(self):
        scheduler=Scheduler(self.database,self.metrics)
        scheduler.queue_limit=1
        now=time.time()+1
        scheduler.tick(now)
        with self.database.connect() as db:
            pending=db.execute("SELECT * FROM check_runs WHERE status='queued'").fetchall()
            self.assertEqual(len(pending),1)
            waiting=db.execute('SELECT next_run FROM check_schedules WHERE model_id=? AND kind!=?',
                               (self.mid,pending[0]['kind'])).fetchone()[0]
        self.assertLessEqual(waiting,now)

    def test_configuration_edits_preserve_running_snapshot(self):
        scheduler=Scheduler(self.database,self.metrics)
        rid=scheduler.enqueue(self.mid,['candy'])[0]
        _,config=scheduler.claim()
        self.repository.save_model({'model':'new-model','effort':'high'},model_id=self.mid)
        detail=self.repository.run(rid)
        self.assertEqual(config['model'],'mock')
        self.assertEqual(detail['config']['model'],'mock')
        self.assertEqual(detail['config']['effort'],'medium')

    def test_invalid_credentials_do_not_claim_queued_request(self):
        scheduler=Scheduler(self.database,self.metrics)
        rid=scheduler.enqueue(self.mid,['candy'])[0]
        # Simulate damaged credentials left by an older database or external edit.
        with self.database.connect() as db:
            db.execute("UPDATE channels SET api_key='' WHERE id=?",(self.cid,))
        self.assertIsNone(scheduler.claim())
        self.assertNotIn(self.repository.run(rid)['status'],('running','queued'))
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM request_attempts').fetchone()[0],0)

    def test_retention_preserves_recent_metrics_and_running_work(self):
        scheduler=Scheduler(self.database,self.metrics)
        now=time.time()
        ids=[]
        for age in (10,40,100):
            rid=scheduler.enqueue(self.mid,['pelican'])[0]
            scheduler.claim()
            aid,_=scheduler.storage.save_svg(rid,'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
            with self.database.connect() as db:
                finished=now-age*86400
                db.execute("UPDATE check_runs SET status='passed',finished=?,output='large output' WHERE id=?",(finished,rid))
                db.execute('UPDATE artifacts SET generated=? WHERE id=?',(finished,aid))
                db.execute("INSERT INTO request_attempts(run_id,channel_id,model_id,attempt,started,status) VALUES(?,?,?,?,?,'success')",
                           (rid,self.cid,self.mid,1,finished))
            ids.append(rid)
        running=scheduler.enqueue(self.mid,['pelican'])[0]
        scheduler.claim()
        running_artifact,_=scheduler.storage.save_svg(running,'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with self.database.connect() as db:
            db.execute("UPDATE check_runs SET created=?,finished=?,output='still needed' WHERE id=?",(now-100*86400,now-100*86400,running))
            db.execute('UPDATE artifacts SET generated=? WHERE id=?',(now-100*86400,running_artifact))
        with patch.dict('os.environ',RUN_RETENTION_DAYS='90',ARTIFACT_RETENTION_DAYS='30',OUTPUT_RETENTION_DAYS='30'):
            scheduler.storage.cleanup(now)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT output FROM check_runs WHERE id=?',(ids[0],)).fetchone()[0],'large output')
            self.assertIsNotNone(db.execute('SELECT 1 FROM artifacts WHERE run_id=?',(ids[0],)).fetchone())
            self.assertEqual(db.execute('SELECT output FROM check_runs WHERE id=?',(ids[1],)).fetchone()[0],'')
            self.assertIsNone(db.execute('SELECT 1 FROM artifacts WHERE run_id=?',(ids[1],)).fetchone())
            self.assertIsNotNone(db.execute('SELECT 1 FROM request_attempts WHERE run_id=?',(ids[1],)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM check_runs WHERE id=?',(ids[2],)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM request_attempts WHERE run_id=?',(ids[2],)).fetchone())
            self.assertEqual(db.execute('SELECT output FROM check_runs WHERE id=?',(running,)).fetchone()[0],'still needed')
            artifact=db.execute('SELECT svg_path FROM artifacts WHERE id=?',(running_artifact,)).fetchone()
            self.assertIsNotNone(scheduler.storage.path(artifact[0]))

    def test_review_requests_are_not_channel_primary_metrics(self):
        scheduler=Scheduler(self.database,self.metrics)
        rid=scheduler.enqueue(self.mid,['pelican'])[0]
        now=time.time()
        with self.database.connect() as db:
            for role,status in (('primary','success'),('review','failed')):
                db.execute('''INSERT INTO request_attempts(run_id,channel_id,model_id,role,attempt,started,finished,status,
                           first_token_seconds,input_tokens,cached_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                           (rid,self.cid,self.mid,role,1,now,now,status,2,100,80))
        stats=self.metrics.dashboard()['models'][0]['metrics']
        self.assertEqual(stats['attempts'],1)
        self.assertEqual(stats['success_rate'],100)
        self.assertEqual(stats['cache_rate'],80)

    def test_graphic_generation_render_and_review_share_one_budget(self):
        self.repository.save_model({'timeout_seconds':10,'retry_count':0},model_id=self.mid)
        for render_elapsed,expected_status,expected_requests in ((3,'passed',2),(5,'error',1)):
            with self.subTest(render_elapsed=render_elapsed):
                clock=[0.0];observed=[]
                scheduler=Scheduler(self.database,self.metrics)
                rid=scheduler.enqueue(self.mid,['pelican'])[0]
                run,config=scheduler.claim()
                svg=f'<svg xmlns="http://www.w3.org/2000/svg"><text>{run["nonce"]}</text><circle><animate attributeName="r" values="1;2" dur="1s" repeatCount="indefinite"/></circle></svg>'
                def model_call(cfg,prompt,on_first=None):
                    observed.append(cfg['timeout_seconds'])
                    clock[0]+=6 if isinstance(prompt,str) else .5
                    text=svg if isinstance(prompt,str) else json.dumps({'checks':{'requirements':True}})
                    return text,dict(status='success',finished=time.time()),'mock'
                def render(content,timeout=30):
                    self.assertAlmostEqual(timeout,4)
                    clock[0]+=render_elapsed
                    return [dict(time=i,png='eA==') for i in range(4)]
                scheduler.model_call=model_call
                with patch('workers.time.monotonic',side_effect=lambda:clock[0]),patch('workers.render_frames',side_effect=render):
                    scheduler.execute(run,config)
                self.assertEqual(len(observed),expected_requests)
                self.assertAlmostEqual(observed[0],10)
                if expected_requests==2:self.assertAlmostEqual(observed[1],1)
                self.assertEqual(self.repository.run(rid)['status'],expected_status)

    def test_retention_handles_more_than_one_batch(self):
        now=time.time()
        old=now-100*86400
        with self.database.connect() as db:
            db.executemany("INSERT INTO check_runs(model_id,channel_id,kind,status,created,finished) VALUES(?,?,'candy','passed',?,?)",
                           [(self.mid,self.cid,old,old)]*230)
        with patch.dict('os.environ',RUN_RETENTION_DAYS='90',ARTIFACT_RETENTION_DAYS='30',OUTPUT_RETENTION_DAYS='30'):
            Storage(self.database).cleanup(now)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs').fetchone()[0],0)

    def test_artifacts_cannot_extend_a_shorter_record_retention(self):
        now=time.time();old=now-8*86400
        scheduler=Scheduler(self.database,self.metrics)
        rid=scheduler.enqueue(self.mid,['pelican'])[0]
        scheduler.claim()
        aid,_=scheduler.storage.save_svg(rid,'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with self.database.connect() as db:
            db.execute("UPDATE check_runs SET status='passed',finished=? WHERE id=?",(old,rid))
            db.execute('UPDATE artifacts SET generated=? WHERE id=?',(old,aid))
        with patch.dict('os.environ',RUN_RETENTION_DAYS='7',ARTIFACT_RETENTION_DAYS='30',OUTPUT_RETENTION_DAYS='30'):
            scheduler.storage.cleanup(now)
        with self.database.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM check_runs WHERE id=?',(rid,)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM artifacts WHERE id=?',(aid,)).fetchone())


class SVGTests(unittest.TestCase):
    def test_encoded_external_resources_are_rejected(self):
        bodies=['<rect style="fill:u&#114;l(https://unwanted.example/pixel)"/>',
                '<style>rect { fill: u\\72l(https://unwanted.example/pixel); }</style><rect/>',
                '<g xml:base="https://unwanted.example/"><use href="#thing"/></g>',
                '<rect><animate attributeName="href" values="https://unwanted.example/pixel"/></rect>']
        for body in bodies:
            with self.subTest(body=body):
                svg,checks,_=inspect_svg('<svg xmlns="http://www.w3.org/2000/svg">'+body+'</svg>','')
                self.assertEqual(svg,'')
                self.assertFalse(checks['svg'])

    def test_safe_local_gradient_reference_remains_displayable(self):
        svg,checks,_=inspect_svg('<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs><rect fill="url(#g)"/></svg>','')
        self.assertTrue(checks['svg'])
        self.assertIn('url(#g)',svg)


class MigrationTests(unittest.TestCase):
    @staticmethod
    def legacy_fixture(directory, *, orphan=False, enabled=False):
        key='legacy-private-key';address='https://legacy.example/v1'
        with sqlite3.connect(Path(directory)/'monitor.sqlite3') as db:
            db.executescript('''CREATE TABLE nodes(id INTEGER PRIMARY KEY,name TEXT,base_url TEXT,api_key TEXT,model TEXT,protocol TEXT,effort TEXT,active INTEGER);
                CREATE TABLE settings(id INTEGER PRIMARY KEY,value TEXT);
                CREATE TABLE runs(id INTEGER PRIMARY KEY,node_id INTEGER,node_name TEXT,started REAL,finished REAL,status TEXT,source TEXT,
                model TEXT,base_url TEXT,effort TEXT,protocol TEXT,prompt TEXT,nonce TEXT,scene TEXT,tests TEXT,svg TEXT,output TEXT,checks TEXT,error TEXT,returned_model TEXT);''')
            db.execute('INSERT INTO nodes VALUES(7,?,?,?,?,?,?,1)',('original',address,key,'old-model','chat','medium'))
            db.execute('INSERT INTO settings VALUES(1,?)',(json.dumps({'enabled':enabled,'interval_minutes':19,'retry_count':3}),))
            secret_text=key+' '+address+' legacy.example'
            tests={'candy':{'status':'invalid','output':'22 '+secret_text,'error':secret_text,
                            'checks':{'reason':secret_text,'nested':[{'secret':secret_text}]}},
                   'pelican':{'status':'passed','checks':{'svg':True,'nested':[secret_text]},
                              'review':{'status':'passed','reason':secret_text}}}
            row=dict(id=5,node_id=999 if orphan else 7,node_name='deleted' if orphan else 'original',started=100,finished=110,
                     status='passed',source='scheduled',model='old-model',base_url=address,effort='medium',protocol='chat',
                     prompt=secret_text,nonce='nonce',scene=secret_text,tests=json.dumps(tests),svg='<svg xmlns="http://www.w3.org/2000/svg"></svg>',
                     output=secret_text,checks='{}',error=secret_text,returned_model='old-model')
            db.execute('INSERT INTO runs('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        return key,address

    def test_actual_legacy_backup_migrates_once_and_preserves_password(self):
        backup=Path(__file__).resolve().parents[2]/'backups/monitor-before-dashboard.sqlite3'
        if not backup.exists():
            self.skipTest('Local deployment backup is not part of source distribution')
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(backup,Path(tmp)/'monitor.sqlite3')
            with sqlite3.connect(backup) as old:
                old_auth=old.execute('SELECT salt,password_hash FROM admin_auth').fetchone()
                count=old.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]
            database=Database(tmp)
            Database(tmp)
            with database.connect() as db:
                self.assertEqual(tuple(db.execute('SELECT salt,password_hash FROM admin_auth').fetchone()),old_auth)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0],count)
                self.assertEqual([row[0] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')],[2,3,4,5])
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')

    def test_legacy_tests_keep_their_node_association_and_unknown_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(Path(tmp)/'monitor.sqlite3') as db:
                db.executescript('CREATE TABLE nodes(id INTEGER PRIMARY KEY,name TEXT,base_url TEXT,api_key TEXT,model TEXT,protocol TEXT,effort TEXT,active INTEGER);CREATE TABLE settings(id INTEGER PRIMARY KEY,value TEXT);CREATE TABLE runs(id INTEGER PRIMARY KEY,node_id INTEGER,node_name TEXT,started REAL,finished REAL,status TEXT,source TEXT,model TEXT,base_url TEXT,effort TEXT,protocol TEXT,prompt TEXT,nonce TEXT,scene TEXT,tests TEXT,svg TEXT,output TEXT,checks TEXT,error TEXT,returned_model TEXT);')
                db.execute('INSERT INTO nodes VALUES(7,?,?,?,?,?,?,1)',('old','https://example.com/v1','old-key','m','chat','medium'))
                db.execute('INSERT INTO settings VALUES(1,?)',(json.dumps({'enabled':False}),))
                db.execute('INSERT INTO runs VALUES(1,7,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('old',100,110,'passed','scheduled','m','https://example.com/v1','medium','chat','prompt','nonce','scene',json.dumps({'candy':{'status':'passed','output':'21'},'pelican':{'status':'passed','checks':{'svg':True}}}),'<svg></svg>','','{}','','m'))
            database=Database(tmp)
            repo=Repository(database)
            with database.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM request_attempts').fetchone()[0],0)
                self.assertFalse(db.execute('SELECT enabled FROM channels').fetchone()[0])
            self.assertEqual(Metrics(database).dashboard()['models'][0]['candy_rate'],100)

    def test_legacy_orphan_history_does_not_belong_to_unrelated_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.legacy_fixture(tmp,orphan=True)
            database=Database(tmp)
            with database.connect() as db:
                original=db.execute("SELECT id FROM channels WHERE title='original'").fetchone()[0]
                self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs WHERE channel_id=?',(original,)).fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs WHERE legacy_id=5').fetchone()[0],2)
                self.assertFalse(db.execute('SELECT c.enabled FROM channels c JOIN check_runs r ON r.channel_id=c.id WHERE r.legacy_id=5').fetchone()[0])

    def test_legacy_nested_text_is_redacted_even_after_key_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            key,address=self.legacy_fixture(tmp)
            database=Database(tmp);repo=Repository(database);metrics=Metrics(database)
            repo.changed=metrics.invalidate
            with database.connect() as db:
                ids=[r[0] for r in db.execute('SELECT id FROM check_runs')]
                cid=db.execute('SELECT id FROM channels').fetchone()[0]
            def assert_redacted():
                payloads=[metrics.dashboard(),repo.history(channel_id=cid)]+[repo.run(rid) for rid in ids]
                serialized=json.dumps(payloads,ensure_ascii=False)
                for secret in (key,address,'legacy.example'):
                    self.assertNotIn(secret,serialized)
            assert_redacted()
            repo.save_channel({'base_url':'https://replacement.example/v1','api_key':'replacement-private-key'},cid)
            assert_redacted()

    def test_legacy_nonactive_nodes_stay_paused_despite_global_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.legacy_fixture(tmp,enabled=True)
            with sqlite3.connect(Path(tmp)/'monitor.sqlite3') as db:
                db.execute('INSERT INTO nodes VALUES(8,?,?,?,?,?,?,0)',('backup','https://backup.example/v1','backup-key','b','responses','low'))
            database=Database(tmp)
            with database.connect() as db:
                states={r['title']:bool(r['enabled']) for r in db.execute('SELECT title,enabled FROM channels')}
                schedules=list(db.execute('SELECT interval_minutes FROM check_schedules'))
            self.assertTrue(states['original']);self.assertFalse(states['backup'])
            self.assertTrue(all(s[0]==19 for s in schedules))

    def test_retention_clears_legacy_payload_copies_and_guest_results(self):
        now=time.time()
        with tempfile.TemporaryDirectory() as tmp:
            self.legacy_fixture(tmp)
            with sqlite3.connect(Path(tmp)/'monitor.sqlite3') as db:
                db.execute('UPDATE runs SET started=?,finished=?',(now-40*86400,now-40*86400))
                db.execute('CREATE TABLE guest_results(id INTEGER PRIMARY KEY,created REAL,result TEXT)')
                db.execute('INSERT INTO guest_results VALUES(1,?,?)',(now-7200,'expired guest output'))
                db.execute('INSERT INTO guest_results VALUES(2,?,?)',(now-600,'recent guest output'))
            database=Database(tmp)
            with patch.dict('os.environ',RUN_RETENTION_DAYS='90',ARTIFACT_RETENTION_DAYS='30',OUTPUT_RETENTION_DAYS='30'):
                Storage(database).cleanup(now)
            with database.connect() as db:
                old=db.execute('SELECT output,tests,svg FROM runs').fetchone()
                self.assertEqual(old['output'],'');self.assertEqual(json.loads(old['tests']),{});self.assertEqual(old['svg'],'')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs').fetchone()[0],2)
                self.assertTrue(all(not r['output'] and not r['legacy_svg'] for r in db.execute('SELECT output,legacy_svg FROM check_runs')))
                self.assertEqual([r[0] for r in db.execute('SELECT id FROM guest_results')],[2])
            with patch.dict('os.environ',RUN_RETENTION_DAYS='90',ARTIFACT_RETENTION_DAYS='30',OUTPUT_RETENTION_DAYS='30'):
                Storage(database).cleanup(now+60*86400)
            with database.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs').fetchone()[0],0)

    def test_legacy_null_finish_times_remain_eligible_for_retention(self):
        for status in ('running','passed'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as tmp:
                self.legacy_fixture(tmp)
                tests={kind:dict(status=status,finished=None,output='21') for kind in ('candy','pelican')}
                with sqlite3.connect(Path(tmp)/'monitor.sqlite3') as db:
                    db.execute('UPDATE runs SET finished=NULL,status=?,tests=?',(status,json.dumps(tests)))
                database=Database(tmp)
                with database.connect() as db:
                    rows=db.execute('SELECT status,finished FROM check_runs').fetchall()
                    self.assertEqual(len(rows),2)
                    self.assertTrue(all(r['finished'] is not None for r in rows))
                    self.assertTrue(all(r['status']==('interrupted' if status=='running' else 'passed') for r in rows))
                with patch.dict('os.environ',RUN_RETENTION_DAYS='90',ARTIFACT_RETENTION_DAYS='30',OUTPUT_RETENTION_DAYS='30'):
                    Storage(database).cleanup(time.time()+91*86400)
                with database.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM check_runs').fetchone()[0],0)


class HTTPTests(unittest.TestCase):
    def test_read_only_api_and_auth_visibility(self):
        from urllib import request,error
        with tempfile.TemporaryDirectory() as tmp:
            app=Application(tmp)
            app.auth.setup('a-valid-admin-password')
            cid=app.repository.save_channel(dict(title='private',base_url='https://example.com/v1',api_key='secret',public=False))['id']
            server=Server(('127.0.0.1',0),Handler);server.app=app
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with request.urlopen(base+'/api/dashboard') as response:
                    self.assertEqual(json.load(response)['models'],[])
                for path,body,status in [('/api/admin/channels',None,401),('/api/guest/run',{},410),('/api/admin/channels',{},401)]:
                    req=request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
                    with self.assertRaises(error.HTTPError) as caught:request.urlopen(req)
                    self.assertEqual(caught.exception.code,status)
                self.assertEqual(app.repository.tasks(),[])
            finally:
                server.shutdown();server.server_close()

    def test_hidden_and_archived_results_are_blocked_on_every_read_route(self):
        from urllib import request,error
        with tempfile.TemporaryDirectory() as tmp:
            app=Application(tmp)
            app.auth.setup('a-valid-admin-password')
            cid=app.repository.save_channel(dict(title='public',base_url='https://example.com/v1',api_key='private-key'))['id']
            mid=app.repository.save_model(dict(model='mock'),channel_id=cid)['id']
            rid=app.scheduler.enqueue(mid,['pelican'])[0]
            aid,relative=app.scheduler.storage.save_svg(rid,'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
            thumbnail=relative.with_name('thumbnail.png')
            app.scheduler.storage.atomic(app.database.artifact_dir/thumbnail,b'\x89PNG\r\n\x1a\n')
            with app.database.connect() as db:
                db.execute("UPDATE artifacts SET thumbnail_path=?,render_status='ready' WHERE id=?",(str(thumbnail),aid))
            server=Server(('127.0.0.1',0),Handler);server.app=app
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base=f'http://127.0.0.1:{server.server_port}'
            paths=[f'/api/runs/{rid}',f'/api/runs/{rid}/svg',f'/api/artifacts/{aid}/svg',f'/api/artifacts/{aid}/thumbnail']
            try:
                for path in paths:
                    with request.urlopen(base+path) as response:self.assertEqual(response.status,200)
                for edit in ({'public':False},{'public':True,'archived':True}):
                    app.repository.save_channel(edit,cid)
                    for path in paths:
                        with self.assertRaises(error.HTTPError) as caught:request.urlopen(base+path)
                        self.assertEqual(caught.exception.code,404,path)
                    for path in (f'/api/channels/{cid}/history',f'/api/models/{mid}/runs'):
                        with request.urlopen(base+path) as response:self.assertEqual(json.load(response)['items'],[])
                    with request.urlopen(base+'/api/dashboard') as response:self.assertEqual(json.load(response)['models'],[])
                with app.database.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM request_attempts').fetchone()[0],0)
            finally:
                server.shutdown();server.server_close()

    def test_bad_admin_payloads_return_validation_errors_not_internal_errors(self):
        from urllib import request,error
        with tempfile.TemporaryDirectory() as tmp:
            app=Application(tmp);app.auth.setup('a-valid-admin-password')
            token=app.auth.login('a-valid-admin-password')
            cid=app.repository.save_channel(dict(title='test',base_url='https://example.com/v1',api_key='private-key'))['id']
            mid=app.repository.save_model(dict(model='mock'),channel_id=cid)['id']
            server=Server(('127.0.0.1',0),Handler);server.app=app
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                cases=[(f'/api/admin/models/{mid}/run',{'kinds':[{}]}),
                       (f'/api/admin/models/{mid}/run',{'kinds':['candy','candy']}),
                       (f'/api/admin/models/{mid}/run',{'kinds':['unknown']}),
                       (f'/api/admin/models/{mid}/run',{'kinds':[]}),
                       (f'/api/admin/models/{mid}',{'protocol':{}}),
                       (f'/api/admin/models/{mid}',{'effort':[]}),
                       (f'/api/admin/models/{mid}',{'schedules':{'candy':[]}}),
                       (f'/api/admin/channels/{cid}',{'api_key':{'nested':'invalid'}})]
                for path,body in cases:
                    req=request.Request(base+path,data=json.dumps(body).encode(),method='POST' if path.endswith('/run') else 'PATCH',
                                        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
                    with self.assertRaises(error.HTTPError) as caught:request.urlopen(req)
                    self.assertEqual(caught.exception.code,400,(path,body))
                self.assertEqual(app.repository.tasks(),[])
            finally:
                server.shutdown();server.server_close()


class CookieSessionHTTPTests(unittest.TestCase):
    """Real local HTTP requests, separate handlers, and browser-style cookie storage."""
    password='test-only-persistent-admin-password'

    def setUp(self):
        from urllib import request
        self.temp=tempfile.TemporaryDirectory()
        self.app=Application(self.temp.name)
        self.app.auth.setup(self.password)
        self.cid=self.app.repository.save_channel(dict(title='Original',base_url='https://example.com/v1',api_key='test-only-key'))['id']
        self.server=Server(('127.0.0.1',0),Handler);self.server.app=self.app
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.jar=CookieJar()
        self.client=request.build_opener(request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.temp.cleanup()

    def fetch(self,path,body=None,headers=None,method=None,client=None):
        from urllib import request
        req=request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,
                            headers={**({'Content-Type':'application/json'} if body is not None else {}),**(headers or {})},method=method)
        with (client or self.client).open(req) as response:
            raw=response.read()
            data=json.loads(raw) if response.headers.get_content_type()=='application/json' else raw.decode()
            return data,response.headers,response.status

    def login(self,headers=None):
        return self.fetch('/api/auth/login',{'password':self.password},headers=headers)

    def denied(self,path,*,body=None,headers=None,method=None,status=401):
        from urllib import error
        with self.assertRaises(error.HTTPError) as caught:
            self.fetch(path,body,headers,method)
        try:
            self.assertEqual(caught.exception.code,status)
            return json.load(caught.exception)
        finally:
            caught.exception.close()

    def test_cookie_login_survives_navigation_refresh_and_new_handler(self):
        from urllib import request
        before,_,_=self.fetch('/api/auth/status')
        self.assertFalse(before['authenticated']);self.assertIsNone(before['expires_at'])
        login,headers,_=self.login()
        cookies=SimpleCookie();cookies.load(headers['Set-Cookie'])
        cookie=cookies['llm_dashboard_admin_session']
        self.assertEqual(cookie['path'],'/')
        self.assertEqual(cookie['samesite'].lower(),'lax')
        self.assertEqual(cookie['max-age'],'7200')
        self.assertTrue(cookie['httponly']);self.assertFalse(cookie['secure'])
        self.assertEqual(cookie.value,login['token'])
        self.assertEqual(login['expires_in'],7200)
        self.assertAlmostEqual(login['expires_at']-time.time(),7200,delta=3)
        # Each HTTP/1.0 request creates a fresh handler; navigation includes no Bearer.
        for path in ('/','/admin','/admin/'):
            self.assertEqual(self.fetch(path)[2],200)
        fresh_client=request.build_opener(request.HTTPCookieProcessor(self.jar))
        status,_,_=self.fetch('/api/auth/status',client=fresh_client)
        self.assertTrue(status['authenticated']);self.assertEqual(status['expires_at'],login['expires_at'])
        self.assertNotIn('token',status)
        self.assertEqual(self.fetch('/api/admin/channels',client=fresh_client)[0][0]['title'],'Original')
        self.fetch(f'/api/admin/channels/{self.cid}',{'title':'Saved after navigation'},method='PATCH',client=fresh_client)
        self.assertEqual(self.fetch('/api/admin/channels')[0][0]['title'],'Saved after navigation')
        self.assertEqual(len(self.jar),1)
        self.assertFalse(next(iter(self.jar)).domain_specified)
        alternate=request.Request(f'http://localhost:{self.server.server_port}/api/auth/status')
        with fresh_client.open(alternate) as response:self.assertFalse(json.load(response)['authenticated'])
        self.assertEqual(self.app.repository.tasks(),[])

    def test_expired_session_cannot_read_or_write_and_logout_is_idempotent(self):
        login,_,_=self.login()
        with self.app.auth.lock:self.app.auth.sessions[login['token']]=time.time()-1
        status,_,_=self.fetch('/api/auth/status')
        self.assertFalse(status['authenticated']);self.assertIsNone(status['expires_at'])
        self.denied('/api/admin/channels')
        self.denied(f'/api/admin/channels/{self.cid}',body={'title':'Must not save'},method='PATCH')
        self.assertEqual(self.app.repository.channels()[0]['title'],'Original')
        for _ in range(2):
            result,headers,code=self.fetch('/api/auth/logout',{})
            self.assertEqual(code,200);self.assertTrue(result['ok'])
            cookie=SimpleCookie();cookie.load(headers['Set-Cookie'])
            self.assertEqual(cookie['llm_dashboard_admin_session']['max-age'],'0')
        self.assertEqual(len(self.jar),0)

    def test_logout_revokes_cookie_and_bearer_even_if_cookie_is_replayed(self):
        login,_,_=self.login()
        token=login['token']
        self.fetch('/api/auth/logout',{})
        self.assertEqual(len(self.jar),0)
        status,_,_=self.fetch('/api/auth/status',headers={'Cookie':'llm_dashboard_admin_session='+token})
        self.assertFalse(status['authenticated'])
        self.denied('/api/admin/channels',headers={'Cookie':'llm_dashboard_admin_session='+token})
        self.denied('/api/admin/channels',headers={'Authorization':'Bearer '+token})
        # Logout revokes both credentials if a Bearer API client also has a cookie.
        cookie_token=self.login()[0]['token']
        bearer_token=self.app.auth.login(self.password)
        self.fetch('/api/auth/logout',{},headers={'Authorization':'Bearer '+bearer_token})
        self.assertFalse(self.app.auth.valid(cookie_token));self.assertFalse(self.app.auth.valid(bearer_token))
        self.assertEqual(len(self.jar),0)

    def test_explicit_invalid_bearer_never_falls_back_to_valid_cookie(self):
        login,_,_=self.login()
        for authorization in ('Bearer invalid-session','Basic invalid-session','Bearer '):
            with self.subTest(authorization=authorization):
                headers={'Authorization':authorization}
                self.denied('/api/admin/channels',headers=headers)
                self.denied(f'/api/admin/channels/{self.cid}',body={'title':'Must not save'},headers=headers,method='PATCH')
                status,_,_=self.fetch('/api/auth/status',headers=headers)
                self.assertFalse(status['authenticated']);self.assertIsNone(status['expires_at'])
        self.assertTrue(self.fetch('/api/auth/status')[0]['authenticated'])
        duplicate='llm_dashboard_admin_session='+login['token']+'; llm_dashboard_admin_session='+login['token']
        self.assertFalse(self.fetch('/api/auth/status',headers={'Cookie':duplicate})[0]['authenticated'])
        self.assertEqual(self.app.repository.channels()[0]['title'],'Original')

    def test_cookie_writes_reject_cross_site_origin_and_fetch_metadata(self):
        self.login()
        for headers in ({'Origin':'https://other.example'}, {'Origin':'null'}, {'Sec-Fetch-Site':'cross-site'},
                        {'Origin':self.base,'Sec-Fetch-Site':'cross-site'}):
            with self.subTest(headers=headers):
                self.denied(f'/api/admin/channels/{self.cid}',body={'title':'Must not save'},method='PATCH',headers=headers,status=403)
                self.denied('/api/auth/logout',body={},headers=headers,status=403)
        self.assertEqual(self.app.repository.channels()[0]['title'],'Original')
        self.assertTrue(self.fetch('/api/auth/status')[0]['authenticated'])
        self.fetch(f'/api/admin/channels/{self.cid}',{'title':'Same origin accepted'},method='PATCH',
                   headers={'Origin':self.base,'Sec-Fetch-Site':'same-origin'})
        self.assertEqual(self.app.repository.channels()[0]['title'],'Same origin accepted')

    def test_restart_loses_sessions_but_keeps_password(self):
        self.login()
        self.app=Application(self.temp.name);self.server.app=self.app
        self.assertFalse(self.fetch('/api/auth/status')[0]['authenticated'])
        self.denied('/api/admin/channels')
        self.login()
        self.assertTrue(self.fetch('/api/auth/status')[0]['authenticated'])

    def test_http_proxy_headers_do_not_mark_cookie_secure_or_expose_password(self):
        login,headers,_=self.login({'X-Forwarded-Proto':'https','Forwarded':'proto=https'})
        cookies=SimpleCookie();cookies.load(headers['Set-Cookie'])
        self.assertFalse(cookies['llm_dashboard_admin_session']['secure'])
        status,status_headers,_=self.fetch('/api/auth/status')
        self.assertTrue(status['authenticated'])
        self.assertNotIn(self.password,json.dumps([login,status,dict(headers),dict(status_headers)]))
        self.assertNotIn(login['token'],json.dumps(status))
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(status_headers['Cache-Control'],'no-store')


if __name__=='__main__':
    unittest.main()
