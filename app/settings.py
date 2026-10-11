"""Validated site settings persisted with the application data."""
import os


class SiteSettings:
    # Environment values are only defaults until settings are saved in the UI.
    fields = {
        'worker_concurrency': ('WORKER_CONCURRENCY', 2, 8, '检测并发数'),
        'run_retention_days': ('RUN_RETENTION_DAYS', 90, 3650, '检测记录保留天数'),
        'artifact_retention_days': ('ARTIFACT_RETENTION_DAYS', 30, 3650, '图形产物保留天数'),
        'output_retention_days': ('OUTPUT_RETENTION_DAYS', 30, 3650, '完整回答保留天数'),
    }

    def __init__(self, database):
        self.database = database

    @classmethod
    def validate(cls, key, value):
        _, _, maximum, label = cls.fields[key]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'{label}必须为 1–{maximum} 的整数')
        return value

    def _read(self, db, updates=None):
        stored = {row['key']: row['value'] for row in db.execute('SELECT key,value FROM app_settings')}
        updates = updates or {}
        result = {}
        for key, (environment, default, _, label) in self.fields.items():
            if key in updates:
                value = updates[key]
            elif key in stored:
                value = stored[key]
            else:
                try:
                    value = int(os.environ.get(environment, str(default)))
                except (TypeError, ValueError):
                    raise ValueError(f'{label}的默认值无效') from None
            result[key] = self.validate(key, value)
        return result

    def get(self):
        with self.database.connect() as db:
            return self._read(db)

    def save(self, values):
        if not isinstance(values, dict):
            raise ValueError('设置必须是对象')
        if set(values) - self.fields.keys():
            raise ValueError('包含未知设置项')
        for key, value in values.items():
            self.validate(key, value)
        with self.database.connect() as db:
            # Read and merge inside the write transaction so simultaneous partial
            # updates cannot replace each other's saved values.
            db.execute('BEGIN IMMEDIATE')
            result = self._read(db, values)
            db.executemany('INSERT INTO app_settings(key,value) VALUES(?,?) '
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', result.items())
        return result
