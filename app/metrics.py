"""Public dashboard aggregation. Primary attempts and benchmark results stay separate."""
from collections import defaultdict
import math
import threading
import time
from checks import redact, redact_nested, json_object


def aggregate(rows):
    ended = [r for r in rows if r['status'] not in ('running','queued')]
    success = [r for r in ended if r['status']=='success']
    measured = [r['first_token_seconds'] for r in success if type(r['first_token_seconds']) in (int,float)
                and math.isfinite(r['first_token_seconds']) and r['first_token_seconds']>=0]
    cached = [r for r in ended if type(r['input_tokens']) is int and type(r['cached_tokens']) is int
              and 0<=r['cached_tokens']<=r['input_tokens']]
    inputs = sum(r['input_tokens'] for r in cached)
    return {'attempts':len(ended),'successful':len(success),
            'success_rate':100*len(success)/len(ended) if ended else None,
            'first_token_seconds':sum(measured)/len(measured) if measured else None,
            'timing_samples':len(measured),'cache_rate':100*sum(r['cached_tokens'] for r in cached)/inputs if inputs else None,
            'cache_samples':len(cached),'cache_coverage':100*len(cached)/len(ended) if ended else None}


def speed(seconds):
    if seconds is None:
        return 'empty'
    return 'fast' if seconds<10 else 'warm' if seconds<30 else 'slow' if seconds<50 else 'late'


