"""Durable bounded queue, single scheduler, and per-check execution."""
import json
import logging
import secrets
import sqlite3
import threading
import time
from checks import RULE_VERSION, SCENES, candy_passes, inspect_svg, redact, json_object
from prompt_templates import render_prompt, evaluate_logic
from storage import Storage
from settings import SiteSettings
from repositories import normalize_url
from upstream import UpstreamError, call_model
from visual_review import render_frames, review_pelican

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, database, metrics, model_call=call_model, concurrency=None, settings=None):
        self.database,self.metrics,self.model_call = database,metrics,model_call
        self.settings = settings if settings is not None else SiteSettings(database)
        self.storage = Storage(database, self.settings)
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.concurrency = (self.settings.get()['worker_concurrency'] if concurrency is None
                            else SiteSettings.validate('worker_concurrency', concurrency))
        self._worker_condition = threading.Condition()
        self._active_workers = 0
        self.queue_limit = 32
        self.threads = []

    def changed(self):
        self.metrics.invalidate()
        self.wake.set()
        with self._worker_condition:
            self._worker_condition.notify_all()

    def settings_changed(self):
        with self._worker_condition:
            self.concurrency = self.settings.get()['worker_concurrency']
            self._worker_condition.notify_all()
        self.changed()

    def start(self):
        self.database.recover()
        # Idle workers wait for capacity; keeping the bounded pool allows the
        # website to raise concurrency without interrupting running requests.
        for target in [self.loop]+[self.worker]*8:
            thread = threading.Thread(target=target,daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stopped.set()
        self.wake.set()
        with self._worker_condition:
            self._worker_condition.notify_all()

    def snapshot(self, row):
        return {key:row[key] for key in ('title','model','display_name','protocol','effort','retry_count',
                                        'timeout_seconds','first_token_timeout','idle_timeout','max_output_tokens')}

    def enqueue(self, model_id, kinds=None, source='manual', now=None):
        now = time.time() if now is None else now
        ids = []
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT m.*,c.title,c.api_key,c.enabled AS channel_enabled,c.archived AS channel_archived
              FROM model_configs m JOIN channels c ON c.id=m.channel_id WHERE m.id=?''',(model_id,)).fetchone()
            if not row or row['archived'] or row['channel_archived']:
                raise ValueError('模型不存在或已归档')
            if not row['api_key']:
                raise ValueError('请先填写模型 API Key')
            if source=='scheduled' and (not row['enabled'] or not row['channel_enabled']):
                return []
            schedules = db.execute('SELECT * FROM check_schedules WHERE model_id=?',(model_id,)).fetchall()
            if kinds is None:
                kinds = [s['kind'] for s in schedules if s['enabled']]
            if not isinstance(kinds,(list,tuple)) or not kinds or any(kind not in ('candy','pelican') for kind in kinds) or len(set(kinds))!=len(kinds):
                raise ValueError('请至少选择一个检测项目')
            pending = db.execute("SELECT COUNT(*) FROM check_runs WHERE status='queued'").fetchone()[0]
            if source=='manual':
                if pending+len(kinds)>self.queue_limit:
                    raise ValueError('任务队列已满，请稍后重试')
                if db.execute("SELECT 1 FROM check_runs WHERE model_id=? AND kind IN ("+','.join('?' for _ in kinds)+") AND status IN ('queued','running')",(model_id,*kinds)).fetchone():
                    raise ValueError('该检测项目已有任务排队或运行')
            for schedule in schedules:
                if schedule['kind'] not in kinds:
                    continue
                if source=='scheduled' and (not schedule['enabled'] or (schedule['next_run'] or 0)>now):
                    continue
                if db.execute("SELECT 1 FROM check_runs WHERE model_id=? AND kind=? AND status IN ('queued','running')",(model_id,schedule['kind'])).fetchone():
                    if source=='scheduled':
                        db.execute('UPDATE check_schedules SET next_run=? WHERE id=?',(now+schedule['interval_minutes']*60,schedule['id']))
                    continue
                if pending>=self.queue_limit:
                    break
                template = db.execute('SELECT * FROM prompt_templates WHERE id=? AND kind=? AND archived=0',
                                      (schedule['template_id'],schedule['kind'])).fetchone()
                if not template:
                    raise ValueError('请先为该检测项目选择可用的提示词模板')
                template = {key:template[key] for key in ('id','name','kind','body','rule_type','expected_answer','review_criteria','require_animation','revision')}
                template['require_animation'] = bool(template['require_animation'])
                nonce,scene = secrets.token_hex(4).upper(),secrets.choice(SCENES)
                prompt = render_prompt(template,scene,nonce)
                snapshot = dict(self.snapshot(row),prompt_template=template)
                rule = f"template-{template['id']}-v{template['revision']}-{template['rule_type']}"
                rid = db.execute('''INSERT INTO check_runs(model_id,channel_id,schedule_id,kind,source,status,created,config,prompt,nonce,scene,rule_version,template_id,template_revision)
                    VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)''',
                                 (model_id,row['channel_id'],schedule['id'],schedule['kind'],source,now,json.dumps(snapshot),prompt,nonce,scene,rule,template['id'],template['revision'])).lastrowid
                ids.append(rid)
                pending += 1
                if source=='scheduled':
                    db.execute('UPDATE check_schedules SET next_run=? WHERE id=?',(now+schedule['interval_minutes']*60,schedule['id']))
        self.changed()
        return ids

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.database.connect() as db:
            due = db.execute('''SELECT DISTINCT s.model_id FROM check_schedules s JOIN model_configs m ON m.id=s.model_id JOIN channels c ON c.id=m.channel_id
                WHERE s.enabled=1 AND COALESCE(s.next_run,0)<=? AND m.enabled=1 AND m.archived=0 AND c.enabled=1 AND c.archived=0
                AND c.api_key!='' ORDER BY COALESCE(s.next_run,0),s.id LIMIT 32''',(now,)).fetchall()
        for row in due:
            try:
                self.enqueue(row['model_id'],source='scheduled',now=now)
            except ValueError:
                continue

    def loop(self):
        last_cleanup = 0
        while not self.stopped.is_set():
            try:
                self.tick()
                if time.time()-last_cleanup>3600:
                    self.storage.cleanup()
                    last_cleanup = time.time()
            except Exception:
                log.exception('Scheduler iteration failed')
            self.wake.wait(2)
            self.wake.clear()

    def claim(self):
        task = None
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute("SELECT * FROM check_runs WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not run:
                return None
            config = db.execute('''SELECT m.*,c.title,c.base_url,c.api_key,c.enabled AS channel_enabled,c.archived AS channel_archived
                FROM model_configs m JOIN channels c ON c.id=m.channel_id WHERE m.id=?''',(run['model_id'],)).fetchone()
            schedule = db.execute('SELECT enabled FROM check_schedules WHERE id=?',(run['schedule_id'],)).fetchone()
            reason = ''
            if not config or config['archived'] or config['channel_archived'] or (run['source']=='scheduled' and
                (not config['enabled'] or not config['channel_enabled'] or not schedule or not schedule['enabled'])):
                reason = '配置已暂停或归档'
            if not reason:
                config = dict(config)
                try:
                    config['base_url'] = normalize_url(config['base_url'])
                    if not config['api_key'] or any(ord(c)<33 or ord(c)>126 for c in config['api_key']):
                        raise ValueError('API Key 无效')
                except (ValueError,TypeError):
                    reason = '模型连接配置无效，请在后台更新地址和 API Key'
            if reason:
                db.execute("UPDATE check_runs SET status='cancelled',stage='finished',finished=?,error=? WHERE id=?",(time.time(),reason,run['id']))
            else:
                started = time.time()
                snapshot = self.snapshot(config)
                template = json_object(run['config']).get('prompt_template')
                if template:
                    snapshot['prompt_template'] = template
                    config['prompt_template'] = template
                db.execute("UPDATE check_runs SET status='running',stage='requesting',started=?,config=? WHERE id=?",
                           (started,json.dumps(snapshot),run['id']))
                task = (dict(run,status='running',stage='requesting',started=started,config=json.dumps(snapshot)),config)
        self.changed()
        return task

    def worker(self):
        while not self.stopped.is_set():
            task = None
            try:
                with self._worker_condition:
                    while self._active_workers >= self.concurrency and not self.stopped.is_set():
                        self._worker_condition.wait()
                    if self.stopped.is_set():
                        return
                    # Claim and reserve capacity under the same lock used by
                    # settings_changed, so lowering the limit cannot race with
                    # workers taking more queued requests.
                    task = self.claim()
                    if task:
                        self._active_workers += 1
                    else:
                        self._worker_condition.wait(.5)
                if task:
                    self.execute(*task)
                    continue
            except Exception:
                log.exception('Worker iteration failed')
            finally:
                if task:
                    with self._worker_condition:
                        self._active_workers -= 1
                        self._worker_condition.notify_all()
            self.stopped.wait(.5)

    def stage(self, run_id, stage):
        with self.database.connect() as db:
            db.execute('UPDATE check_runs SET stage=? WHERE id=?',(stage,run_id))
        self.changed()

    def request(self, run, config, prompt, role='primary', deadline=None):
        local_deadline = time.monotonic()+config['timeout_seconds']
        deadline = min(local_deadline,deadline) if deadline is not None else local_deadline
        for attempt in range(1,config['retry_count']+2):
            remaining = deadline-time.monotonic()
            if remaining<=0:
                raise UpstreamError('本项检测总时间已耗尽','timeout')
            attempt_clock,attempt_started = time.monotonic(),time.time()
            with self.database.connect() as db:
                aid = db.execute('INSERT INTO request_attempts(run_id,channel_id,model_id,role,attempt,started) VALUES(?,?,?,?,?,?)',
                                 (run['id'],run['channel_id'],run['model_id'],role,attempt,attempt_started)).lastrowid
            first_seconds = None
            def on_first(seconds):
                nonlocal first_seconds
                if first_seconds is not None:
                    return
                first_seconds = seconds
                with self.database.connect() as db:
                    db.execute('UPDATE request_attempts SET first_token_seconds=? WHERE id=?',(seconds,aid))
                self.changed()
            failure = None
            try:
                if attempt>1:
                    self.stage(run['id'],'requesting' if role=='primary' else 'reviewing')
                output,metrics,model = self.model_call(dict(config,timeout_seconds=remaining),prompt,on_first=on_first)
            except UpstreamError as exc:
                metrics,failure = exc.metrics,exc
                output,model = '', ''
            except Exception:
                # An internal callback/parser failure must not leave a billed attempt
                # permanently running or trigger repeated calls with broken settings.
                log.exception('Request execution failed for check %s',run['id'])
                metrics,failure = {},UpstreamError('请求执行异常，请查看管理员运行日志','execution')
                output,model = '', ''
            metrics = dict(metrics)
            metrics.setdefault('finished',time.time())
            metrics.setdefault('duration_seconds',time.monotonic()-attempt_clock)
            metrics.setdefault('first_token_seconds',first_seconds)
            metrics.setdefault('status','failed' if failure else 'success')
            if failure and metrics['status'] not in ('failed','timeout','interrupted'):
                metrics['status'] = 'failed'
            keys = ('finished','status','http_status','first_token_seconds','duration_seconds','input_tokens','output_tokens','cached_tokens')
            with self.database.connect() as db:
                db.execute('UPDATE request_attempts SET '+','.join(k+'=?' for k in keys)+',error_code=?,error=? WHERE id=?',
                           (*[metrics.get(k) for k in keys],redact(failure.code,config) if failure else '',redact(str(failure),config) if failure else '',aid))
            self.changed()
            if not failure:
                return output,metrics,model
            delay = min(2**(attempt-1),8)
            if not failure.retryable or attempt>config['retry_count'] or deadline-time.monotonic()<=delay:
                raise failure
            self.stage(run['id'],'retrying')
            if self.stopped.wait(delay):
                raise UpstreamError('服务停止，重试中断','interrupted')
        raise UpstreamError('请求失败')

    def execute(self, run, config):
        status,error,output,checks,review,returned = 'error','', '',{}, {},''
        deadline = time.monotonic()+config['timeout_seconds']
        try:
            output,usage,returned = self.request(run,config,run['prompt'],deadline=deadline)
            output = redact(output,config)
            if run['kind']=='candy':
                if config.get('prompt_template'):
                    status,checks,error = evaluate_logic(output,config['prompt_template'])
                else:
                    checks = {'answer_21':candy_passes(output)}
                    status = 'passed' if checks['answer_21'] else 'invalid'
                    error = '' if status=='passed' else '回答中未出现独立数字 21'
            else:
                svg,checks,error = inspect_svg(output,run['nonce'],require_animation=config.get('prompt_template',{}).get('require_animation',True))
                status = 'passed' if all(checks.values()) else 'invalid'
                if svg:
                    artifact_id,relative = self.storage.save_svg(run['id'],svg)
                    self.stage(run['id'],'rendering')
                    try:
                        frames = render_frames(svg,timeout=deadline-time.monotonic())
                        self.storage.save_thumbnail(artifact_id,relative,frames)
                        if status=='passed':
                            self.stage(run['id'],'reviewing')
                            def review_call(review_config,prompt):
                                answer,meta,model = self.request(run,dict(review_config,timeout_seconds=min(config['timeout_seconds'],120)),prompt,'review',deadline=deadline)
                                return answer,meta,model
                            review = review_pelican(config,svg,review_call,frames=frames)
                            status = review['status'] if review['status']!='uncertain' else 'error'
                            error = review['reason']
                    except Exception:
                        with self.database.connect() as db:
                            db.execute("UPDATE artifacts SET render_status=CASE WHEN thumbnail_path IS NULL THEN 'error' ELSE render_status END WHERE id=?",(artifact_id,))
                        if status=='passed':
                            status,error = 'error','图形审核未完成：渲染、图片输入或审核请求失败'
                            review = {'status':'error','reason':error}
        except UpstreamError as exc:
            error = str(exc)
            status = 'interrupted' if exc.code=='interrupted' else 'error'
        except Exception:
            error = '检测执行异常，请查看管理员运行日志'
            log.exception('Check %s failed',run['id'])
        finally:
            with self.database.connect() as db:
                db.execute('''UPDATE check_runs SET status=?,stage='finished',finished=?,output=?,checks=?,review=?,error=?,returned_model=? WHERE id=?''',
                           (status,time.time(),output,json.dumps(checks),json.dumps(review),redact(error,config),redact(returned,config)[:200],run['id']))
            self.changed()
