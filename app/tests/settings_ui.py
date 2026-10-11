"""Real HTTP/SQLite browser checks for first setup and system settings.

Run separately from the backend suite: python tests/settings_ui.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Application, Handler, Server


PASSWORD = 'isolated-settings-password'
NEW_PASSWORD = 'updated-settings-password'
SETTINGS = {
    'worker_concurrency': 'settings-concurrency',
    'run_retention_days': 'settings-run-retention',
    'artifact_retention_days': 'settings-artifact-retention',
    'output_retention_days': 'settings-output-retention',
}


class SettingsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start()
        for name in ['ADMIN_TOKEN', 'WORKER_CONCURRENCY', 'RUN_RETENTION_DAYS',
                     'ARTIFACT_RETENTION_DAYS', 'OUTPUT_RETENTION_DAYS']:
            os.environ.pop(name, None)
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name)
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
        self.environment.stop()
        self.assertEqual(self.errors, [])

    def setup_login(self):
        self.page.goto(self.url + '/admin')
        expect(self.page.locator('#auth-title')).to_have_text('首次设置管理密码')
        expect(self.page.locator('#manage-settings')).to_be_hidden()
        self.page.locator('#admin-token').fill(PASSWORD)
        self.page.locator('#confirm-password').fill(PASSWORD)
        self.page.locator('#auth-submit').click()
        expect(self.page.locator('#manage-settings')).to_be_visible()
        expect(self.page.locator('#model-count')).to_contain_text('张卡片')

    def login(self, page=None, password=PASSWORD):
        page = page or self.page
        page.goto(self.url + '/admin')
        page.locator('#admin-token').fill(password)
        page.locator('#auth-submit').click()
        expect(page.locator('#manage-settings')).to_be_visible()

    def open_settings(self, page=None):
        page = page or self.page
        page.locator('#manage-settings').click()
        expect(page.locator('#settings-fields')).to_be_enabled()

    def assert_settings(self, values, page=None):
        page = page or self.page
        for key, input_id in SETTINGS.items():
            expect(page.locator('#' + input_id)).to_have_value(str(values[key]))

    def assert_private_settings_cleared(self, page=None):
        page = page or self.page
        expect(page.locator('#manage-settings')).to_be_hidden()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        expect(page.locator('#password-dialog')).not_to_be_visible()
        for input_id in [*SETTINGS.values(), 'current-password', 'new-password',
                         'new-password-confirm', 'admin-token', 'confirm-password']:
            expect(page.locator('#' + input_id)).to_have_value('')

    def screenshot(self, name):
        directory = os.environ.get('UI_ARTIFACT_DIR')
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(directory) / name), full_page=True)

    def release_response(self, delayed):
        route, response = delayed
        with self.page.expect_response(lambda item: item.url.endswith('/api/admin/settings')) as received:
            route.fulfill(response=response)
        received.value.json()
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')

    def test_first_web_setup_and_settings_survive_reload_and_restart(self):
        status = self.context.request.get(self.url + '/api/auth/status').json()
        self.assertFalse(status['configured'])
        self.assertTrue(status['setup_allowed'])
        self.setup_login()
        self.open_settings()
        values = dict(worker_concurrency=1, run_retention_days=30,
                      artifact_retention_days=7, output_retention_days=7)
        for key, input_id in SETTINGS.items():
            self.page.locator('#' + input_id).fill(str(values[key]))
        self.page.locator('#settings-save').click()
        expect(self.page.locator('#settings-status')).to_contain_text('设置已保存')
        self.assertEqual(self.context.request.get(self.url + '/api/admin/settings').json(), values)
        self.page.reload()
        expect(self.page.locator('#manage-settings')).to_be_visible()
        self.open_settings()
        self.assert_settings(values)
        self.screenshot('system-settings-desktop.png')

        # A fresh application reads the same SQLite data, while in-memory
        # sessions intentionally expire when the application restarts.
        self.app = Application(self.temp.name)
        self.server.app = self.app
        self.page.reload()
        expect(self.page.locator('#auth-title')).to_have_text('登录管理员后台')
        self.login()
        self.open_settings()
        self.assert_settings(values)

    def test_loading_retry_failed_save_unsaved_protection_and_mobile_layout(self):
        self.setup_login()
        self.page.route('**/api/admin/settings', lambda route: route.fulfill(
            status=503, content_type='application/json', body=json.dumps({'error': '暂时不可用'})))
        self.page.locator('#manage-settings').click()
        expect(self.page.locator('#settings-error')).to_contain_text('暂时不可用')
        expect(self.page.locator('#settings-save')).to_be_disabled()
        expect(self.page.locator('#settings-retry')).to_be_visible()
        self.page.unroute('**/api/admin/settings')
        self.page.locator('#settings-retry').click()
        expect(self.page.locator('#settings-fields')).to_be_enabled()
        self.page.locator('#settings-concurrency').fill('8')
        self.page.locator('#settings-run-retention').fill('3650')
        self.page.locator('#settings-output-retention').fill('1')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('.confirm-dialog')).to_be_visible()
        self.page.locator('[data-continue]').click()
        expect(self.page.locator('#settings-concurrency')).to_have_value('8')

        self.page.locator('#change-password').click()
        self.page.locator('#current-password').fill(PASSWORD)
        self.page.keyboard.press('Escape')
        self.page.locator('[data-discard]').click()
        expect(self.page.locator('#password-dialog')).not_to_be_visible()
        expect(self.page.locator('#current-password')).to_have_value('')
        expect(self.page.locator('#settings-concurrency')).to_have_value('8')
        expect(self.page.locator('#change-password')).to_be_focused()

        self.page.route('**/api/admin/settings', lambda route: route.fulfill(
            status=400, content_type='application/json', body=json.dumps({'error': '模拟保存失败'})))
        self.page.locator('#settings-save').click()
        expect(self.page.locator('#settings-error')).to_have_text('模拟保存失败')
        expect(self.page.locator('#settings-concurrency')).to_have_value('8')
        expect(self.page.locator('#settings-save')).to_be_enabled()
        self.page.unroute('**/api/admin/settings')
        self.page.locator('#settings-save').click()
        expect(self.page.locator('#settings-status')).to_contain_text('设置已保存')
        for width in [320, 390, 768, 1440]:
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.screenshot('system-settings-mobile.png')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#settings-dialog')).not_to_be_visible()
        expect(self.page.locator('.confirm-dialog')).to_have_count(0)
        expect(self.page.locator('#settings-concurrency')).to_have_value('')
        expect(self.page.locator('#manage-settings')).to_be_focused()
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_password_confirmation_errors_and_all_sessions_revoked(self):
        self.setup_login()
        observer = self.context.new_page()
        observer.goto(self.url + '/admin')
        self.open_settings(observer)
        observer.locator('#settings-concurrency').fill('8')
        observer.locator('#change-password').click()
        observer.locator('#current-password').fill(PASSWORD)

        other_context = self.browser.new_context()
        self.addCleanup(other_context.close)
        other = other_context.new_page()
        self.login(other)
        old_bearer = self.app.auth.login(PASSWORD)
        self.open_settings()
        self.page.locator('#change-password').click()
        self.page.locator('#current-password').fill('incorrect-password')
        self.page.locator('#new-password').fill(NEW_PASSWORD)
        self.page.locator('#new-password-confirm').fill('different-password')
        self.page.locator('#password-save').click()
        expect(self.page.locator('#password-error')).to_have_text('两次新密码不一致')
        self.page.locator('#new-password-confirm').fill(NEW_PASSWORD)
        self.page.locator('#password-save').click()
        expect(self.page.locator('#password-error')).to_contain_text('密码不正确')
        expect(self.page.locator('#current-password')).to_have_value('incorrect-password')
        expect(self.page.locator('#new-password')).to_have_value(NEW_PASSWORD)
        self.page.locator('#current-password').fill(PASSWORD)
        self.page.locator('#password-save').click()
        expect(self.page.locator('#auth-error')).to_contain_text('管理密码已修改')
        expect(observer.locator('#auth-error')).to_contain_text('管理密码已修改')
        self.assert_private_settings_cleared()
        self.assert_private_settings_cleared(observer)
        self.assertEqual(other_context.request.get(self.url + '/api/admin/settings').status, 401)
        self.assertEqual(self.context.request.get(self.url + '/api/admin/settings',
                         headers={'Authorization': 'Bearer ' + old_bearer}).status, 401)
        other.reload()
        expect(other.locator('#login-panel')).to_be_visible()
        other.locator('#admin-token').fill(PASSWORD)
        other.locator('#auth-submit').click()
        expect(other.locator('#auth-error')).to_contain_text('密码不正确')
        self.login(password=NEW_PASSWORD)
        self.open_settings()

    def test_late_settings_load_cannot_restore_values_after_new_session(self):
        self.setup_login()
        observer = self.context.new_page()
        observer.goto(self.url + '/admin')
        expect(observer.locator('#logout')).to_be_visible()
        delayed = []

        def hold_first_response(route):
            if not delayed:
                delayed.append((route, route.fetch()))
            else:
                route.continue_()

        self.page.route('**/api/admin/settings', hold_first_response)
        self.page.locator('#manage-settings').click()
        expect(self.page.locator('#settings-status')).to_contain_text('正在加载')
        expect(self.page.locator('#settings-save')).to_be_disabled()
        observer.locator('#logout').click()
        expect(self.page.locator('#login-panel')).to_be_visible()
        self.assert_private_settings_cleared()
        # Stay on the same document so its original fetch remains pending.
        self.page.locator('#admin-token').fill(PASSWORD)
        self.page.locator('#auth-submit').click()
        expect(self.page.locator('#manage-settings')).to_be_visible()
        values = dict(worker_concurrency=3, run_retention_days=50,
                      artifact_retention_days=14, output_retention_days=9)
        response = self.context.request.patch(self.url + '/api/admin/settings', data=values)
        self.assertEqual(response.status, 200)
        self.open_settings()
        self.assert_settings(values)
        self.release_response(delayed[0])
        self.page.wait_for_function('!settingsLoading')
        self.assert_settings(values)
        expect(self.page.locator('#settings-error')).to_have_text('')

    def test_pending_settings_save_disables_form_and_logout_clears_late_response(self):
        self.setup_login()
        observer = self.context.new_page()
        observer.goto(self.url + '/admin')
        expect(observer.locator('#logout')).to_be_visible()
        self.open_settings()
        self.page.locator('#settings-concurrency').fill('4')
        delayed = []

        def hold_saved_response(route):
            delayed.append((route, route.fetch()))

        self.page.route('**/api/admin/settings', hold_saved_response)
        self.page.locator('#settings-save').click()
        expect(self.page.locator('#settings-save')).to_be_disabled()
        expect(self.page.locator('#settings-dialog [data-close]').first).to_be_disabled()
        self.assertTrue(self.page.locator('#settings-form .dialog-scroll').evaluate('(el) => el.inert'))
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#settings-dialog')).to_be_visible()
        observer.locator('#logout').click()
        expect(self.page.locator('#login-panel')).to_be_visible()
        self.release_response(delayed[0])
        self.page.wait_for_function('!busy')
        self.assert_private_settings_cleared()
        self.assertFalse(self.page.locator('#settings-form .dialog-scroll').evaluate('(el) => el.inert'))

    def test_closed_settings_load_cannot_replace_a_reopened_dialog(self):
        self.setup_login()
        delayed = []

        def hold_first_response(route):
            if not delayed:
                delayed.append((route, route.fetch()))
            else:
                route.continue_()

        self.page.route('**/api/admin/settings', hold_first_response)
        self.page.locator('#manage-settings').click()
        expect(self.page.locator('#settings-status')).to_contain_text('正在加载')
        self.page.locator('#settings-dialog [data-close]').first.click()
        expect(self.page.locator('#settings-dialog')).not_to_be_visible()
        values = dict(worker_concurrency=5, run_retention_days=90,
                      artifact_retention_days=20, output_retention_days=10)
        response = self.context.request.patch(self.url + '/api/admin/settings', data=values)
        self.assertEqual(response.status, 200)
        self.open_settings()
        self.assert_settings(values)
        self.release_response(delayed[0])
        self.assert_settings(values)


if __name__ == '__main__':
    unittest.main(verbosity=2)