def timeline(rows, now):
    step = 600
    end = int(now//step)*step
    start = end-35*step
    buckets = [[] for _ in range(36)]
    for row in rows:
        index = int((row['started']-start)//step)
        if 0<=index<36:
            buckets[index].append(row)
    result = []
    for i, group in enumerate(buckets):
        stats = aggregate(group)
        running = any(r['status']=='running' for r in group)
        availability = 'empty'
        if stats['attempts']:
            availability = 'passed' if stats['successful']==stats['attempts'] else 'failed' if not stats['successful'] else 'mixed'
        result.append(dict(start=start+i*step,**stats,availability=availability,speed=speed(stats['first_token_seconds']),running=running))
    return {'start':start,'end':end+step,'step_seconds':step,'buckets':result}


class Metrics:
    def __init__(self, database):
        self.database = database
        self.lock = threading.Lock()
        self.cache = {}
        self.generation = 0

    def invalidate(self):
        with self.lock:
            self.cache.clear()
            self.generation += 1

    def dashboard(self, hours=24):
        with self.lock:
            generation = self.generation
            entry = self.cache.get(hours)
            if entry and time.monotonic()-entry[0]<5:
                return entry[1]
        now = time.time()
        with self.database.connect() as db:
            # All projections must reflect one coherent configuration snapshot.
            db.execute('BEGIN')
            channels = db.execute('SELECT * FROM channels WHERE public=1 AND archived=0 ORDER BY sort_order,id').fetchall()
            models = db.execute('SELECT m.* FROM model_configs m JOIN channels c ON c.id=m.channel_id WHERE c.public=1 AND c.archived=0 AND m.archived=0 ORDER BY m.position,m.id').fetchall()
            schedules = db.execute('''SELECT s.*,t.name AS template_name,t.revision AS template_revision
                FROM check_schedules s LEFT JOIN prompt_templates t ON t.id=s.template_id''').fetchall()
            categories = db.execute('SELECT id,name,sort_order FROM model_categories ORDER BY sort_order,id').fetchall()
            summary = dict(db.execute('''WITH recent AS (
                SELECT r.model_id,r.status,
                  (r.finished BETWEEN :start AND :end AND r.status NOT IN ('running','queued')) AS completed
                FROM check_runs r JOIN model_configs m ON m.id=r.model_id JOIN channels c ON c.id=m.channel_id
                WHERE r.kind='candy' AND c.public=1 AND c.archived=0 AND m.archived=0
                  AND (r.created BETWEEN :start AND :end OR r.started BETWEEN :start AND :end
                       OR r.finished BETWEEN :start AND :end))
                SELECT COUNT(DISTINCT model_id) AS participating_models,SUM(completed) AS completed,
                  SUM(completed AND status='passed') AS passed,SUM(completed AND status='invalid') AS wrong,
                  SUM(completed AND status='error') AS failed,SUM(completed AND status='ungraded') AS ungraded,
                  SUM(completed AND status='interrupted') AS interrupted,SUM(completed AND status='cancelled') AS cancelled,
                  SUM(completed AND status NOT IN ('passed','invalid','error','ungraded','interrupted','cancelled')) AS other
                FROM recent''',{'start':now-86400,'end':now}).fetchone())
            summary = {key: value or 0 for key,value in summary.items()}
            summary['anomalies'] = summary['wrong'] + summary['failed']
            overview = {r['model_id']:dict(r) for r in db.execute('''SELECT r.model_id,
                SUM(r.status='passed') AS passed,SUM(r.status='invalid') AS wrong,
                SUM(r.status='ungraded') AS ungraded,MAX(r.finished) AS updated,
                COUNT(DISTINCT CASE WHEN r.status IN ('passed','invalid') THEN
                  COALESCE(r.template_id || ':' || r.template_revision,'legacy') END) AS template_count
                FROM check_runs r JOIN model_configs m ON m.id=r.model_id JOIN channels c ON c.id=m.channel_id
                WHERE r.kind='candy' AND r.finished>=? AND r.finished<=? AND c.public=1 AND c.archived=0 AND m.archived=0
                GROUP BY r.model_id''',(now-86400,now))}
            attempts = db.execute('''SELECT a.* FROM request_attempts a JOIN channels c ON c.id=a.channel_id
              JOIN model_configs m ON m.id=a.model_id WHERE c.public=1 AND c.archived=0 AND m.archived=0
              AND a.role='primary' AND a.started>=?''',(now-max(hours,6)*3600,)).fetchall()
            runs = db.execute('''SELECT * FROM (SELECT r.id,r.model_id,r.channel_id,r.kind,r.status,r.created,r.started,r.finished,
                r.stage,r.error,r.source,r.template_id,r.template_revision,ROW_NUMBER() OVER(PARTITION BY r.model_id,r.kind ORDER BY r.id DESC) AS position
                FROM check_runs r JOIN channels c ON c.id=r.channel_id JOIN model_configs m ON m.id=r.model_id
                WHERE c.public=1 AND c.archived=0 AND m.archived=0) WHERE position<=50 ORDER BY id''').fetchall()
            artifacts = db.execute('''SELECT * FROM (SELECT a.id,a.run_id,a.generated,a.render_status,a.thumbnail_path IS NOT NULL AS has_thumbnail,
                r.channel_id,r.model_id,r.status,r.config,m.display_name,ROW_NUMBER() OVER(PARTITION BY r.model_id ORDER BY a.generated DESC,r.id DESC) AS position
                FROM artifacts a JOIN check_runs r ON r.id=a.run_id JOIN channels c ON c.id=r.channel_id
                JOIN model_configs m ON m.id=r.model_id WHERE c.public=1 AND c.archived=0 AND m.archived=0)
                WHERE position<=50 ORDER BY generated''').fetchall()
            legacy = db.execute("""SELECT * FROM (SELECT r.id AS run_id,r.channel_id,r.model_id,r.status,
                COALESCE(r.finished,r.created) AS generated,r.config,m.display_name,
                ROW_NUMBER() OVER(PARTITION BY r.model_id ORDER BY COALESCE(r.finished,r.created) DESC,r.id DESC) AS position
                FROM check_runs r JOIN channels c ON c.id=r.channel_id JOIN model_configs m ON m.id=r.model_id
                WHERE r.legacy_svg!='' AND c.public=1 AND c.archived=0 AND m.archived=0
                AND NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.run_id=r.id)) WHERE position<=50""").fetchall()
        by_channel = defaultdict(list)
        by_model = defaultdict(list)
        runs_by_model = defaultdict(list)
        schedules_by_model = defaultdict(list)
        for attempt in attempts:
            by_channel[attempt['channel_id']].append(attempt)
        for model in models:
            by_model[model['channel_id']].append(model)
        for run in runs:
            runs_by_model[run['model_id']].append(dict(run))
        for schedule in schedules:
            schedules_by_model[schedule['model_id']].append(schedule)
        cards,flat_models = [],[]
        for channel in channels:
            cid = channel['id']
            if not by_model[cid]:
                continue
            samples = by_channel[cid]
            stats = aggregate([r for r in samples if r['started']>=now-hours*3600])
            model_cards = []
            for model in by_model[cid]:
                mid = model['id']
                model_runs = redact_nested(runs_by_model[mid],dict(channel))
                candy = [r for r in model_runs if r['kind']=='candy']
                passed = sum(r['status']=='passed' for r in candy)
                wrong = sum(r['status']=='invalid' for r in candy)
                schedule = [{**dict(s),'enabled':bool(s['enabled']),
                             'effective_enabled':bool(s['enabled'] and model['enabled'] and channel['enabled'])} for s in schedules_by_model[mid]]
                ms = aggregate([r for r in samples if r['model_id']==mid and r['started']>=now-hours*3600])
                logic = overview.get(mid,dict(passed=0,wrong=0,ungraded=0,updated=None,template_count=0))
                logic['graded'] = logic['passed']+logic['wrong']
                logic['rate'] = 100*logic['passed']/logic['graded'] if logic['graded'] else None
                model_cards.append({'id':mid,'name':redact(model['display_name'],dict(channel)),'model':redact(model['model'],dict(channel)),
                                    'category_id':channel['category_id'],'position':model['position'],'enabled':bool(model['enabled'] and channel['enabled']),'protocol':model['protocol'],'effort':model['effort'],
                                    'schedules':schedule,'runs':model_runs,'candy_rate':100*passed/(passed+wrong) if passed+wrong else None,
                                    'passed':passed,'wrong':wrong,'unavailable':sum(r['status'] in ('error','interrupted','cancelled','legacy') for r in candy),
                                    'ungraded':sum(r['status']=='ungraded' for r in candy),'logic_24h':logic,
                                    'pending':sum(r['status'] in ('queued','running') for r in candy),'metrics':ms})
            images = [{**{k:r[k] for k in ('id','run_id','generated','render_status','has_thumbnail','model_id','status')},
                       'display_name':json_object(r['config']).get('display_name') or r['display_name']} for r in artifacts if r['channel_id']==cid]
            images += [dict(id=None,run_id=r['run_id'],generated=r['generated'],render_status='legacy',has_thumbnail=False,
                            model_id=r['model_id'],status=r['status'],display_name=json_object(r['config']).get('display_name') or r['display_name']) for r in legacy if r['channel_id']==cid]
            images.sort(key=lambda i:(i['generated'],i['run_id']))
            for image in images:
                image['display_name'] = redact(image['display_name'],dict(channel))
            for model_card in model_cards:
                mid = model_card['id']
                model_card.update(title=model_card['name'],card_id=cid,card_title=redact(channel['title'],dict(channel)))
                flat_models.append(redact_nested({**model_card,'timeline':timeline([r for r in samples if r['model_id']==mid],now),
                                                 'images':[i for i in images if i['model_id']==mid][-50:]},dict(channel)))
            cards.append(redact_nested({'id':cid,'title':channel['title'],'category_id':channel['category_id'],
                          'enabled':bool(channel['enabled']),'active_models':sum(m['enabled'] for m in model_cards),
                          'models':model_cards,'metrics':stats,'timeline':timeline(samples,now),'images':images[-50:]},dict(channel)))
        summary['monitoring_models'] = sum(any(s['kind']=='candy' and s['effective_enabled'] for s in m['schedules']) for m in flat_models)
        result = {'server_time':now,'window_hours':hours,'timeline_hours':6,'refresh_seconds':15,'cards':cards,'models':flat_models,
                  'overview_24h':summary,
                  'overview_start':now-86400,'overview_end':now,
                  'categories':[{**dict(c),'card_count':sum(card['category_id']==c['id'] for card in cards),
                                 'model_count':sum(m['category_id']==c['id'] for m in flat_models)}
                                for c in categories if any(card['category_id']==c['id'] for card in cards)],
                  'definitions':{'cache':'缓存输入 Token / 对应样本输入 Token；缺失不按 0 计',
                                 'success':'主检测成功请求尝试 / 已结束请求尝试，包含重试，不包含视觉审核',
                                 'first_token':'成功请求首次有效内容返回的平均时间；非流式响应不提供',
                                 'candy':'各模型最近 50 轮已评分回答的通过率；请求失败和未评分不计入'}}
        with self.lock:
            if generation == self.generation:
                self.cache[hours] = (time.monotonic(),result)
        return result
