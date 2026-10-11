"""Atomic artifact writes and bounded retention cleanup."""
import base64
import datetime
import os
from pathlib import Path
import time
from settings import SiteSettings


class Storage:
    def __init__(self, database, settings=None):
        self.database = database
        self.settings = settings if settings is not None else SiteSettings(database)

    @staticmethod
    def atomic(path, content):
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        temp = path.with_suffix(path.suffix+'.tmp')
        temp.write_bytes(content)
        os.chmod(temp,0o600)
        os.replace(temp,path)

    def save_svg(self, run_id, svg):
        now = time.time()
        date = datetime.datetime.fromtimestamp(now,datetime.timezone.utc).strftime('%Y/%m')
        relative = Path(date)/str(run_id)/'result.svg'
        self.atomic(self.database.artifact_dir/relative,svg.encode())
        with self.database.connect() as db:
            artifact_id = db.execute('INSERT INTO artifacts(run_id,svg_path,generated) VALUES(?,?,?)',
                                     (run_id,str(relative),now)).lastrowid
        return artifact_id,relative

    def save_thumbnail(self, artifact_id, relative, frames):
        path = relative.with_name('thumbnail.png')
        self.atomic(self.database.artifact_dir/path,base64.b64decode(frames[1]['png']))
        with self.database.connect() as db:
            db.execute("UPDATE artifacts SET thumbnail_path=?,render_status='ready' WHERE id=?",(str(path),artifact_id))

    def path(self, relative):
        if not relative:
            return None
        root = self.database.artifact_dir.resolve()
        path = (root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        settings = self.settings.get()
        run_days = settings['run_retention_days']
        file_days = settings['artifact_retention_days']
        output_days = settings['output_retention_days']
        file_days = min(file_days,run_days)
        output_days = min(output_days,run_days)
        budget = time.monotonic()+10
        while time.monotonic()<budget:
            with self.database.connect() as db:
                rows = db.execute("SELECT a.* FROM artifacts a JOIN check_runs r ON r.id=a.run_id WHERE a.generated<? AND r.status NOT IN ('queued','running') ORDER BY a.id LIMIT 100",(now-file_days*86400,)).fetchall()
            if not rows:
                break
            removed = 0
            for row in rows:
                try:
                    directory = None
                    for field in ('svg_path','thumbnail_path'):
                        path = self.path(row[field])
                        if path:
                            directory = path.parent
                            path.unlink()
                    with self.database.connect() as db:
                        db.execute('DELETE FROM artifacts WHERE id=?',(row['id'],))
                    removed += 1
                    if directory:
                        for parent in (directory,directory.parent,directory.parent.parent):
                            if parent==self.database.artifact_dir:
                                break
                            try:
                                parent.rmdir()
                            except OSError:
                                break
                except OSError:
                    continue
            if not removed:
                break
        with self.database.connect() as db:
            db.execute("UPDATE check_runs SET output='' WHERE finished<? AND output!='' AND status NOT IN ('queued','running')",(now-output_days*86400,))
            db.execute("UPDATE check_runs SET legacy_svg='' WHERE finished<? AND legacy_svg!='' AND status NOT IN ('queued','running')",(now-file_days*86400,))
            # Migrated source tables are never used by the new service, but keeping
            # their old payloads indefinitely would bypass the retention policy.
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'runs' in tables:
                columns = {r[1] for r in db.execute('PRAGMA table_info(runs)')}
                timestamp = 'COALESCE(finished,started)' if 'finished' in columns else 'started'
                for column in ('output','tests'):
                    if column in columns:
                        db.execute(f"UPDATE runs SET {column}=? WHERE {timestamp}<? AND {column}!=?",
                                   ('{}' if column=='tests' else '',now-output_days*86400,'{}' if column=='tests' else ''))
                if 'svg' in columns:
                    db.execute(f"UPDATE runs SET svg='' WHERE {timestamp}<? AND svg!=''",(now-file_days*86400,))
                db.execute(f'DELETE FROM runs WHERE id IN (SELECT id FROM runs WHERE {timestamp}<? LIMIT 100)',(now-run_days*86400,))
            if 'guest_results' in tables:
                db.execute('DELETE FROM guest_results WHERE created<?',(now-3600,))
        while time.monotonic()<budget:
            with self.database.connect() as db:
                result = db.execute("DELETE FROM check_runs WHERE id IN (SELECT r.id FROM check_runs r LEFT JOIN artifacts a ON a.run_id=r.id WHERE r.finished<? AND r.status NOT IN ('queued','running') AND a.id IS NULL LIMIT 100)",(now-run_days*86400,))
                if result.rowcount<100:
                    break
