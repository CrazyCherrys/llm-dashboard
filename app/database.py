"""SQLite connections, versioned migration, and legacy data preservation."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import time
from checks import json_object, redact, redact_nested
from prompt_templates import DEFAULT_TEMPLATES

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied REAL NOT NULL);
CREATE TABLE IF NOT EXISTS admin_auth(id INTEGER PRIMARY KEY CHECK(id=1),salt TEXT NOT NULL,password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_templates(
 id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('candy','pelican')),
 body TEXT NOT NULL,rule_type TEXT NOT NULL,expected_answer TEXT NOT NULL DEFAULT '',review_criteria TEXT NOT NULL DEFAULT '',
 require_animation INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,archived INTEGER NOT NULL DEFAULT 0,
 builtin_key TEXT UNIQUE,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS model_categories(
 id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,name_key TEXT NOT NULL UNIQUE,
 sort_order INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS channels(
 id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,base_url TEXT NOT NULL,api_key TEXT NOT NULL DEFAULT '',
 enabled INTEGER NOT NULL DEFAULT 0,public INTEGER NOT NULL DEFAULT 1,archived INTEGER NOT NULL DEFAULT 0,
 sort_order INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS model_configs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER NOT NULL REFERENCES channels(id),display_name TEXT NOT NULL,
 model TEXT NOT NULL,protocol TEXT NOT NULL DEFAULT 'responses',effort TEXT NOT NULL DEFAULT 'medium',
 retry_count INTEGER NOT NULL DEFAULT 2,timeout_seconds INTEGER NOT NULL DEFAULT 300,
 first_token_timeout INTEGER NOT NULL DEFAULT 120,idle_timeout INTEGER NOT NULL DEFAULT 60,
 max_output_tokens INTEGER NOT NULL DEFAULT 16000,enabled INTEGER NOT NULL DEFAULT 1,
 archived INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS models_channel ON model_configs(channel_id,archived,id);
CREATE TABLE IF NOT EXISTS check_schedules(
 id INTEGER PRIMARY KEY AUTOINCREMENT,model_id INTEGER NOT NULL REFERENCES model_configs(id),
 kind TEXT NOT NULL CHECK(kind IN ('candy','pelican')),enabled INTEGER NOT NULL DEFAULT 0,
 interval_minutes INTEGER NOT NULL DEFAULT 30,next_run REAL,UNIQUE(model_id,kind));
CREATE INDEX IF NOT EXISTS schedules_due ON check_schedules(enabled,next_run);
CREATE TABLE IF NOT EXISTS check_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,model_id INTEGER NOT NULL REFERENCES model_configs(id),
 channel_id INTEGER NOT NULL REFERENCES channels(id),schedule_id INTEGER REFERENCES check_schedules(id),
 kind TEXT NOT NULL CHECK(kind IN ('candy','pelican')),source TEXT NOT NULL DEFAULT 'scheduled',
 status TEXT NOT NULL,created REAL NOT NULL,started REAL,finished REAL,stage TEXT NOT NULL DEFAULT 'queued',
 config TEXT NOT NULL DEFAULT '{}',prompt TEXT NOT NULL DEFAULT '',nonce TEXT NOT NULL DEFAULT '',
 scene TEXT NOT NULL DEFAULT '',output TEXT NOT NULL DEFAULT '',checks TEXT NOT NULL DEFAULT '{}',
 review TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',returned_model TEXT NOT NULL DEFAULT '',
 rule_version TEXT NOT NULL DEFAULT 'candy-v1-independent-21',legacy_id INTEGER,legacy_svg TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_check ON check_runs(model_id,kind) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS runs_model_time ON check_runs(model_id,kind,created DESC);
CREATE INDEX IF NOT EXISTS runs_channel_time ON check_runs(channel_id,created DESC);
CREATE INDEX IF NOT EXISTS runs_status ON check_runs(status,created);
CREATE INDEX IF NOT EXISTS runs_finished ON check_runs(finished);
CREATE TABLE IF NOT EXISTS request_attempts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL REFERENCES check_runs(id) ON DELETE CASCADE,
 channel_id INTEGER NOT NULL,model_id INTEGER NOT NULL,role TEXT NOT NULL DEFAULT 'primary',
 attempt INTEGER NOT NULL,started REAL NOT NULL,finished REAL,status TEXT NOT NULL DEFAULT 'running',
 http_status INTEGER,first_token_seconds REAL,duration_seconds REAL,input_tokens INTEGER,
 output_tokens INTEGER,cached_tokens INTEGER,error_code TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS attempts_channel_time ON request_attempts(channel_id,role,started DESC);
CREATE INDEX IF NOT EXISTS attempts_run ON request_attempts(run_id,id);
CREATE TABLE IF NOT EXISTS artifacts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL UNIQUE REFERENCES check_runs(id) ON DELETE CASCADE,
 svg_path TEXT NOT NULL,thumbnail_path TEXT,generated REAL NOT NULL,render_status TEXT NOT NULL DEFAULT 'pending');
CREATE INDEX IF NOT EXISTS artifacts_time ON artifacts(generated);
"""


