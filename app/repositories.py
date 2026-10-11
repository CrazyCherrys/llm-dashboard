"""Configuration validation, management, and public result projections."""
import json
import sqlite3
import time
from urllib import parse
from checks import mask, mask_url, redact, redact_nested, json_object
from prompt_templates import validate_template

MODEL_DEFAULTS = dict(display_name='',model='',protocol='responses',effort='medium',retry_count=2,
                      timeout_seconds=300,first_token_timeout=120,idle_timeout=60,max_output_tokens=16000,enabled=True,position=0)


def text(value, label, maximum=120):
    if not isinstance(value,str) or not 1<=len(value.strip())<=maximum or any(ord(c)<32 for c in value):
        raise ValueError(f'{label}需要 1–{maximum} 个字符，不能包含控制字符')
    return value.strip()


def boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f'{label}必须为开关值')
    return value


def integer(value, label, low, high):
    if type(value) is not int or not low<=value<=high:
        raise ValueError(f'{label}需要 {low}–{high} 之间的整数')
    return value


def normalize_url(value):
    value = text(value,'API 地址',2048)
    if '://' not in value:
        value = 'https://' + value
    if any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('API 地址不能包含空格或反斜杠')
    try:
        url = parse.urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError('API 地址格式不正确') from None
    if not url.hostname or url.username or url.password or url.query or url.fragment or (port is not None and not 1<=port<=65535):
        raise ValueError('API 地址不能包含密钥、账号、查询参数或片段')
    if url.scheme!='https' and not (url.scheme=='http' and url.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('请使用 HTTPS API 地址；本机测试可使用 HTTP')
    value = value.rstrip('/')
    for suffix in ('/responses','/chat/completions'):
        value = value.removesuffix(suffix)
    if not parse.urlsplit(value).path:
        value += '/v1'
    return value


class Repository:
    def __init__(self, database):
        self.database = database
        self.changed = lambda: None

    def categories(self):
        with self.database.connect() as db:
            return [dict(row) for row in db.execute('''SELECT g.id,g.name,g.sort_order,
                COUNT(DISTINCT CASE WHEN c.archived=0 THEN c.id END) AS card_count,
                COUNT(CASE WHEN m.archived=0 AND c.archived=0 THEN 1 END) AS model_count
                FROM model_categories g LEFT JOIN channels c ON c.category_id=g.id
                LEFT JOIN model_configs m ON m.channel_id=c.id GROUP BY g.id ORDER BY g.sort_order,g.id''')]

    def templates(self):
        with self.database.connect() as db:
            rows = db.execute('''SELECT t.*,COUNT(CASE WHEN m.archived=0 AND c.archived=0 THEN 1 END) AS model_count
                FROM prompt_templates t LEFT JOIN check_schedules s ON s.template_id=t.id
                LEFT JOIN model_configs m ON m.id=s.model_id LEFT JOIN channels c ON c.id=m.channel_id
                WHERE t.archived=0 GROUP BY t.id ORDER BY t.kind,t.id''').fetchall()
        return [dict(r,require_animation=bool(r['require_animation'])) for r in rows]

    def save_template(self, values, template_id=None):
        keys = ('name','kind','body','rule_type','expected_answer','review_criteria','require_animation')
        if not isinstance(values,dict) or set(values)-set(keys):
            raise ValueError('模板字段不正确')
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM prompt_templates WHERE id=? AND archived=0',(template_id,)).fetchone() if template_id else None
            if template_id and not old:
                raise ValueError('模板不存在或已归档')
            config = dict(old) if old else dict(name='',kind='candy',body='',rule_type='number',expected_answer='',review_criteria='',require_animation=False)
            config['require_animation'] = bool(config['require_animation'])
            config.update(values)
            config['name'] = text(config['name'],'模板名称',100)
            boolean(config['require_animation'],'要求动画')
            if old and config['kind']!=old['kind']:
                raise ValueError('已有模板不能改变检测类型，请新增或复制模板')
            validate_template(config)
            now = time.time()
            if old:
                revision = old['revision']+int(any(config[k]!=old[k] for k in keys))
                db.execute('UPDATE prompt_templates SET '+','.join(k+'=?' for k in keys)+',revision=?,updated=? WHERE id=?',
                           (*[config[k] for k in keys],revision,now,template_id))
            else:
                template_id = db.execute('INSERT INTO prompt_templates('+','.join(keys)+',created,updated) VALUES('+','.join('?' for _ in range(len(keys)+2))+')',
                                         (*[config[k] for k in keys],now,now)).lastrowid
        self.changed()
        return {'id':template_id}

    def archive_template(self, template_id):
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('''SELECT 1 FROM check_schedules s JOIN model_configs m ON m.id=s.model_id
                JOIN channels c ON c.id=m.channel_id WHERE s.template_id=? AND m.archived=0 AND c.archived=0''',(template_id,)).fetchone():
                raise ValueError('模板仍被模型使用，请先在模型设置中更换模板')
            if not db.execute('UPDATE prompt_templates SET archived=1,updated=? WHERE id=? AND archived=0',(time.time(),template_id)).rowcount:
                raise ValueError('模板不存在或已归档')
        self.changed()
        return {'ok':True}

    def _save_category(self, db, values, category_id=None):
        if not isinstance(values,dict) or set(values)-{'name','sort_order'}:
            raise ValueError('分类字段不正确')
        old = db.execute('SELECT * FROM model_categories WHERE id=?',(category_id,)).fetchone() if category_id else None
        if category_id and not old:
            raise ValueError('分类不存在，请重新选择')
        name = text(values.get('name',old['name'] if old else ''),'分类名称',60)
        key = name.casefold()
        order = integer(values.get('sort_order',old['sort_order'] if old else 0),'分类排序',0,10000)
        if db.execute('SELECT 1 FROM model_categories WHERE name_key=? AND id!=?',(key,category_id or 0)).fetchone():
            raise ValueError('已有同名分类，请选择该分类')
        now = time.time()
        if old:
            db.execute('UPDATE model_categories SET name=?,name_key=?,sort_order=?,updated=? WHERE id=?',
                       (name,key,order,now,category_id))
        else:
            category_id = db.execute('INSERT INTO model_categories(name,name_key,sort_order,created,updated) VALUES(?,?,?,?,?)',
                                     (name,key,order,now,now)).lastrowid
        return {'id':category_id}

    def save_category(self, values, category_id=None):
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result = self._save_category(db,values,category_id)
        self.changed()
        return result

    def delete_category(self, category_id):
        with self.database.connect() as db:
            if not db.execute('DELETE FROM model_categories WHERE id=?',(category_id,)).rowcount:
                raise ValueError('分类不存在')
        self.changed()
        return {'ok':True}

    def models(self):
        result = []
        # channels() returns only masked connection values, never credentials.
        for channel in self.channels():
            for model in channel['models']:
                result.append({**model,**{k:channel[k] for k in ('public','sort_order','base_url_masked','api_key_masked','has_key')},
                               'enabled':bool(model['enabled'] and channel['enabled'])})
        return sorted(result,key=lambda m:(m['sort_order'],m['id']))

    def save_config(self, values, model_id=None):
        """One transaction for connection, category, model and both schedules."""
        extra = {'base_url','api_key','public','sort_order','archived','category_id','category_name','copy_from','schedules'}
        if not isinstance(values,dict) or set(values)-set(MODEL_DEFAULTS)-extra:
            raise ValueError('模型配置字段不正确')
        if model_id and 'copy_from' in values:
            raise ValueError('已有模型不能指定复制来源')
        for key in ('base_url','api_key'):
            if key in values and not isinstance(values[key],str):
                raise ValueError('API 地址和 API Key 必须为文本；留空表示保留')
        if 'category_name' in values:
            text(values['category_name'],'分类名称',60)
        if 'category_id' in values and values.get('category_name'):
            raise ValueError('请选择已有分类或填写新分类')
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source_id = model_id
            if 'copy_from' in values:
                source_id = integer(values['copy_from'],'复制来源',1,2**63-1)
            old = db.execute('''SELECT m.*,c.base_url,c.api_key,c.public,c.sort_order,c.enabled AS connection_enabled
                FROM model_configs m JOIN channels c ON c.id=m.channel_id
                WHERE m.id=? AND m.archived=0 AND c.archived=0''',(source_id,)).fetchone() if source_id else None
            if source_id and not old:
                raise ValueError('模型配置不存在或已归档')
            if old and db.execute('SELECT COUNT(*) FROM model_configs WHERE channel_id=? AND archived=0',(old['channel_id'],)).fetchone()[0]>1:
                raise ValueError('此模型属于多模型卡片，请分别使用卡片设置和模型设置')
            config = {k:old[k] for k in MODEL_DEFAULTS} if old else dict(MODEL_DEFAULTS)
            config['enabled'] = bool(old['enabled'] and old['connection_enabled']) if model_id else False
            config.update({k:v for k,v in values.items() if k in MODEL_DEFAULTS})
            config['display_name'] = text(config['display_name'] or config['model'],'卡片名称',120)
            category_id = values.get('category_id',old['category_id'] if old else None)
            if values.get('category_name'):
                category_id = self._save_category(db,{'name':values['category_name']})['id']
            elif category_id is not None:
                integer(category_id,'分类',1,2**63-1)
                if not db.execute('SELECT 1 FROM model_categories WHERE id=?',(category_id,)).fetchone():
                    raise ValueError('分类不存在，请重新选择')
            connection = {k:values[k] for k in ('base_url','api_key','public','sort_order') if k in values}
            connection.update(title=config['display_name'],enabled=config['enabled'],category_id=category_id)
            if not model_id and old:
                # Duplicate credentials entirely on the server; never send plaintext to the browser.
                for key in ('base_url','api_key','public','sort_order'):
                    if key not in connection or (key in ('base_url','api_key') and not connection[key]):
                        connection[key] = old[key] if key!='public' else bool(old[key])
                if connection['base_url']!=old['base_url'] and not values.get('api_key'):
                    raise ValueError('更换 API 地址时请重新填写该地址的 API Key')
            cid = self._save_channel(db,connection,old['channel_id'] if model_id else None)['id']
            if 'schedules' in values:
                config['schedules'] = values['schedules']
            elif not model_id and old:
                config['schedules'] = {r['kind']:{'enabled':bool(r['enabled']),'interval_minutes':r['interval_minutes'],'template_id':r['template_id']}
                    for r in db.execute('SELECT * FROM check_schedules WHERE model_id=?',(source_id,))}
            elif not model_id:
                config['schedules'] = {'candy':{'enabled':True,'interval_minutes':10},'pelican':{'enabled':False,'interval_minutes':45}}
            if 'archived' in values:
                config['archived'] = values['archived']
            result = self._save_model(db,config,channel_id=cid,model_id=model_id)
            if values.get('archived'):
                db.execute('UPDATE channels SET archived=1 WHERE id=?',(cid,))
            db.execute('UPDATE model_configs SET category_id=? WHERE id=?',(category_id,result['id']))
        self.changed()
        return result

    def channels(self):
        with self.database.connect() as db:
            rows = db.execute('SELECT * FROM channels WHERE archived=0 ORDER BY sort_order,id').fetchall()
            models = db.execute('SELECT * FROM model_configs WHERE archived=0 ORDER BY position,id').fetchall()
            schedules = db.execute('SELECT * FROM check_schedules ORDER BY kind').fetchall()
            tasks = db.execute("SELECT id,model_id,kind,status,stage,created FROM check_runs WHERE status IN ('queued','running')").fetchall()
        groups = {}
        for m in models:
            item = dict(m)
            item.update(enabled=bool(m['enabled']),schedules=[dict(s,enabled=bool(s['enabled'])) for s in schedules if s['model_id']==m['id']],
                        tasks=[dict(t) for t in tasks if t['model_id']==m['id']])
            groups.setdefault(m['channel_id'],[]).append(item)
        result = []
        for c in rows:
            item = {k:c[k] for k in ('id','title','enabled','public','sort_order','category_id','created','updated')}
            item.update(title=redact(item['title'],dict(c)),enabled=bool(c['enabled']),public=bool(c['public']),
                        base_url_masked=mask_url(c['base_url']),api_key_masked=mask(c['api_key']) if c['api_key'] else '尚未配置',
                        has_key=bool(c['api_key']),models=redact_nested(groups.get(c['id'],[]),dict(c)))
            for model in item['models']:
                model.update(card_id=c['id'],category_id=c['category_id'],has_key=bool(c['api_key']),
                             effective_enabled=bool(model['enabled'] and c['enabled']))
            result.append(item)
        return result

    def save_channel(self, values, channel_id=None):
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result = self._save_channel(db,values,channel_id)
        self.changed()
        return result

    def _save_channel(self, db, values, channel_id=None):
        allowed = {'title','base_url','api_key','enabled','public','sort_order','archived','category_id','category_name'}
        if not isinstance(values,dict) or set(values)-allowed:
            raise ValueError('卡片字段不正确')
        now = time.time()
        old = db.execute('SELECT * FROM channels WHERE id=? AND archived=0',(channel_id,)).fetchone() if channel_id else None
        if channel_id and not old:
            raise ValueError('卡片不存在')
        config = dict(old) if old else dict(title='',base_url='',api_key='',enabled=False,public=True,sort_order=0,archived=False,category_id=None)
        if 'category_name' in values and 'category_id' in values:
            raise ValueError('请选择已有分类或填写新分类')
        if 'category_name' in values:
            config['category_id'] = self._save_category(db,{'name':values['category_name']})['id']
        elif 'category_id' in values:
            category = values['category_id']
            if category is not None:
                integer(category,'分类',1,2**63-1)
                if not db.execute('SELECT 1 FROM model_categories WHERE id=?',(category,)).fetchone():
                    raise ValueError('分类不存在')
            config['category_id'] = category
        for key in ('base_url','api_key'):
            if key in values and not isinstance(values[key],str):
                raise ValueError('API 地址和 API Key 必须为文本；留空表示保留')
        if 'title' in values:
            config['title'] = text(values['title'],'卡片标题',120)
        if values.get('base_url'):
            config['base_url'] = normalize_url(values['base_url'])
        if 'api_key' in values and values['api_key']:
            key = text(values['api_key'],'API Key',4096)
            if '****' in key or any(ord(c)<33 or ord(c)>126 for c in key):
                raise ValueError('请填写完整的 API Key')
            config['api_key'] = key
        if old and config['base_url']!=old['base_url'] and not values.get('api_key'):
            raise ValueError('更换 API 地址时请重新填写该地址的 API Key')
        for key in ('enabled','public','archived'):
            if key in values:
                config[key] = boolean(values[key],key)
        if 'sort_order' in values:
            config['sort_order'] = integer(values['sort_order'],'排序',0,10000)
        text(config['title'],'卡片标题',120)
        normalize_url(config['base_url'])
        if config['enabled'] and not config['api_key']:
            raise ValueError('启用模型前请填写 API Key')
        keys = ('title','base_url','api_key','enabled','public','sort_order','archived','category_id')
        if old:
            if config['base_url']!=old['base_url'] or config['api_key']!=old['api_key']:
                self.clean_channel_history(db,channel_id,dict(old))
            db.execute('UPDATE channels SET '+','.join(k+'=?' for k in keys)+',updated=? WHERE id=?',
                       (*[config[k] for k in keys],now,channel_id))
        else:
            channel_id = db.execute('INSERT INTO channels('+','.join(keys)+',created,updated) VALUES('+','.join('?' for _ in range(len(keys)+2))+')',
                                    (*[config[k] for k in keys],now,now)).lastrowid
        if config['archived'] or (not config['enabled'] and (not old or old['enabled'])):
            db.execute("UPDATE check_runs SET status='cancelled',stage='finished',finished=?,error='管理员暂停或归档了卡片' WHERE channel_id=? AND status='queued'",(now,channel_id))
        elif config['enabled'] and (not old or not old['enabled']):
            db.execute('UPDATE check_schedules SET next_run=? WHERE model_id IN (SELECT id FROM model_configs WHERE channel_id=? AND enabled=1 AND archived=0) AND enabled=1',(now,channel_id))
        return {'id':channel_id}

    @staticmethod
    def clean_channel_history(db, channel_id, old_config):
        """Rotating credentials must not expose old values in imported history."""
        fields = ('prompt','nonce','scene','output','error','returned_model','legacy_svg')
        json_fields = ('config','checks','review')
        for run in db.execute('SELECT * FROM check_runs WHERE channel_id=?',(channel_id,)).fetchall():
            values = [redact(run[key],old_config) for key in fields]
            values += [json.dumps(redact_nested(json_object(run[key]),old_config),ensure_ascii=False) for key in json_fields]
            db.execute('UPDATE check_runs SET '+','.join(key+'=?' for key in fields+json_fields)+' WHERE id=?',(*values,run['id']))
        for attempt in db.execute('SELECT id,error,error_code FROM request_attempts WHERE channel_id=?',(channel_id,)).fetchall():
            db.execute('UPDATE request_attempts SET error=?,error_code=? WHERE id=?',
                       (redact(attempt['error'],old_config),redact(attempt['error_code'],old_config),attempt['id']))

    def save_model(self, values, channel_id=None, model_id=None):
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result = self._save_model(db,values,channel_id,model_id)
        self.changed()
        return result

    def save_card_model(self, values, channel_id=None, model_id=None):
        """Child-only writes never change the card's credentials or switches."""
        if not isinstance(values,dict) or set(values)-set(MODEL_DEFAULTS)-{'schedules','archived','copy_from'}:
            raise ValueError('模型字段不正确；连接、分类和公开设置请在卡片中修改')
        if model_id and 'copy_from' in values:
            raise ValueError('已有模型不能指定复制来源')
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            config = {}
            if 'copy_from' in values:
                source_id = integer(values['copy_from'],'复制来源',1,2**63-1)
                old = db.execute('SELECT * FROM model_configs WHERE id=? AND channel_id=? AND archived=0',(source_id,channel_id)).fetchone()
                if not old:
                    raise ValueError('复制来源必须是当前卡片内的可用模型')
                config = {k:old[k] for k in MODEL_DEFAULTS}
                config['schedules'] = {r['kind']:{'enabled':bool(r['enabled']),'interval_minutes':r['interval_minutes'],'template_id':r['template_id']}
                                       for r in db.execute('SELECT * FROM check_schedules WHERE model_id=?',(source_id,))}
            if not model_id:
                config['enabled'] = False
                config.setdefault('schedules',{'candy':{'enabled':True,'interval_minutes':10},'pelican':{'enabled':False,'interval_minutes':45}})
            config.update({k:v for k,v in values.items() if k!='copy_from'})
            result = self._save_model(db,config,channel_id,model_id)
        self.changed()
        return result

    def _save_model(self, db, values, channel_id=None, model_id=None):
        if not isinstance(values,dict) or set(values)-set(MODEL_DEFAULTS)-{'schedules','archived'}:
            raise ValueError('模型字段不正确')
        now = time.time()
        old = db.execute('SELECT * FROM model_configs WHERE id=? AND archived=0',(model_id,)).fetchone() if model_id else None
        if model_id and not old:
            raise ValueError('模型配置不存在')
        channel_id = old['channel_id'] if old else channel_id
        channel = db.execute('SELECT * FROM channels WHERE id=? AND archived=0',(channel_id,)).fetchone()
        if not channel:
            raise ValueError('卡片不存在或已归档')
        config = dict(old) if old else dict(MODEL_DEFAULTS)
        config['enabled'] = bool(config['enabled'])
        config.update({k:v for k,v in values.items() if k in MODEL_DEFAULTS})
        config['model'] = text(config['model'],'模型名称')
        config['display_name'] = text(config['display_name'] or config['model'],'模型显示名称')
        if config['protocol'] not in ('responses','chat') or config['effort'] not in ('low','medium','high','xhigh'):
            raise ValueError('接口协议或思考强度不正确')
        for key,low,high in (('retry_count',0,5),('timeout_seconds',10,600),('first_token_timeout',5,600),
                             ('idle_timeout',5,300),('max_output_tokens',1024,64000)):
            integer(config[key],key,low,high)
        boolean(config['enabled'],'模型启停')
        integer(config['position'],'模型排序',0,10000)
        archived = boolean(values.get('archived',False),'归档')
        keys = tuple(MODEL_DEFAULTS)
        if old:
            db.execute('UPDATE model_configs SET '+','.join(k+'=?' for k in keys)+',archived=?,updated=? WHERE id=?',
                       (*[config[k] for k in keys],archived,now,model_id))
        else:
            model_id = db.execute('INSERT INTO model_configs('+','.join(keys)+',channel_id,created,updated) VALUES('+','.join('?' for _ in range(len(keys)+3))+')',
                                  (*[config[k] for k in keys],channel_id,now,now)).lastrowid
            for kind,interval in (('candy',10),('pelican',45)):
                template = db.execute('SELECT id FROM prompt_templates WHERE kind=? AND archived=0 ORDER BY builtin_key IS NULL,id LIMIT 1',(kind,)).fetchone()
                db.execute('INSERT INTO check_schedules(model_id,kind,interval_minutes,template_id) VALUES(?,?,?,?)',(model_id,kind,interval,template['id'] if template else None))
        if 'schedules' in values:
            schedules = values['schedules']
            if not isinstance(schedules,dict) or set(schedules)-{'candy','pelican'}:
                raise ValueError('检测项目配置不正确')
            for kind, settings in schedules.items():
                if not isinstance(settings,dict) or set(settings)-{'enabled','interval_minutes','template_id'}:
                    raise ValueError('检测项目配置不正确')
                schedule = dict(db.execute('SELECT * FROM check_schedules WHERE model_id=? AND kind=?',(model_id,kind)).fetchone())
                enabled = boolean(settings.get('enabled',bool(schedule['enabled'])),'检测项目启停')
                interval = integer(settings.get('interval_minutes',schedule['interval_minutes']),'检测间隔',1,1440)
                template_id = settings.get('template_id',schedule['template_id'])
                if template_id is not None:
                    integer(template_id,'提示词模板',1,2**63-1)
                    if not db.execute('SELECT 1 FROM prompt_templates WHERE id=? AND kind=? AND archived=0',(template_id,kind)).fetchone():
                        raise ValueError('请选择对应检测类型的可用模板')
                elif enabled:
                    raise ValueError('启用检测前请选择提示词模板')
                due = schedule['next_run']
                if not enabled:
                    due = None
                    if schedule['enabled']:
                        db.execute("UPDATE check_runs SET status='cancelled',stage='finished',finished=?,error='管理员暂停了检测项目' WHERE model_id=? AND kind=? AND status='queued'",(now,model_id,kind))
                elif not schedule['enabled']:
                    due = now
                elif interval!=schedule['interval_minutes']:
                    due = now+interval*60
                db.execute('UPDATE check_schedules SET enabled=?,interval_minutes=?,next_run=?,template_id=? WHERE id=?',(enabled,interval,due,template_id,schedule['id']))
        if archived or (not config['enabled'] and (not old or old['enabled'])):
            db.execute("UPDATE check_runs SET status='cancelled',stage='finished',finished=?,error='管理员暂停了模型' WHERE model_id=? AND status='queued'",(now,model_id))
        elif config['enabled'] and old and not old['enabled']:
            db.execute('UPDATE check_schedules SET next_run=? WHERE model_id=? AND enabled=1',(now,model_id))
        return {'id':model_id}

    def visible(self, db, run_id):
        return db.execute('''SELECT r.*,c.api_key,c.base_url FROM check_runs r JOIN channels c ON c.id=r.channel_id
         JOIN model_configs m ON m.id=r.model_id WHERE r.id=? AND c.public=1 AND c.archived=0 AND m.archived=0''',(run_id,)).fetchone()

    def run(self, run_id, admin=False):
        with self.database.connect() as db:
            row = db.execute('SELECT r.*,c.api_key,c.base_url FROM check_runs r JOIN channels c ON c.id=r.channel_id WHERE r.id=?',(run_id,)).fetchone() if admin else self.visible(db,run_id)
            if not row:
                return None
            attempts = db.execute('SELECT * FROM request_attempts WHERE run_id=? ORDER BY id',(run_id,)).fetchall()
            artifact = db.execute('SELECT id,generated,render_status FROM artifacts WHERE run_id=?',(run_id,)).fetchone()
        result = self.run_summary(row)
        snapshot = json_object(row['config'])
        secret = dict(row)
        result.update(config={k:redact(str(snapshot[k]),secret) for k in ('title','model','display_name','protocol','effort','timeout_seconds','retry_count') if k in snapshot},
                      output=redact(row['output'],secret),prompt=redact(row['prompt'],secret),checks=redact_nested(json_object(row['checks']),secret),
                      review=redact_nested(json_object(row['review']),secret),scene=redact(row['scene'],secret),rule_version=redact(row['rule_version'],secret),
                      attempts=[{k:a[k] for k in ('id','role','attempt','started','finished','status','http_status','first_token_seconds',
                                                'duration_seconds','input_tokens','output_tokens','cached_tokens','error_code','error')} for a in attempts],
                      returned_model=redact(row['returned_model'],secret),artifact=dict(artifact) if artifact else None)
        template = snapshot.get('prompt_template')
        result['template'] = {k:template[k] for k in ('id','name','revision','kind','rule_type','expected_answer','review_criteria','require_animation') if k in template} if isinstance(template,dict) else None
        return redact_nested(result,secret)

    @staticmethod
    def run_summary(row):
        result = {key:row[key] for key in ('id','model_id','channel_id','kind','source','status','created','started','finished','stage','error')}
        result.update({key:row[key] for key in ('template_id','template_revision') if key in row.keys()})
        if 'has_graphic' in row.keys():
            result['has_graphic'] = bool(row['has_graphic'])
        if 'config' in row.keys():
            snapshot = json_object(row['config'])
            result.update(display_name=snapshot.get('display_name') or snapshot.get('model') or '历史模型',
                          model=snapshot.get('model',''),card_title=snapshot.get('title',''))
        if 'api_key' in row.keys():
            result = redact_nested(result,dict(row))
        return result

    def history(self, channel_id=None, model_id=None, kind=None, before=None, limit=50):
        clauses = ['c.public=1','c.archived=0','m.archived=0']
        params = []
        for column,value in (('r.channel_id',channel_id),('r.model_id',model_id),('r.kind',kind),('r.id',before)):
            if value is not None:
                clauses.append(column+(' < ?' if column=='r.id' else ' = ?'))
                params.append(value)
        with self.database.connect() as db:
            rows = db.execute("SELECT r.*,c.api_key,c.base_url,(r.legacy_svg!='' OR EXISTS(SELECT 1 FROM artifacts a WHERE a.run_id=r.id)) AS has_graphic FROM check_runs r JOIN channels c ON c.id=r.channel_id JOIN model_configs m ON m.id=r.model_id WHERE "
                              +' AND '.join(clauses)+' ORDER BY r.id DESC LIMIT ?',(*params,limit+1)).fetchall()
        return {'items':[self.run_summary(r) for r in rows[:limit]],'next_before':rows[limit-1]['id'] if len(rows)>limit else None}

    def tasks(self):
        with self.database.connect() as db:
            return [dict(row) for row in db.execute("SELECT r.id,r.model_id,r.kind,r.status,r.stage,r.created,r.started,m.display_name,c.title FROM check_runs r JOIN model_configs m ON m.id=r.model_id JOIN channels c ON c.id=r.channel_id WHERE r.status IN ('queued','running') ORDER BY r.created")]
