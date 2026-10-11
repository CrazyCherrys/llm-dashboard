import concurrent.futures
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from database import Database
from metrics import Metrics
from repositories import Repository
from settings import SiteSettings
from storage import Storage
from workers import Scheduler


DEFAULTS = dict(worker_concurrency=2, run_retention_days=90,
                artifact_retention_days=30, output_retention_days=30)
SETTING_ENV = ('WORKER_CONCURRENCY', 'RUN_RETENTION_DAYS',
               'ARTIFACT_RETENTION_DAYS', 'OUTPUT_RETENTION_DAYS')


class SiteSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(self.temp.name)
        self.settings = SiteSettings(self.database)
        environment = {key: value for key, value in os.environ.items() if key not in SETTING_ENV}
        self.env_patch = patch.dict(os.environ, environment, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_defaults_and_saved_values_survive_restart_and_environment_changes(self):
        self.assertEqual(self.settings.get(), DEFAULTS)
        expected = dict(DEFAULTS, worker_concurrency=1, run_retention_days=45)
        self.assertEqual(self.settings.save({'worker_concurrency': 1, 'run_retention_days': 45}), expected)
        with patch.dict(os.environ, WORKER_CONCURRENCY='invalid', RUN_RETENTION_DAYS='1',
                        ARTIFACT_RETENTION_DAYS='1', OUTPUT_RETENTION_DAYS='1'):
            restarted = SiteSettings(Database(self.temp.name))
            self.assertEqual(restarted.get(), expected)
            self.assertEqual(restarted.save({'output_retention_days': 7}), dict(expected, output_retention_days=7))

    def test_environment_defaults_are_used_until_first_save(self):
        with patch.dict(os.environ, WORKER_CONCURRENCY='3', RUN_RETENTION_DAYS='120',
                        ARTIFACT_RETENTION_DAYS='60', OUTPUT_RETENTION_DAYS='40'):
            expected = dict(worker_concurrency=3, run_retention_days=120,
                            artifact_retention_days=60, output_retention_days=40)
            self.assertEqual(self.settings.get(), expected)
            self.assertEqual(self.settings.save({'worker_concurrency': 1}), dict(expected, worker_concurrency=1))
        self.assertEqual(self.settings.get(), dict(expected, worker_concurrency=1))

    def test_bad_values_reject_the_entire_update(self):
        expected = self.settings.save(DEFAULTS)
        cases = [None, [], {'unknown': 1}, {'worker_concurrency': 0}, {'worker_concurrency': 9}]
        for field in DEFAULTS:
            for invalid in (True, False, 1.0, '2', None, {}, [], -1):
                cases.append({field: invalid})
        for field in ('run_retention_days', 'artifact_retention_days', 'output_retention_days'):
            cases.extend(({field: 0}, {field: 3651}, {'worker_concurrency': 1, field: False}))
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    self.settings.save(values)
                self.assertEqual(self.settings.get(), expected)

    def test_failed_first_save_does_not_freeze_defaults(self):
        with self.assertRaises(ValueError):
            self.settings.save({'worker_concurrency': 1, 'output_retention_days': False})
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM app_settings').fetchone()[0], 0)
        with patch.dict(os.environ, WORKER_CONCURRENCY='4'):
            self.assertEqual(self.settings.get()['worker_concurrency'], 4)

    def test_boundaries_and_concurrent_partial_updates_are_preserved(self):
        self.settings.save(dict(worker_concurrency=8, run_retention_days=3650,
                                artifact_retention_days=1, output_retention_days=1))
        barrier = threading.Barrier(2)

        def update(values):
            barrier.wait(timeout=3)
            return SiteSettings(self.database).save(values)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(update, {'run_retention_days': 365}),
                       pool.submit(update, {'output_retention_days': 7})]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.settings.get(), dict(worker_concurrency=8, run_retention_days=365,
                                                  artifact_retention_days=1, output_retention_days=7))


class LiveSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(self.temp.name)
        self.settings = SiteSettings(self.database)
        self.settings.save(DEFAULTS)
        self.repository = Repository(self.database)
        self.metrics = Metrics(self.database)
        self.cid = self.repository.save_channel(dict(title='test', base_url='https://example.com/v1',
                                                     api_key='test-only-key'))['id']

    def model(self, name):
        return self.repository.save_model({'model': name}, channel_id=self.cid)['id']

    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for live scheduler change')

    def test_web_settings_raise_and_lower_concurrency_without_interruptions(self):
        self.settings.save({'worker_concurrency': 1})
        entered = [threading.Event() for _ in range(6)]
        released = [threading.Event() for _ in range(6)]
        state_lock = threading.Lock()
        counts = {'active': 0, 'peak': 0}

        def model_call(config, prompt, on_first=None):
            index = int(config['model'][4:])
            with state_lock:
                counts['active'] += 1
                counts['peak'] = max(counts['peak'], counts['active'])
            entered[index].set()
            try:
                if not released[index].wait(10):
                    raise RuntimeError('Test did not release blocked model call')
                return '21', {'http_status': 200}, config['model']
            finally:
                with state_lock:
                    counts['active'] -= 1

        scheduler = Scheduler(self.database, self.metrics, model_call=model_call, settings=self.settings)

        def stop_workers():
            scheduler.stop()
            for event in released:
                event.set()
            for thread in scheduler.threads:
                thread.join(timeout=3)
            self.assertTrue(all(not thread.is_alive() for thread in scheduler.threads))

        self.addCleanup(stop_workers)
        scheduler.start()
        ids = [scheduler.enqueue(self.model(f'mock{index}'), ['candy'])[0] for index in range(6)]
        self.assertTrue(entered[0].wait(3))
        self.assertFalse(entered[1].wait(.15))
        self.settings.save({'worker_concurrency': 3})
        scheduler.settings_changed()
        self.assertTrue(entered[1].wait(3))
        self.assertTrue(entered[2].wait(3))
        self.assertFalse(entered[3].wait(.15))

        self.settings.save({'worker_concurrency': 1})
        scheduler.settings_changed()
        # Finishing two of three in-flight calls must not free capacity under
        # the new limit; both queued work and the third request stay untouched.
        for index in (0, 1):
            released[index].set()
            self.wait_for(lambda: self.repository.run(ids[index])['status'] == 'passed')
            self.assertFalse(entered[3].wait(.15))
        self.assertEqual(self.repository.run(ids[2])['status'], 'running')
        self.assertEqual(self.repository.run(ids[3])['status'], 'queued')
        released[2].set()
        self.assertTrue(entered[3].wait(3))
        self.assertFalse(entered[4].wait(.15))
        released[3].set()
        self.assertTrue(entered[4].wait(3))
        released[4].set()
        self.assertTrue(entered[5].wait(3))
        released[5].set()
        self.wait_for(lambda: all(self.repository.run(rid)['status'] == 'passed' for rid in ids))
        self.assertEqual(counts['peak'], 3)

    def finished_run(self, scheduler, age, now):
        rid = scheduler.enqueue(self.model(f'retention-{age}'), ['pelican'])[0]
        scheduler.claim()
        aid, relative = scheduler.storage.save_svg(rid, '<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with self.database.connect() as db:
            db.execute("UPDATE check_runs SET status='passed',finished=?,output='saved answer' WHERE id=?",
                       (now-age*86400, rid))
            db.execute('UPDATE artifacts SET generated=? WHERE id=?', (now-age*86400, aid))
        return rid, relative

    def test_cleanup_uses_saved_retention_and_applies_updated_values(self):
        scheduler = Scheduler(self.database, self.metrics, settings=self.settings)
        self.assertIs(scheduler.storage.settings, self.settings)
        now = time.time()
        young, young_file = self.finished_run(scheduler, 2, now)
        old, old_file = self.finished_run(scheduler, 6, now)
        self.settings.save({'run_retention_days': 10, 'artifact_retention_days': 5,
                            'output_retention_days': 3})
        with patch.dict(os.environ, RUN_RETENTION_DAYS='1', ARTIFACT_RETENTION_DAYS='1', OUTPUT_RETENTION_DAYS='1'):
            scheduler.storage.cleanup(now)
        self.assertEqual(self.repository.run(young)['output'], 'saved answer')
        self.assertIsNotNone(scheduler.storage.path(young_file))
        self.assertEqual(self.repository.run(old)['output'], '')
        self.assertIsNone(scheduler.storage.path(old_file))
        self.settings.save({'output_retention_days': 1})
        scheduler.storage.cleanup(now)
        self.assertEqual(self.repository.run(young)['output'], '')
        self.assertIsNotNone(scheduler.storage.path(young_file))

    def test_artifact_and_answer_retention_cannot_extend_record_lifetime(self):
        self.settings.save({'run_retention_days': 1, 'artifact_retention_days': 3650,
                            'output_retention_days': 3650})
        scheduler = Scheduler(self.database, self.metrics, settings=self.settings)
        now = time.time()
        rid, relative = self.finished_run(scheduler, 2, now)
        Storage(self.database, self.settings).cleanup(now)
        with self.database.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM check_runs WHERE id=?', (rid,)).fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], 0)
        self.assertIsNone(scheduler.storage.path(relative))

    def test_next_hourly_cleanup_uses_new_settings_without_early_deletion(self):
        scheduler = Scheduler(self.database, self.metrics, settings=self.settings)
        clock = [10000]
        iterations = [0]
        calls = []

        def tick():
            iterations[0] += 1
            if iterations[0] == 2:
                clock[0] += 1
                self.settings.save({'worker_concurrency': 1, 'output_retention_days': 1})
                scheduler.settings_changed()
            elif iterations[0] == 3:
                self.assertEqual(len(calls), 1)
                self.assertEqual(scheduler.concurrency, 1)
                clock[0] += 3601
            elif iterations[0] == 4:
                scheduler.stop()

        def cleanup():
            calls.append(self.settings.get())

        scheduler.tick = tick
        scheduler.storage.cleanup = cleanup
        with patch('workers.time.time', side_effect=lambda: clock[0]), \
             patch.object(scheduler.wake, 'wait', return_value=True):
            scheduler.loop()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['output_retention_days'], 30)
        self.assertEqual(calls[1]['output_retention_days'], 1)


if __name__ == '__main__':
    unittest.main()