class Database:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'monitor.sqlite3'
        self.artifact_dir = self.directory / 'artifacts'
        self.artifact_dir.mkdir(exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
                self.migrate_legacy(db)
                db.execute('INSERT INTO schema_migrations VALUES(2,?)', (time.time(),))
            if not db.execute('SELECT 1 FROM schema_migrations WHERE version=3').fetchone():
                self.migrate_model_cards(db)
                db.execute('INSERT INTO schema_migrations VALUES(3,?)', (time.time(),))
            if not db.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
                self.migrate_templates(db)
                db.execute('INSERT INTO schema_migrations VALUES(4,?)', (time.time(),))
            if not db.execute('SELECT 1 FROM schema_migrations WHERE version=5').fetchone():
                # Enable new multi-model cards without regrouping existing data.
                db.execute('DROP INDEX IF EXISTS model_private_connection')
                db.execute('ALTER TABLE channels ADD COLUMN category_id INTEGER REFERENCES model_categories(id) ON DELETE SET NULL')
                db.execute('ALTER TABLE model_configs ADD COLUMN position INTEGER NOT NULL DEFAULT 0')
                db.execute('CREATE INDEX cards_category ON channels(category_id,archived,sort_order,id)')
                db.execute('INSERT INTO schema_migrations VALUES(5,?)', (time.time(),))
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def migrate_model_cards(self, db):
        """Keep model/run IDs while giving every model a private connection row.

        channels remains an internal storage detail for existing run foreign keys.
        The unique index prevents any newly configured models sharing credentials.
        """
        db.execute('ALTER TABLE model_configs ADD COLUMN category_id INTEGER REFERENCES model_categories(id) ON DELETE SET NULL')
        for channel in db.execute('SELECT * FROM channels ORDER BY id').fetchall():
            models = db.execute('SELECT * FROM model_configs WHERE channel_id=? ORDER BY id',(channel['id'],)).fetchall()
            for index, model in enumerate(models):
                title = channel['title'] if len(models)==1 else f"{channel['title']} · {model['display_name']}"
                title = title[:120]
                if index == 0:
                    cid = channel['id']
                    db.execute('UPDATE channels SET title=? WHERE id=?',(title,cid))
                else:
                    keys = ('base_url','api_key','enabled','public','archived','sort_order','created','updated')
                    cid = db.execute('INSERT INTO channels(title,'+','.join(keys)+') VALUES('+','.join('?' for _ in range(9))+')',
                                     (title,*[channel[k] for k in keys])).lastrowid
                    db.execute('UPDATE model_configs SET channel_id=? WHERE id=?',(cid,model['id']))
                    db.execute('UPDATE check_runs SET channel_id=? WHERE model_id=?',(cid,model['id']))
                    db.execute('UPDATE request_attempts SET channel_id=? WHERE model_id=?',(cid,model['id']))
                db.execute('UPDATE model_configs SET display_name=? WHERE id=?',(title,model['id']))
        db.execute('CREATE UNIQUE INDEX model_private_connection ON model_configs(channel_id)')
        db.execute('CREATE INDEX models_category ON model_configs(category_id,archived,id)')
        db.execute('CREATE INDEX attempts_model_time ON request_attempts(model_id,role,started DESC)')

    def migrate_templates(self, db):
        db.execute('ALTER TABLE check_schedules ADD COLUMN template_id INTEGER REFERENCES prompt_templates(id)')
        db.execute('ALTER TABLE check_runs ADD COLUMN template_id INTEGER REFERENCES prompt_templates(id)')
        db.execute('ALTER TABLE check_runs ADD COLUMN template_revision INTEGER')
        for template in DEFAULT_TEMPLATES:
            keys = tuple(template)
            tid = db.execute('INSERT INTO prompt_templates('+','.join(keys)+',created,updated) VALUES('+','.join('?' for _ in range(len(keys)+2))+')',
                             (*template.values(),time.time(),time.time())).lastrowid
            db.execute('UPDATE check_schedules SET template_id=? WHERE kind=?',(tid,template['kind']))
        db.execute('CREATE INDEX schedules_template ON check_schedules(template_id)')
        db.execute('CREATE INDEX runs_logic_overview ON check_runs(kind,finished,model_id,status)')

    def migrate_legacy(self, db):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'nodes' not in tables:
            return
        config = {}
        if 'settings' in tables:
            row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
            config = json_object(row[0]) if row else {}
        now = time.time()
        nodes = db.execute('SELECT * FROM nodes ORDER BY id').fetchall()
        mapping = {}
        for node in nodes:
            enabled = bool(node['active'] and config.get('enabled') and node['api_key'])
            cid = db.execute('INSERT INTO channels(title,base_url,api_key,enabled,created,updated) VALUES(?,?,?,?,?,?)',
                             (node['name'], node['base_url'], node['api_key'], enabled, now, now)).lastrowid
            mid = db.execute('INSERT INTO model_configs(channel_id,display_name,model,protocol,effort,retry_count,timeout_seconds,max_output_tokens,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (cid, node['model'], node['model'], node['protocol'], node['effort'], config.get('retry_count', 2),
                              config.get('timeout_seconds', 300), config.get('max_output_tokens', 16000), now, now)).lastrowid
            mapping[node['id']] = (cid, mid, dict(node))
            for kind in ('candy', 'pelican'):
                db.execute('INSERT INTO check_schedules(model_id,kind,enabled,interval_minutes,next_run) VALUES(?,?,?,?,?)',
                           (mid, kind, True, config.get('interval_minutes', 30), now if enabled else None))
        if 'runs' not in tables:
            return
        orphan_mapping = {}
        for old in db.execute('SELECT * FROM runs ORDER BY id').fetchall():
            old = dict(old)
            association = mapping.get(old.get('node_id'))
            if not association:
                # Deleted nodes and guest runs get their own paused historical channel;
                # attaching them to an unrelated live node corrupts statistics.
                orphan_key = (old.get('node_id'),old.get('node_name'),old.get('base_url'),old.get('model'),old.get('protocol'),old.get('effort'))
                association = orphan_mapping.get(orphan_key)
                if not association:
                    title = old.get('node_name') or '历史独立检测'
                    base_url = old.get('base_url') or ''
                    model = old.get('model') or '历史模型'
                    cid = db.execute('INSERT INTO channels(title,base_url,created,updated) VALUES(?,?,?,?)',
                                     (title,base_url,now,now)).lastrowid
                    mid = db.execute('''INSERT INTO model_configs(channel_id,display_name,model,protocol,effort,enabled,created,updated)
                         VALUES(?,?,?,?,?,0,?,?)''',(cid,model,model,old.get('protocol') or 'responses',old.get('effort') or 'medium',now,now)).lastrowid
                    for kind in ('candy','pelican'):
                        db.execute('INSERT INTO check_schedules(model_id,kind) VALUES(?,?)',(mid,kind))
                    association = (cid,mid,dict(api_key='',base_url=base_url))
                    orphan_mapping[orphan_key] = association
            cid, mid, secrets = association
            # The run address may differ from its node's current address.
            run_secrets = dict(secrets,base_url=old.get('base_url') or secrets.get('base_url',''))
            tests = json_object(old.get('tests'))
            if not tests:
                tests = {'pelican': {'status': old['status'], 'checks': json_object(old.get('checks'))}}
            snapshot = {key: old.get(key) or '' for key in ('model', 'effort', 'protocol')}
            snapshot['title'] = old.get('node_name') or '历史节点'
            snapshot = redact_nested(redact_nested(snapshot,secrets),run_secrets)
            for kind, result in tests.items():
                if kind not in ('candy', 'pelican') or not isinstance(result,dict) or result.get('status') == 'not_run':
                    continue
                result = redact_nested(redact_nested(result,secrets),run_secrets)
                status = result.get('status', old['status'])
                finished = result.get('finished') or old.get('finished') or old.get('started') or now
                if status in ('queued', 'running'):
                    status = 'interrupted'
                    finished = now
                db.execute('''INSERT INTO check_runs(model_id,channel_id,kind,source,status,created,started,finished,stage,config,prompt,nonce,scene,output,checks,review,error,returned_model,legacy_id,legacy_svg)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (mid,cid,kind,'legacy',status,old.get('started') or now,result.get('started') or old.get('started') or now,
                            finished,'finished',json.dumps(snapshot),
                            redact(redact(old.get('prompt') or '',secrets),run_secrets) if kind=='pelican' else '',
                            redact(redact(old.get('nonce') or '',secrets),run_secrets),redact(redact(old.get('scene') or '',secrets),run_secrets),
                            result.get('output') or (redact(redact(old.get('output') or '',secrets),run_secrets) if kind=='pelican' else ''),
                            json.dumps(result.get('checks',{})),json.dumps(result.get('review') or {}),
                            result.get('error') or redact(redact(old.get('error') or '',secrets),run_secrets),
                            result.get('returned_model') or redact(redact(old.get('returned_model') or '',secrets),run_secrets),
                            old['id'],redact(redact(old.get('svg') or '',secrets),run_secrets) if kind=='pelican' else ''))

    def recover(self):
        now = time.time()
        with self.connect() as db:
            db.execute("UPDATE check_runs SET status='interrupted',stage='finished',finished=?,error='服务重启，任务已中断，未自动重试' WHERE status IN ('queued','running')", (now,))
            db.execute("UPDATE request_attempts SET status='interrupted',finished=?,duration_seconds=?-started,error_code='interrupted',error='服务重启' WHERE status='running'", (now,now))
