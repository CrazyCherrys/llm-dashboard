"""Real-server browser checks for model/category management, without running workers."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Application, Handler, Server
from playwright.sync_api import sync_playwright, expect


class ModelManagementBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True,args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name)
        self.app.auth.setup('isolated-browser-password')
        self.server = Server(('127.0.0.1',0),Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.context = self.browser.new_context(viewport={'width':1440,'height':1000})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
        self.assertEqual(self.errors,[])

    def login(self):
        self.page.goto(self.url+'/admin')
        self.page.locator('#admin-token').fill('isolated-browser-password')
        self.page.locator('#auth-submit').click()
        expect(self.page.locator('#add-model')).to_be_visible()
        expect(self.page.locator('#model-count')).to_contain_text('张卡片')

    def screenshot(self, name):
        directory = os.environ.get('UI_ARTIFACT_DIR')
        if directory:
            Path(directory).mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(Path(directory)/name),full_page=True)

    def create_card(self, title='CodeX-Pro', category=None):
        self.page.locator('#add-model').click()
        self.page.locator('#card-name').fill(title)
        self.page.locator('#card-url').fill('https://fixture.invalid/v1')
        self.page.locator('#card-key').fill('shared-isolated-key')
        if category:
            self.page.locator('#card-category').select_option('new')
            self.page.locator('#card-category-name').fill(category)
        self.page.locator('#card-save').click()
        expect(self.page.locator('#model-dialog')).to_be_visible()

    def test_create_inline_category_copy_filter_and_session_restore(self):
        self.login()
        self.create_card(category='我的 GPT 系列')
        self.assertEqual(self.app.metrics.dashboard()['cards'],[])
        self.page.locator('#model-name').fill('gpt-custom')
        self.page.locator('#model-display').fill('主模型')
        self.page.locator('#model-save').click()
        expect(self.page.locator('#model-dialog')).not_to_be_visible()
        for name in ['备用模型','第三个模型']:
            self.page.locator('[data-model-copy]').first.click()
            expect(self.page.locator('#model-enabled')).not_to_be_checked()
            self.page.locator('#model-display').fill(name)
            self.page.locator('#model-save').click()
            expect(self.page.locator('#model-dialog')).not_to_be_visible()
        expect(self.page.locator('[data-card]')).to_have_count(1)
        expect(self.page.locator('[data-model-edit]')).to_have_count(3)
        self.assertTrue(all(not m['enabled'] for m in self.app.repository.channels()[0]['models']))
        self.create_card('第二张卡片')
        self.page.locator('#model-name').fill('other-model')
        self.page.locator('#model-save').click()
        expect(self.page.locator('#model-dialog')).not_to_be_visible()
        self.page.reload()
        expect(self.page.locator('[data-model-edit]')).to_have_count(4)
        expect(self.page.locator('#login-panel')).not_to_be_visible()
        with self.app.database.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0],2)
        self.screenshot('models-admin-desktop.png')
        self.page.goto(self.url)
        expect(self.page.locator('.channel-card')).to_have_count(2)
        expect(self.page.locator('.channel-card').first.locator('.model-block')).to_have_count(3)
        self.screenshot('models-dashboard-desktop.png')
        self.page.locator('[data-category-filter="1"]').click()
        expect(self.page.locator('.channel-card')).to_have_count(1)
        expect(self.page.locator('.channel-card')).to_contain_text('主模型')
        expect(self.page.locator('.channel-card')).to_contain_text('第三个模型')
        self.page.locator('#refresh').click()
        expect(self.page.locator('[data-category-filter="1"]')).to_have_attribute('aria-pressed','true')
        self.page.locator('[data-category-filter="all"]').click()
        expect(self.page.locator('.channel-card')).to_have_count(2)
        self.page.set_viewport_size({'width':390,'height':844})
        self.screenshot('models-dashboard-mobile.png')
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.page.goto(self.url+'/admin')
        expect(self.page.locator('[data-model-edit]')).to_have_count(4)
        self.assertEqual(self.app.repository.tasks(),[])

    def test_mixed_graphics_and_independent_logic_navigation(self):
        import json, time
        repo = self.app.repository
        cid = repo.save_channel(dict(title='CodeX-Pro',base_url='https://fixture.invalid/v1',api_key='fixture-key',enabled=True))['id']
        mids = [repo.save_card_model(dict(model=f'model-{i}',display_name=f'模型 {i}',enabled=i!=2),channel_id=cid)['id'] for i in range(1,4)]
        now = time.time()-1000
        with self.app.database.connect() as db:
            for i in range(60):
                mid = mids[i%3]
                snapshot=json.dumps(dict(title='CodeX-Pro',display_name=f'模型 {i%3+1}',model=f'model-{i%3+1}'))
                for kind in ['candy','pelican']:
                    db.execute("""INSERT INTO check_runs(model_id,channel_id,kind,status,source,created,finished,config,legacy_svg)
                       VALUES(?,?,?,'passed','legacy',?,?,?,?)""",(mid,cid,kind,now+i,now+i,snapshot,'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="skyblue"/></svg>' if kind=='pelican' else ''))
        self.page.goto(self.url)
        expect(self.page.locator('.channel-card')).to_have_count(1)
        expect(self.page.locator('.model-block')).to_have_count(3)
        expect(self.page.locator('.thumbnail')).to_have_count(50)
        self.page.locator('.thumbnail').last.click()
        expect(self.page.locator('#detail-title')).to_contain_text('CodeX-Pro / 模型 3')
        expect(self.page.locator('.preview-image')).to_be_visible()
        self.page.keyboard.press('ArrowLeft')
        expect(self.page.locator('#detail-title')).to_contain_text('CodeX-Pro / 模型 2')
        self.screenshot('multi-model-graphic-detail.png')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('[data-card-graphics-history]')).to_have_count(0)
        self.page.evaluate('(cid)=>showHistory(cid, \'\', \'pelican\', null, \'card\')',cid)
        expect(self.page.locator('.history-record')).to_have_count(50)
        self.page.locator('[data-history-more]').click()
        expect(self.page.locator('.history-record')).to_have_count(10)
        self.page.locator('.history-record').first.click()
        expect(self.page.locator('.preview-navigation > span')).to_have_text('1 / 10')
        self.page.keyboard.press('ArrowRight')
        expect(self.page.locator('.preview-navigation > span')).to_have_text('2 / 10')
        expect(self.page.locator('#detail-title')).to_contain_text('模型 3')
        self.page.keyboard.press('Escape')
        self.page.locator('.model-block').first.locator('.candy-cell[data-run]').last.click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 20 / 20 轮')
        self.page.locator('#logic-prev').click()
        expect(self.page.locator('#logic-position')).to_have_text('窗口内 19 / 20 轮')
        expect(self.page.locator('#detail-title')).to_contain_text('模型 1')
        self.page.keyboard.press('Escape')
        self.screenshot('multi-model-results-desktop.png')
        self.page.set_viewport_size({'width':390,'height':844})
        self.screenshot('multi-model-results-mobile.png')
        self.assertTrue(self.page.locator('.model-row').evaluate_all('''rows => rows.every(row => {
            const name = row.querySelector('.model-name').getBoundingClientRect();
            const template = row.querySelector('.model-template').getBoundingClientRect();
            const countdown = row.querySelector('.countdown').getBoundingClientRect();
            return name.width > 15 && template.width > 15 && Math.abs(row.querySelector('.model-rate').getBoundingClientRect().top + row.querySelector('.model-rate').getBoundingClientRect().height / 2 - countdown.top - countdown.height / 2) < 16;
        })'''))
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.login()
        self.page.locator('[data-card-toggle]').click()
        expect(self.page.locator('[data-card-toggle]')).to_have_text('恢复整卡')
        self.assertEqual([m['enabled'] for m in repo.channels()[0]['models']],[True,False,True])
        self.page.locator('[data-card-toggle]').click()
        expect(self.page.locator('[data-card-toggle]')).to_have_text('暂停整卡')
        self.assertEqual([m['enabled'] for m in repo.channels()[0]['models']],[True,False,True])
        self.screenshot('multi-model-admin-mobile.png')

    def test_category_rename_duplicate_validation_and_delete_preserve_history(self):
        mid = self.app.repository.save_config(dict(model='custom',display_name='保留记录',base_url='https://fixture.invalid/v1',api_key='test-key',category_name='自定义系列'))['id']
        rid = self.app.scheduler.enqueue(mid,['candy'])[0]
        self.login()
        self.page.locator('#manage-categories').click()
        expect(self.page.locator('.category-row')).to_have_count(1)
        self.page.locator('[data-category-edit]').click()
        self.page.locator('#category-name').fill('重命名后的系列')
        self.page.locator('#category-order').fill('9')
        self.page.locator('#category-save').click()
        expect(self.page.locator('#category-dialog')).not_to_be_visible()
        expect(self.page.locator('#model-list')).to_contain_text('重命名后的系列')
        self.page.locator('#manage-categories').click()
        self.page.locator('#category-name').fill(' 重命名后的系列 ')
        self.page.locator('#category-save').click()
        expect(self.page.locator('#category-error')).to_contain_text('同名分类')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('.confirm-dialog')).to_be_visible()
        self.page.locator('[data-discard]').click()
        self.page.locator('#manage-categories').click()
        self.page.once('dialog',lambda dialog:dialog.accept())
        self.page.locator('[data-category-delete]').click()
        expect(self.page.locator('.category-row')).to_have_count(0)
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#model-list')).to_contain_text('未分类')
        self.assertEqual(self.app.repository.run(rid)['model_id'],mid)
        self.assertEqual(self.app.repository.run(rid)['status'],'queued')
        self.assertEqual(self.app.repository.models()[0]['id'],mid)

    def test_prompt_templates_selection_versions_and_compact_overview(self):
        self.login()
        self.page.locator('#manage-templates').click()
        expect(self.page.locator('#template-list .category-row')).to_have_count(2)
        self.page.locator('#template-name').fill('七的测试')
        self.page.locator('#template-body').fill('只回答数字 7')
        self.page.locator('#template-answer').fill('7')
        self.page.locator('#template-save').click()
        expect(self.page.locator('#template-dialog')).not_to_be_visible()
        logic = next(t for t in self.app.repository.templates() if t['name']=='七的测试')
        self.page.locator('#manage-templates').click()
        self.page.locator('#template-kind').select_option('pelican')
        self.page.locator('#template-name').fill('静态小猫')
        self.page.locator('#template-body').fill('绘制小猫，允许 CSS .cat { fill: red; }')
        self.page.locator('#template-criteria').fill('画面有一只清晰的小猫和两只耳朵。')
        self.page.locator('#template-animation').uncheck()
        self.page.locator('#template-save').click()
        expect(self.page.locator('#template-dialog')).not_to_be_visible()
        graphic = next(t for t in self.app.repository.templates() if t['name']=='静态小猫')
        self.create_card()
        self.page.locator('#model-name').fill('test-model')
        self.page.locator('#model-display').fill('模板试用模型')
        self.page.locator('#candy-template').select_option(str(logic['id']))
        self.page.locator('#pelican-template').select_option(str(graphic['id']))
        self.page.locator('#candy-template').locator('..').locator('..').locator('.template-preview > summary').click()
        expect(self.page.locator('#candy-template-preview')).to_have_text('只回答数字 7')
        self.screenshot('template-selection.png')
        self.page.locator('#model-save').click()
        expect(self.page.locator('#model-dialog')).not_to_be_visible()
        mid = self.app.repository.models()[0]['id']
        import time
        self.app.scheduler.model_call = lambda config,prompt,on_first=None:('7',dict(status='success',finished=time.time()),'test-model')
        rid = self.app.scheduler.enqueue(mid,['candy'])[0]
        self.app.scheduler.execute(*self.app.scheduler.claim())
        self.page.goto(self.url)
        expect(self.page.locator('h1')).to_have_text('模型健康监控')
        expect(self.page.locator('[data-overview="completed"] strong')).to_have_text('1')
        expect(self.page.locator('[data-overview="anomalies"] strong')).to_have_text('0')
        expect(self.page.locator('#window')).not_to_be_visible()
        self.screenshot('templates-overview-desktop.png')
        self.page.locator('.dashboard-details > summary').click()
        expect(self.page.locator('#window')).to_be_visible()
        self.page.locator('#window').select_option('1')
        expect(self.page.locator('[data-overview="completed"] strong')).to_have_text('1')
        self.page.goto(self.url+'/admin')
        self.page.locator('#manage-templates').click()
        expect(self.page.locator(f'[data-template-archive="{logic["id"]}"]')).to_be_disabled()
        self.page.locator(f'[data-template-edit="{logic["id"]}"]').click()
        self.page.locator('#template-body').fill('只回答数字 8')
        self.page.locator('#template-answer').fill('8')
        self.page.locator('#template-save').click()
        expect(self.page.locator('#template-dialog')).not_to_be_visible()
        self.assertEqual(self.app.repository.run(rid)['template']['revision'],1)
        next_run = self.app.scheduler.enqueue(mid,['candy'])[0]
        self.app.scheduler.execute(*self.app.scheduler.claim())
        self.assertEqual(self.app.repository.run(next_run)['status'],'invalid')
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('#manage-templates').click()
        self.page.locator(f'[data-template-edit="{graphic["id"]}"]').click()
        self.screenshot('graphic-template-mobile.png')
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.page.keyboard.press('Escape')
        self.page.goto(self.url)
        expect(self.page.locator('[data-overview="completed"] strong')).to_have_text('2')
        expect(self.page.locator('[data-overview="anomalies"] strong')).to_have_text('1')
        self.screenshot('templates-overview-mobile.png')

    def test_mobile_long_custom_names_and_unsaved_category(self):
        mid = self.app.repository.save_config(dict(model='custom',display_name='自定义模型名称'*15,base_url='https://fixture.invalid/v1',api_key='test-key',category_name='任意自定义系列名称'*6))['id']
        self.page.set_viewport_size({'width':320,'height':700})
        self.login()
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.page.locator(f'[data-model-edit="{mid}"]').click()
        self.screenshot('models-form-mobile.png')
        self.page.keyboard.press('Escape')
        self.page.locator('#manage-categories').click()
        self.page.locator('#category-name').fill('尚未保存')
        self.page.keyboard.press('Escape')
        self.page.locator('[data-continue]').click()
        expect(self.page.locator('#category-name')).to_have_value('尚未保存')
        self.screenshot('categories-mobile.png')
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.page.locator('#category-save').click()
        expect(self.page.locator('#category-dialog')).not_to_be_visible()
        self.page.goto(self.url)
        expect(self.page.locator('.channel-card')).to_have_count(1)
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.screenshot('models-long-names-mobile.png')


if __name__ == '__main__':
    unittest.main(verbosity=2)
