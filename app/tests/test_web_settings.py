"""Real HTTP checks for browser initialization, site settings and password changes."""
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib import error, request

from server import Application, Handler, Server


class WebSettingsHTTPTests(unittest.TestCase):
    password = 'web-settings-test-password'
    new_password = 'replacement-web-settings-password'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name)
        self.server = Server(('0.0.0.0', 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.jar = CookieJar()
        self.client = request.build_opener(request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def fetch(self, path, values=None, method=None, headers=None, client=None):
        req = request.Request(self.base + path,
                              data=json.dumps(values).encode() if values is not None else None,
                              method=method,
                              headers={**({'Content-Type': 'application/json'} if values is not None else {}),
                                       **(headers or {})})
        try:
            response = (client or self.client).open(req, timeout=5)
        except error.HTTPError as failure:
            response = failure
        with response:
            return response.status, json.load(response), response.headers

    def initialize(self):
        self.assertEqual(self.fetch('/api/auth/setup', {'password': self.password})[0], 200)

    def login(self, password=None, client=None):
        code, data, _ = self.fetch('/api/auth/login', {'password': password or self.password}, client=client)
        self.assertEqual(code, 200)
        return data['token']

    def test_non_loopback_listener_supports_once_only_browser_initialization(self):
        code, status, _ = self.fetch('/api/auth/status')
        self.assertEqual(code, 200)
        self.assertFalse(status['configured'])
        self.assertTrue(status['setup_allowed'])
        self.assertEqual(self.fetch('/api/admin/settings')[0], 401)
        self.assertEqual(self.fetch('/api/admin/settings', {'worker_concurrency': 1}, 'PATCH')[0], 401)
        self.assertEqual(self.fetch('/api/admin/password', {'current_password': '', 'new_password': self.password})[0], 401)
        self.assertEqual(self.fetch('/api/auth/setup', {'password': self.password},
                                    headers={'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.fetch('/api/auth/setup', {'password': 'short'})[0], 400)
        self.assertFalse(self.app.auth.configured())
        self.assertEqual(self.fetch('/api/auth/setup', {'password': self.password},
                                    headers={'X-Forwarded-For': '192.0.2.1', 'X-Forwarded-Proto': 'https'})[0], 200)
        status = self.fetch('/api/auth/status')[1]
        self.assertTrue(status['configured'])
        self.assertFalse(status['setup_allowed'])
        self.assertEqual(self.fetch('/api/auth/setup', {'password': self.new_password})[0], 400)
        self.assertEqual(self.fetch('/api/auth/setup', {'password': self.password}, 'PATCH')[0], 405)
        self.login()

    def test_simultaneous_initialization_has_one_winner(self):
        barrier = threading.Barrier(2)
        passwords = [self.password, self.new_password]

        def setup(password):
            barrier.wait(timeout=5)
            code = self.fetch('/api/auth/setup', {'password': password}, client=request.build_opener())[0]
            return code, password

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(setup, passwords))
        self.assertEqual(sorted(code for code, _ in results), [200, 400])
        winning_password = next(password for code, password in results if code == 200)
        self.login(winning_password)
        with self.app.database.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM admin_auth').fetchone()[0], 1)

    def test_settings_are_private_validated_and_persist_across_restart(self):
        self.initialize()
        self.login()
        settings = dict(worker_concurrency=1, run_retention_days=30,
                        artifact_retention_days=7, output_retention_days=7)
        code, saved, headers = self.fetch('/api/admin/settings', settings, 'PATCH')
        self.assertEqual(code, 200)
        self.assertEqual(saved, settings)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.app.scheduler.concurrency, 1)
        for values in [{'worker_concurrency': 0}, {'worker_concurrency': True},
                       {'run_retention_days': 0}, {'output_retention_days': 3651},
                       {'artifact_retention_days': '7'}, {'password': self.password}]:
            with self.subTest(values=values):
                self.assertEqual(self.fetch('/api/admin/settings', values, 'PATCH')[0], 400)
                self.assertEqual(self.fetch('/api/admin/settings')[1], settings)
        self.assertEqual(self.fetch('/api/admin/settings', settings)[0], 405)
        self.app = Application(self.temp.name)
        self.server.app = self.app
        self.assertEqual(self.fetch('/api/admin/settings')[0], 401)
        self.login()
        self.assertEqual(self.fetch('/api/admin/settings')[1], settings)
        self.assertEqual(self.app.scheduler.concurrency, 1)
        self.assertEqual(self.app.repository.tasks(), [])

    def test_password_change_verifies_current_password_and_revokes_every_session(self):
        self.initialize()
        old_cookie_token = self.login()
        old_bearer_token = self.login(client=request.build_opener())
        settings = self.fetch('/api/admin/settings')[1]
        values = {'current_password': self.password, 'new_password': self.new_password}
        for invalid in [dict(values, current_password='incorrect'), dict(values, new_password='short')]:
            self.assertEqual(self.fetch('/api/admin/password', invalid)[0], 400)
            self.assertTrue(self.app.auth.valid(old_cookie_token))
            self.assertTrue(self.app.auth.valid(old_bearer_token))
        self.assertEqual(self.fetch('/api/admin/password', values, 'PATCH')[0], 405)
        code, result, headers = self.fetch('/api/admin/password', values)
        self.assertEqual(code, 200)
        self.assertEqual(result, {'ok': True})
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertNotIn(self.password, json.dumps(result))
        self.assertNotIn(self.new_password, json.dumps(result))
        self.assertEqual(len(self.jar), 0)
        for token in [old_cookie_token, old_bearer_token]:
            self.assertEqual(self.fetch('/api/admin/settings', headers={'Authorization': 'Bearer ' + token})[0], 401)
        self.assertEqual(self.fetch('/api/auth/login', {'password': self.password})[0], 401)
        self.login(self.new_password)
        self.assertEqual(self.fetch('/api/admin/settings')[1], settings)
        self.app = Application(self.temp.name)
        self.server.app = self.app
        self.login(self.new_password)
        self.assertEqual(self.app.repository.tasks(), [])

    def test_authenticated_settings_and_password_writes_reject_cross_site_requests(self):
        self.initialize()
        self.login()
        defaults = self.fetch('/api/admin/settings')[1]
        for headers in [{'Origin': 'https://other.example'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Authorization': 'Bearer invalid-session'}]:
            expected = 401 if 'Authorization' in headers else 403
            self.assertEqual(self.fetch('/api/admin/settings', {'worker_concurrency': 1}, 'PATCH', headers)[0], expected)
            self.assertEqual(self.fetch('/api/admin/password',
                                        {'current_password': self.password, 'new_password': self.new_password},
                                        headers=headers)[0], expected)
        self.assertEqual(self.fetch('/api/admin/settings')[1], defaults)
        self.assertTrue(self.fetch('/api/auth/status')[1]['authenticated'])


class FreshDeploymentTests(unittest.TestCase):
    def test_server_starts_on_all_interfaces_without_environment_password(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            environment = dict(os.environ, DATA_DIR=directory, HOST='0.0.0.0', PORT=str(port))
            for key in ['ADMIN_TOKEN', 'WORKER_CONCURRENCY', 'RUN_RETENTION_DAYS',
                        'ARTIFACT_RETENTION_DAYS', 'OUTPUT_RETENTION_DAYS']:
                environment.pop(key, None)
            process = subprocess.Popen([sys.executable, 'server.py'],
                                       cwd=Path(__file__).resolve().parents[1], env=environment,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                status = None
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        with request.urlopen(f'http://127.0.0.1:{port}/api/auth/status', timeout=.5) as response:
                            status = json.load(response)
                        break
                    except (OSError, error.URLError):
                        time.sleep(.05)
                self.assertIsNone(process.poll(), 'fresh server exited before webpage initialization')
                self.assertIsNotNone(status, 'fresh server did not become ready')
                self.assertFalse(status['configured'])
                self.assertTrue(status['setup_allowed'])
            finally:
                process.terminate()
                process.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
