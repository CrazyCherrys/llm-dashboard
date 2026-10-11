"""Browser workflow checks with an isolated fixture server and no model requests.

Run separately from the backend suite: python tests/ui_interactions.py
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

WEB = Path(__file__).resolve().parents[1] / 'web'
NOW = 1791684000
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 240"><rect width="320" height="240" fill="#bdeee8"/><circle cx="160" cy="115" r="60" fill="#009a61"/><text x="160" y="210" text-anchor="middle">Browser fixture</text></svg>'
ANIMATED_SVG = SVG.replace(b'<circle cx="160" cy="115" r="60" fill="#009a61"/>', b'<circle cx="160" cy="115" r="60" fill="#009a61"><animate attributeName="cx" values="70;250;70" dur="2s" repeatCount="indefinite"/></circle>')


def summary(run_id, kind='candy'):
    return dict(id=run_id, channel_id=1, model_id=1, kind=kind, status='passed',
                stage='finished', source='scheduled', created=NOW-run_id,
                started=NOW-run_id, finished=NOW-run_id+2, error='')


def dashboard():
    metrics = dict(attempts=50, successful=48, success_rate=96, first_token_seconds=29.7,
                   timing_samples=48, cache_rate=80, cache_samples=50, cache_coverage=100)
    models = []
    for model_id in range(1, 5):
        models.append(dict(id=model_id, name=f'浏览器测试模型 {model_id}', model=f'fixture-{model_id}',
                           protocol='responses', effort='medium', enabled=True,
                           schedules=[dict(kind=kind, enabled=True, effective_enabled=True,
                                           next_run=NOW+600, interval_minutes=10) for kind in ['candy', 'pelican']],
                           runs=[summary(run_id) for run_id in range(151, 201)],
                           candy_rate=0, passed=0, wrong=50, unavailable=0, pending=0, metrics=metrics.copy()))
    buckets = [dict(start=NOW-(36-i)*600, availability='passed', speed='warm', running=False,
                    attempts=1, successful=1, success_rate=100, first_token_seconds=20,
                    timing_samples=1, cache_rate=80) for i in range(36)]
    images = [dict(id=i, run_id=1000+i, model_id=1 if i < 10 else 2, generated=NOW+i,
                   display_name='浏览器测试模型', status='passed', has_thumbnail=True, render_status='ready')
              for i in range(1, 13)]
    for m in models:
        m.update(title=m['name'],category_id=1 if m['id'] % 2 else 2,
                 timeline=dict(start=NOW-21600,end=NOW,buckets=buckets),
                 images=[dict(i,model_id=1) for i in images] if m['id']==1 else [])
    return dict(server_time=NOW,window_hours=24,refresh_seconds=15,models=models,
                overview_24h=dict(participating_models=4,monitoring_models=4,completed=286,passed=274,wrong=9,failed=3,anomalies=12,ungraded=0,interrupted=0,cancelled=0,other=0),
                categories=[dict(id=i,name=f'自定义系列 {i}',model_count=2) for i in (1,2)],
                definitions={'cache':'缓存率定义'})


def admin_models():
    models = []
    for model_id in range(1, 5):
        models.append(dict(id=model_id, channel_id=1, model=f'fixture-{model_id}',
                           display_name=f'浏览器测试模型 {model_id}', enabled=True, protocol='responses',
                           effort='medium', retry_count=2, timeout_seconds=300, first_token_timeout=120,
                           idle_timeout=60, max_output_tokens=16000, tasks=[],
                           schedules=[dict(kind=kind, enabled=kind == 'candy', interval_minutes=10,
                                           next_run=NOW+600) for kind in ['candy', 'pelican']]))
    for m in models:
        m.update(enabled=False,public=True,sort_order=0,category_id=1 if m['id'] % 2 else 2,
                 base_url_masked='https://fixture.invalid/***',api_key_masked='***',has_key=True)
    return dict(cards=[dict(id=1,title='Fixture card',enabled=True,public=True,sort_order=0,category_id=1,has_key=True,base_url_masked='https://fixture.invalid/***',api_key_masked='***',models=[dict(m,card_id=1,position=0,effective_enabled=False) for m in models])],categories=[dict(id=i,name=f'自定义系列 {i}',sort_order=0,model_count=2) for i in (1,2)],
                templates=[dict(id=i,name=name,kind=kind,body='Fixture prompt',rule_type=rule,expected_answer='21',review_criteria='Fixture criteria',require_animation=True,revision=1,model_count=4) for i,name,kind,rule in [(1,'逻辑模板','candy','number'),(2,'图形模板','pelican','visual')]])


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, value, content_type='application/json', status=200, headers=None):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        files = {'/': 'index.html', '/admin': 'admin.html', **{f'/{name}': name for name in
                 ['app.js', 'admin.js', 'ui.js', 'style.css', 'favicon.svg']}}
        if path in files:
            name = files[path]
            mime = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}[name.split('.')[-1]]
            return self.send((WEB/name).read_bytes(), mime+'; charset=utf-8')
        if path == '/api/auth/status':
            authenticated = 'fixture_session=1' in self.headers.get('Cookie', '')
            return self.send(dict(configured=True, setup_allowed=False, authenticated=authenticated,
                                  expires_at=NOW+7200 if authenticated else None))
        if path == '/api/admin/cards':
            return self.send(admin_models())
        if path == '/api/admin/tasks':
            return self.send([])
        if path == '/api/dashboard':
            value = dashboard()
            value['window_hours'] = int(query.get('hours', ['24'])[0])
            return self.send(value)
        if path.endswith('/history') or path.endswith('/runs'):
            before = int(query.get('before', ['201'])[0])
            items = [summary(i) for i in range(before-1, max(before-51, 100), -1)]
            return self.send(dict(items=items, next_before=items[-1]['id'] if before > 151 else None))
        if path.endswith('/svg'):
            return self.send(ANIMATED_SVG, 'image/svg+xml')
        if path.startswith('/api/artifacts/'):
            return self.send(SVG, 'image/svg+xml')
        if path.startswith('/api/runs/'):
            run_id = int(path.rsplit('/', 1)[-1])
            kind = 'pelican' if run_id >= 1000 else 'candy'
            value = summary(run_id, kind)
            if run_id == 501:
                self.server.run_reads += 1
                if self.server.run_reads < 2:
                    value.update(status='running', stage='requesting', finished=None)
            value.update(config=dict(title='浏览器交互检查', model='fixture-1', protocol='responses', effort='medium'),
                         checks={}, review={}, attempts=[], prompt='Fixture prompt', output='21\n'*80,
                         rule_version='fixture', artifact=dict(generated=NOW) if kind == 'pelican' else None)
            return self.send(value)
        return self.send(dict(error='Not found'), status=404)

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        self.server.writes.append((self.path, value))
        if self.path == '/api/auth/login':
            return self.send(dict(token='browser-fixture-token'), headers={'Set-Cookie': 'fixture_session=1; Path=/; HttpOnly; SameSite=Strict'})
        if self.path == '/api/auth/logout':
            return self.send(dict(ok=True), headers={'Set-Cookie': 'fixture_session=; Path=/; Max-Age=0'})
        time.sleep(self.server.save_delay)
        if self.server.fail_save:
            return self.send(dict(error='模拟保存失败，请重试'), status=400)
        return self.send(dict(id=9))

    do_PATCH = do_POST


class UIWorkflows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        cls.server.writes, cls.server.run_reads = [], 0
        cls.server.save_delay, cls.server.fail_save = 0, False
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.server.writes, self.server.run_reads = [], 0
        self.server.save_delay, self.server.fail_save = 0, False

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def dashboard(self):
        self.page.goto(self.url)
        self.page.wait_for_selector('.channel-card')

    def admin(self):
        self.page.goto(self.url+'/admin')
        self.page.locator('#admin-token').fill('fixture-password')
        self.page.locator('#auth-submit').click()
        self.page.wait_for_selector('#add-model:visible')
        self.page.wait_for_selector('#add-model')
        self.server.writes.clear()

    def test_global_overview_layout_filter_independence_and_missing_data(self):
        self.dashboard()
        completed = self.page.locator('[data-overview="completed"] strong')
        expect(completed).to_have_text('286')
        expect(self.page.locator('.overview-stat')).to_have_count(3)
        expect(self.page.locator('[data-overview="anomalies"] .has-anomalies')).to_be_visible()
        self.page.locator('[data-category-filter="1"]').click()
        expect(completed).to_have_text('286')
        self.page.locator('.dashboard-details > summary').click()
        self.page.locator('#window').select_option('1')
        expect(completed).to_have_text('286')
        expect(self.page.locator('#overview-breakdown')).to_contain_text('通过 274')
        for width in [320,390,768,1440]:
            self.page.set_viewport_size({'width':width,'height':900})
            self.assertFalse(self.page.locator('#logic-overview').evaluate('(el)=>el.scrollWidth > el.clientWidth'))
        self.page.evaluate('render({...data, overview_24h:undefined})')
        expect(completed).to_have_text('—')
        expect(self.page.locator('#overview-breakdown')).to_contain_text('暂不可用')
        self.page.evaluate('render({...data, overview_24h:Object.fromEntries(["participating_models","monitoring_models","completed","passed","wrong","failed","anomalies","ungraded","interrupted","cancelled","other"].map(k=>[k,0]))})')
        expect(completed).to_have_text('0')
        expect(self.page.locator('[data-overview="anomalies"] .has-anomalies')).to_have_count(0)

    def test_hover_preview_and_dismissal(self):
        self.dashboard()
        thumbnail = self.page.locator('.thumbnail').last
        thumbnail.hover()
        expect(self.page.locator('#hover-preview')).to_be_visible()
        box = self.page.locator('#hover-preview').bounding_box()
        anchor = thumbnail.bounding_box()
        self.assertGreaterEqual(box['x'], 12)
        self.assertLessEqual(box['x']+box['width'], 1440-12)
        self.assertLess(box['y']+box['height'], anchor['y'])
        self.page.locator('#hover-preview').hover()
        expect(self.page.locator('#hover-preview')).to_be_visible()
        self.page.mouse.move(1400, 50)
        expect(self.page.locator('#hover-preview')).to_be_hidden()

    def test_hover_plays_only_selected_animation_and_stops_on_leave(self):
        requested = []
        self.page.on('request', lambda req: requested.append(req.url) if req.url.endswith('/svg') else None)
        self.dashboard()
        self.assertEqual(requested, [])
        target = self.page.locator('.thumbnail').last
        target.hover()
        animation = self.page.locator('#hover-preview [data-hover-animation]')
        expect(animation).to_have_attribute('src', '/api/runs/1012/svg')
        self.page.wait_for_function('document.querySelector("[data-hover-animation]")?.naturalWidth > 0')
        before = animation.screenshot()
        self.page.wait_for_timeout(300)
        self.assertNotEqual(before, animation.screenshot())
        expect(target.locator('img')).to_have_attribute('src', '/api/artifacts/12/thumbnail')
        self.page.locator('.thumbnail').nth(10).hover()
        expect(animation).to_have_attribute('src', '/api/runs/1011/svg')
        expect(animation).to_have_count(1)
        self.assertEqual(len(requested), 2)
        self.page.mouse.move(1400, 50)
        expect(self.page.locator('#hover-preview')).to_be_hidden()
        expect(animation).to_have_count(0)

    def test_hover_animation_failure_and_reduced_motion(self):
        self.page.route('**/api/runs/*/svg', lambda route: route.abort())
        self.dashboard()
        target = self.page.locator('.thumbnail').last
        target.hover()
        expect(self.page.locator('.hover-playback-status')).to_contain_text('已恢复静态预览')
        expect(self.page.locator('#hover-preview img')).to_have_attribute('src', '/api/artifacts/12/thumbnail')
        expect(self.page.locator('#hover-preview [data-hover-animation]')).to_have_count(0)
        self.page.emulate_media(reduced_motion='reduce')
        expect(self.page.locator('#hover-preview')).to_be_hidden()
        self.page.mouse.move(1400, 50)
        target.hover()
        expect(self.page.locator('.hover-playback-status')).to_contain_text('减少动态效果')
        expect(self.page.locator('#hover-preview img')).to_have_attribute('src', '/api/artifacts/12/thumbnail')
        self.page.locator('#hover-preview [data-run]').click()
        expect(self.page.locator('#detail-dialog')).to_be_visible()
        expect(self.page.locator('#hover-preview')).to_be_hidden()

    def test_dialog_close_scroll_and_focus(self):
        self.dashboard()
        self.page.locator('.thumbnail').last.click()
        expect(self.page.locator('.preview-image')).to_be_visible()
        self.assertTrue(self.page.evaluate('document.body.classList.contains("modal-open")'))
        self.page.mouse.move(5, 500)
        locked_top = self.page.evaluate('document.body.style.top')
        self.page.mouse.wheel(0, 800)
        self.assertTrue(self.page.evaluate('document.body.classList.contains("modal-open")'))
        self.assertEqual(self.page.evaluate('document.body.style.top'), locked_top)
        self.page.locator('#detail-body').evaluate('(el) => { el.scrollTop = el.scrollHeight; }')
        close = self.page.locator('#close-detail').bounding_box()
        self.assertGreater(close['y'], 0)
        self.assertLess(close['y']+close['height'], 1000)
        self.page.mouse.click(5, 5)
        expect(self.page.locator('#detail-dialog')).not_to_be_visible()
        self.page.wait_for_function('!document.body.classList.contains("modal-open")')
        self.assertFalse(self.page.evaluate('document.body.classList.contains("modal-open")'))
        self.assertAlmostEqual(self.page.evaluate('scrollY'), -float(locked_top.removesuffix('px')), delta=1)
        self.assertEqual(self.page.evaluate('document.activeElement.dataset.run'), '1012')

    def test_form_dirty_guard_and_clean_close(self):
        self.admin()
        self.page.locator('#add-model').click()
        self.page.mouse.click(5, 5)
        expect(self.page.locator('#card-dialog')).not_to_be_visible()
        self.page.locator('#add-model').click()
        self.page.locator('#card-name').fill('未保存的输入')
        self.page.locator('#card-key').fill('fixture-only-key')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('.confirm-dialog')).to_be_visible()
        self.page.locator('[data-continue]').click()
        expect(self.page.locator('.confirm-dialog')).not_to_be_visible()
        expect(self.page.locator('#card-name')).to_have_value('未保存的输入')
        self.page.locator('[data-close="card-dialog"]').last.click()
        self.page.locator('[data-discard]').click()
        expect(self.page.locator('#card-dialog')).not_to_be_visible()
        expect(self.page.locator('#card-key')).to_have_value('')
        self.assertEqual(self.server.writes, [])

    def test_mobile_history_return_and_pagination(self):
        self.context.close()
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.dashboard()
        self.page.locator('[data-model-details]').first.click()
        self.page.locator('[data-model-history]').click()
        expect(self.page.locator('.history-record')).to_have_count(50)
        self.page.locator('#detail-body').evaluate('(el) => { el.scrollTop = 500; }')
        record = self.page.locator('.history-record').nth(8)
        record.click()
        expect(self.page.locator('#detail-back')).to_be_visible()
        self.page.locator('#detail-back').click()
        expect(record).to_be_focused()
        self.assertGreater(self.page.locator('#detail-body').evaluate('(el) => el.scrollTop'), 300)
        self.page.locator('[data-history-more]').click()
        expect(self.page.locator('.history-record').first).to_have_attribute('data-run', '150')
        self.page.locator('[data-history-prev]').click()
        expect(self.page.locator('.history-record').first).to_have_attribute('data-run', '200')
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_mobile_form_controls_and_save_failure(self):
        self.page.set_viewport_size({'width': 320, 'height': 700})
        self.admin()
        self.page.locator('[data-card-add-model]').first.click()
        expect(self.page.locator('#pelican-interval')).to_be_disabled()
        self.page.locator('#pelican-enabled').check()
        expect(self.page.locator('#pelican-interval')).to_be_enabled()
        self.page.locator('#model-name').fill('fixture-model')
        self.page.locator('.advanced-settings > summary').click()
        self.page.locator('#model-form .dialog-scroll').evaluate('(el) => { el.scrollTop = el.scrollHeight; }')
        save = self.page.locator('#model-save').bounding_box()
        close = self.page.locator('[data-close="model-dialog"]').first.bounding_box()
        self.assertGreaterEqual(save['height'], 44)
        self.assertLess(save['y']+save['height'], 700)
        self.assertGreater(close['y'], 0)
        self.server.save_delay, self.server.fail_save = .5, True
        self.page.locator('#model-save').click()
        expect(self.page.locator('#model-save')).to_have_text('保存中…')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#model-dialog')).to_be_visible()
        expect(self.page.locator('#model-error')).to_have_text('模拟保存失败，请重试')
        expect(self.page.locator('#model-name')).to_have_value('fixture-model')
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_refresh_preserves_focus_category_scroll_and_nodes(self):
        self.dashboard()
        self.page.locator('[data-category-filter="1"]').click()
        expect(self.page.locator('.channel-card')).to_have_count(2)
        strip = self.page.locator('.image-strip').first
        strip.evaluate('(el) => { el.scrollLeft = 100; }')
        self.page.locator('[data-model-details="3"]').focus()
        button = self.page.locator('[data-model-details="3"]').element_handle()
        image = self.page.locator('.thumbnail').last.element_handle()
        self.page.evaluate('render({...data, cards:undefined, models:data.models.map(c => ({...c, metrics:{...c.metrics, attempts:51}}))})')
        expect(self.page.locator('[data-model-details="3"]')).to_be_focused()
        expect(self.page.locator('[data-category-filter="1"]')).to_have_attribute('aria-pressed', 'true')
        self.assertAlmostEqual(strip.evaluate('(el) => el.scrollLeft'), 100, delta=1)
        self.assertTrue(button.evaluate('(el) => el.isConnected'))
        self.assertTrue(image.evaluate('(el) => el.isConnected'))

    def test_latest_result_and_countdown_preserve_rate_position(self):
        self.dashboard()
        row = self.page.locator('[data-model="1"] .model-row')
        rate = row.locator('.model-rate')
        before = rate.bounding_box()
        for seconds in [59, 3601, 36001, -1]:
            self.page.evaluate("""(seconds)=>{ document.querySelector('[data-model="1"] [data-due]').dataset.due = (Date.now()+timeOffset)/1000+seconds; updateCountdowns(); }""",seconds)
            after = rate.bounding_box()
            self.assertAlmostEqual(after['x'],before['x'],delta=.5)
            self.assertAlmostEqual(after['width'],before['width'],delta=.5)
        row.locator('.model-latest').click()
        expect(self.page.locator('#detail-dialog')).to_be_visible()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 50 / 50 轮')
        self.page.wait_for_function('currentRunId===200 && currentRunHash')
        self.page.keyboard.press('Escape')
        expect(row.locator('.model-latest')).to_be_focused()
        self.page.set_viewport_size({'width':320,'height':700})
        expect(row.locator('.model-name')).to_be_visible()
        expect(row.locator('.model-latest')).to_be_visible()
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))

    def test_compact_card_details_and_gallery_keyboard_scroll(self):
        self.dashboard()
        expect(self.page.locator('.gallery-controls')).to_have_count(0)
        expect(self.page.locator('[data-card-graphics-history]')).to_have_count(0)
        expect(self.page.locator('.model-caption')).to_have_count(0)
        expect(self.page.locator('.channel-card').first.locator('.segments')).to_have_count(2)
        self.page.locator('[data-model-details="1"]').click()
        expect(self.page.locator('#detail-body')).to_contain_text('Responses / medium')
        expect(self.page.locator('#detail-body')).to_contain_text('最近 50 轮正确率')
        self.page.locator('#detail-body [data-run]').click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 50 / 50 轮')
        self.page.keyboard.press('Escape')
        self.page.locator('[data-card-details]').first.click()
        expect(self.page.locator('#detail-body')).to_contain_text('缓存样本覆盖')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('[data-card-details]').first).to_be_focused()
        slider = self.page.locator('[data-gallery-scroll]').first
        strip = self.page.locator('[data-strip]').first
        slider.fill('0')
        strip.focus()
        self.page.keyboard.press('ArrowRight')
        self.assertGreater(strip.evaluate('(el)=>el.scrollLeft'),0)
        slider.fill('1000')
        self.assertAlmostEqual(strip.evaluate('(el)=>el.scrollWidth-el.clientWidth-el.scrollLeft'),0,delta=2)
        slider.fill('0')
        first = strip.locator('.thumbnail').first
        first.focus()
        self.page.keyboard.press('ArrowLeft')
        expect(first).to_be_focused()
        for index in range(1,12):
            self.page.keyboard.press('ArrowRight')
            expect(strip.locator('.thumbnail').nth(index)).to_be_focused()
        expect(strip.locator('.thumbnail').last).to_be_focused()
        self.assertGreater(strip.evaluate('(el)=>el.scrollLeft'),0)
        self.page.keyboard.press('Enter')
        expect(self.page.locator('.preview-image')).to_be_visible()
        self.page.keyboard.press('Escape')
        expect(strip.locator('.thumbnail').last).to_be_focused()
        self.page.evaluate('render({...data, cards:undefined, models:data.models.map(m=>({...m,metrics:{...m.metrics,attempts:99}}))})')
        expect(strip.locator('.thumbnail').last).to_be_focused()
        self.assertGreater(float(slider.input_value()),0)

    def test_keyboard_gallery_and_live_run(self):
        self.dashboard()
        self.page.locator('.thumbnail').last.click()
        expect(self.page.locator('.preview-navigation > span')).to_have_text('12 / 12')
        self.page.keyboard.press('ArrowLeft')
        expect(self.page.locator('.preview-navigation > span')).to_have_text('11 / 12')
        self.page.keyboard.press('Escape')
        self.page.evaluate('showRun(501)')
        expect(self.page.locator('.detail-refresh-status')).to_contain_text('结果自动更新')
        expect(self.page.locator('.detail-grid')).to_contain_text('通过', timeout=8000)
        self.assertGreaterEqual(self.server.run_reads, 2)

    def test_logic_navigation_boundaries_and_frozen_window(self):
        self.dashboard()
        self.page.locator('[data-model="1"] .candy-cell[data-run]').last.click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 50 / 50 轮')
        expect(self.page.locator('#logic-next')).to_be_disabled()
        self.page.locator('#logic-prev').click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 49 / 50 轮')
        expect(self.page.locator('#logic-prev')).to_be_focused()
        self.page.wait_for_function('currentRunHash && JSON.parse(currentRunHash).id === 199')
        self.page.evaluate('''render({...data, cards:undefined, models:data.models.map(m => m.id === 1 ?
            {...m, runs:[...m.runs.slice(1), {...m.runs.at(-1), id:201}]} : m)})''')
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 49 / 50 轮')
        self.page.keyboard.press('ArrowRight')
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 50 / 50 轮')
        self.assertEqual(self.page.evaluate('currentRunId'), 200)
        expect(self.page.locator('#logic-next')).to_be_disabled()
        self.page.keyboard.press('Escape')
        self.page.locator('[data-model="1"] .candy-cell[data-run]').first.click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 1 / 50 轮')
        expect(self.page.locator('#logic-prev')).to_be_disabled()
        self.page.keyboard.press('ArrowLeft')
        self.assertEqual(self.page.evaluate('currentRunId'), 152)
        self.page.keyboard.press('ArrowRight')
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 2 / 50 轮')

    def test_logic_small_windows_model_isolation_and_mobile_footer(self):
        self.page.set_viewport_size({'width': 320, 'height': 700})
        self.dashboard()
        self.page.evaluate('''render({...data, cards:undefined, models:data.models.map(m => ({...m,
            runs:m.id === 1 ? m.runs.slice(-21) : m.id === 2 ?
              [{...m.runs[0], id:701, model_id:2}] : []}))})''')
        self.page.locator('[data-model="1"] .candy-cell[data-run]').last.click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 21 / 21 轮')
        expect(self.page.locator('#detail-body pre').first).to_contain_text('Fixture prompt')
        before = self.page.locator('#logic-navigation').bounding_box()
        self.page.locator('#detail-body').evaluate('(el) => { el.scrollTop = el.scrollHeight; }')
        after = self.page.locator('#logic-navigation').bounding_box()
        self.assertAlmostEqual(before['y'], after['y'], delta=1)
        self.assertLessEqual(after['y'] + after['height'], 700)
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.page.locator('#logic-prev').click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 20 / 21 轮')
        self.assertEqual(self.page.locator('#detail-body').evaluate('(el) => el.scrollTop'), 0)
        self.page.locator('#close-detail').click()
        self.page.locator('[data-model="2"] .candy-cell[data-run]').last.click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 1 / 1 轮')
        expect(self.page.locator('#logic-prev')).to_be_disabled()
        expect(self.page.locator('#logic-next')).to_be_disabled()
        self.assertEqual(self.page.evaluate('currentRunId'), 701)
        self.page.locator('#close-detail').click()
        expect(self.page.locator('[data-model="3"] [data-run]')).to_have_count(0)
        expect(self.page.locator('#logic-navigation')).not_to_be_visible()

    def test_logic_navigation_ignores_stale_response_and_recovers_errors(self):
        held = []
        self.page.route('**/api/runs/199', lambda route: held.append(route))
        self.dashboard()
        self.page.locator('[data-model="1"] .candy-cell[data-run]').last.click()
        with self.page.expect_request('**/api/runs/199'):
            self.page.locator('#logic-prev').click()
        self.page.locator('#logic-prev').click()
        self.page.wait_for_function('currentRunHash && JSON.parse(currentRunHash).id === 198')
        stale = dict(**summary(199), config={}, checks={}, review={}, attempts=[],
                     prompt='过期结果', output='过期结果', rule_version='fixture')
        with self.page.expect_request_finished(lambda request: request.url.endswith('/api/runs/199')):
            held[0].fulfill(json=stale)
        expect(self.page.locator('#detail-body')).not_to_contain_text('过期结果')
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 48 / 50 轮')
        self.page.unroute('**/api/runs/199')
        self.page.route('**/api/runs/197', lambda route: route.fulfill(status=503, json={'error':'模拟读取失败'}))
        self.page.locator('#logic-prev').click()
        expect(self.page.locator('#detail-body')).to_contain_text('模拟读取失败')
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 47 / 50 轮')
        expect(self.page.locator('#logic-next')).to_be_enabled()
        self.page.unroute('**/api/runs/197')
        self.page.locator('#detail-body [data-run="197"]').click()
        expect(self.page.locator('#detail-body pre').first).to_contain_text('Fixture prompt')
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 47 / 50 轮')

    def test_logic_history_navigation_and_live_result(self):
        self.dashboard()
        self.page.locator('[data-model-details="1"]').click()
        self.page.locator('[data-model-history="1"]').click()
        self.page.locator('[data-history-more]').click()
        self.page.locator('.history-record').nth(8).click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 42 / 50 轮')
        self.page.locator('#logic-prev').click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 41 / 50 轮')
        self.page.locator('#detail-back').click()
        expect(self.page.locator('.history-record').nth(8)).to_be_focused()
        expect(self.page.locator('.history-record').first).to_have_attribute('data-run', '150')
        expect(self.page.locator('#logic-navigation')).not_to_be_visible()
        self.page.keyboard.press('Escape')
        self.page.evaluate('''render({...data, cards:undefined, models:data.models.map(m => m.id === 1 ?
            {...m, runs:[m.runs.at(-1), {...m.runs.at(-1), id:501, status:'running', stage:'requesting'}]} : m)})''')
        self.page.locator('[data-model="1"] .candy-cell[data-run]').last.click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 2 / 2 轮')
        expect(self.page.locator('.detail-refresh-status')).to_contain_text('结果自动更新')
        expect(self.page.locator('.detail-grid')).to_contain_text('通过', timeout=8000)
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 2 / 2 轮')

    def test_hover_handles_top_edge_and_returns_to_thumbnail(self):
        self.dashboard()
        self.page.locator('.thumbnail').last.scroll_into_view_if_needed()
        self.page.evaluate('document.body.style.paddingBottom = "1200px"')
        self.page.evaluate('scrollTo(0, document.querySelector(".thumbnail").getBoundingClientRect().top + scrollY - 20)')
        thumbnail = self.page.locator('.thumbnail').last
        thumbnail.hover()
        expect(self.page.locator('#hover-preview')).to_be_visible()
        box = self.page.locator('#hover-preview').bounding_box()
        self.assertGreater(box['y'], thumbnail.bounding_box()['y'])
        self.page.locator('#hover-preview [data-run]').click()
        expect(self.page.locator('.preview-image')).to_be_visible()
        self.page.keyboard.press('Escape')
        expect(thumbnail).to_be_focused()

    def test_save_succeeds_without_discard_prompt_and_reports_feedback(self):
        self.admin()
        self.page.locator('[data-card-add-model]').first.click()
        self.page.locator('#model-name').fill('fixture-model')
        self.page.locator('#model-save').click()
        expect(self.page.locator('#model-dialog')).not_to_be_visible()
        expect(self.page.locator('.confirm-dialog')).to_have_count(0)
        expect(self.page.locator('#admin-toast')).to_contain_text('已保存')
        writes = [body for path, body in self.server.writes if path.endswith('/models')]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['model'], 'fixture-model')
        self.assertFalse(writes[0]['schedules']['pelican']['enabled'])

    def test_changed_model_form_survives_confirm_escape_and_backdrop(self):
        self.admin()
        self.page.locator('[data-model-edit]').first.click()
        self.page.locator('#model-display').fill('保留这次修改')
        self.page.mouse.click(5, 5)
        expect(self.page.locator('.confirm-dialog')).to_be_visible()
        self.page.keyboard.press('Escape')
        expect(self.page.locator('.confirm-dialog')).to_have_count(0)
        expect(self.page.locator('#model-dialog')).to_be_visible()
        expect(self.page.locator('#model-display')).to_have_value('保留这次修改')
        self.page.mouse.click(5, 5)
        expect(self.page.locator('.confirm-dialog')).to_be_visible()
        self.page.mouse.click(5, 5)
        expect(self.page.locator('.confirm-dialog')).to_have_count(0)
        expect(self.page.locator('#model-dialog')).to_be_visible()
        self.assertEqual(self.server.writes, [])

    def test_mobile_has_no_hover_and_swipe_changes_image(self):
        self.context.close()
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.dashboard()
        self.page.locator('.thumbnail').last.tap()
        expect(self.page.locator('#hover-preview')).to_be_hidden()
        expect(self.page.locator('.preview-navigation > span')).to_have_text('12 / 12')
        session = self.context.new_cdp_session(self.page)
        box = self.page.locator('.preview-image').bounding_box()
        x, y = box['x']+70, box['y']+box['height']/2
        session.send('Input.dispatchTouchEvent', dict(type='touchStart', touchPoints=[dict(x=x, y=y)]))
        session.send('Input.dispatchTouchEvent', dict(type='touchMove', touchPoints=[dict(x=x+100, y=y)]))
        session.send('Input.dispatchTouchEvent', dict(type='touchEnd', touchPoints=[]))
        expect(self.page.locator('.preview-navigation > span')).to_have_text('11 / 12')

    def test_admin_refresh_keeps_keyboard_focus(self):
        self.admin()
        button = self.page.locator('[data-model-edit="4"]')
        button.focus()
        original = button.element_handle()
        self.page.evaluate('render()')
        expect(button).to_be_focused()
        self.assertTrue(original.evaluate('(el) => el.isConnected'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
