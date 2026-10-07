"""Back up and restore synthetic account data in two disposable local PostGIS databases."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from backend.app.account_data import process_account_deletion_cleanup, replay_account_deletions
from backend.app.account_deletion_journal import initialize_ledger
from backend.app.auth import create_identity_and_session
from backend.app.main import create_app
from backend.app.storage import LocalObjectStore
from backend.app.uploads import process_upload
from backend.app.worker import run_once

GPX = b'<gpx version="1.1" creator="synthetic"><trk><name>Restore drill</name><trkseg><trkpt lat="51.01" lon="3.71"><time>2026-10-07T08:00:00Z</time></trkpt><trkpt lat="51.0101" lon="3.7101"><time>2026-10-07T08:00:10Z</time></trkpt></trkseg></trk></gpx>'


def identity(engine, subject):
    with engine.begin() as db:
        return create_identity_and_session(db, 'google', subject)


def headers(session):
    return {'Authorization': 'Bearer ' + session['token']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    admin_url = make_url(os.environ['POSTGIS_ADMIN_DATABASE_URL']).set(database='postgres')
    if admin_url.host not in (None, 'localhost', '127.0.0.1', '::1'):
        parser.error('restore drill requires local PostGIS')
    tools = {}
    for name in ('pg_dump', 'pg_restore'):
        tools[name] = shutil.which(name)
        if not tools[name]: parser.error(f'{name} must be available on PATH')
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    databases, engines = [], []
    report = {'scope': 'Synthetic local PostgreSQL/source-file backup and restore; no production backup or deployment acceptance'}
    try:
        with tempfile.TemporaryDirectory(prefix='city-runner-restore-drill-') as temporary:
            directory = Path(temporary)
            os.environ['ACCOUNT_DELETION_LEDGER'] = str(directory / 'independent' / 'ledger.jsonl')
            initialize_ledger()
            for _ in range(2):
                name = 'city_runner_test_' + uuid.uuid4().hex[:12]
                with admin.connect() as db: db.exec_driver_sql(f'CREATE DATABASE "{name}"')
                databases.append(name)
                engines.append(create_engine(admin_url.set(database=name)))
            original, restored = engines
            os.environ['DATABASE_URL'] = original.url.render_as_string(hide_password=False)
            os.environ['UPLOAD_STORAGE_DIR'] = str(directory / 'originals')
            config = Config(str(ROOT / 'backend/alembic.ini'))
            config.set_main_option('sqlalchemy.url', os.environ['DATABASE_URL'].replace('%', '%%'))
            command.upgrade(config, 'head')
            old = identity(original, 'synthetic-deleted-subject')
            other = identity(original, 'synthetic-unrelated-subject')
            old_id = old['account']['id']; other_id = other['account']['id']
            with TestClient(create_app(original)) as client:
                uploaded = client.post('/api/uploads', headers=headers(old), files={'file': ('restore.gpx', GPX, 'application/gpx+xml')})
                assert uploaded.status_code == 202, uploaded.text
                assert run_once(original, {'process_upload': lambda job: process_upload(original, job)})
                before = client.get('/api/account/export', headers=headers(old))
                assert before.status_code == 200, before.text
                with zipfile.ZipFile(io.BytesIO(before.content)) as archive:
                    assert archive.read(f"originals/source-{uploaded.json()['id']}.gpx") == GPX
                    activities = json.loads(archive.read('data/activities.json'))
                    assert len(activities) == 1 and activities[0]['tracks'] == [[[3.71, 51.01], [3.7101, 51.0101]]]
                backup = directory / 'database.dump'
                env = os.environ.copy()
                if admin_url.password: env['PGPASSWORD'] = admin_url.password
                source_url = admin_url.set(database=databases[0], drivername='postgresql', password=None).render_as_string(hide_password=False)
                restore_url = admin_url.set(database=databases[1], drivername='postgresql', password=None).render_as_string(hide_password=False)
                subprocess.run([tools['pg_dump'], '--format=custom', '--no-owner', '--file', str(backup), '--dbname', source_url], env=env, check=True, capture_output=True)
                shutil.copytree(directory / 'originals', directory / 'source-backup')
                deleted = client.request('DELETE', '/api/account', headers=headers(old), json={'confirmation': 'DELETE'})
                assert deleted.status_code == 202, deleted.text
                while process_account_deletion_cleanup(original): pass
                assert client.get('/api/me', headers=headers(old)).status_code == 401
                assert not list((directory / 'originals').rglob('*.gpx'))
            # Restore the old DB/source backup, retaining the newer independent journal.
            subprocess.run([tools['pg_restore'], '--no-owner', '--dbname', restore_url, str(backup)], env=env, check=True, capture_output=True)
            shutil.copytree(directory / 'source-backup', directory / 'restored-originals')
            os.environ['DATABASE_URL'] = restored.url.render_as_string(hide_password=False)
            os.environ['UPLOAD_STORAGE_DIR'] = str(directory / 'restored-originals')
            with restored.connect() as db:
                assert db.execute(text('SELECT count(*) FROM activities WHERE user_id=:id'), {'id': old_id}).scalar_one() == 1
            with TestClient(create_app(restored)) as client:
                assert client.get('/api/me', headers=headers(old)).status_code == 401
                assert replay_account_deletions(restored, force_cleanup=True) == 1
                while process_account_deletion_cleanup(restored): pass
                assert client.get('/api/me', headers=headers(other)).status_code == 200
                fresh = identity(restored, 'synthetic-deleted-subject')
                assert fresh['account']['id'] not in (old_id, other_id)
                replay_account_deletions(restored)
                assert client.get('/api/me', headers=headers(fresh)).status_code == 200
                assert client.get('/api/activities', headers=headers(fresh)).json()['items'] == []
                with restored.connect() as db:
                    for table, column in [('accounts', 'id'), ('activities', 'user_id'), ('activity_sources', 'account_id'), ('sessions', 'account_id'), ('login_identities', 'account_id'), ('jobs', 'account_id')]:
                        assert db.execute(text(f'SELECT count(*) FROM {table} WHERE {column}=:id'), {'id': old_id}).scalar_one() == 0
                    assert db.execute(text('SELECT status FROM account_deletions WHERE account_id=:id'), {'id': old_id}).scalar_one() == 'complete'
                assert LocalObjectStore().list_owned_keys(old_id) == []
                assert not list((directory / 'restored-originals').rglob('*.gpx'))
            report |= {'actual_pg_dump_restore': True, 'byte_exact_original_before_backup': True, 'old_token_revoked_after_restore_before_replay': True,
                'deleted_owner_rows_and_files_absent': True, 'unrelated_account_preserved': True, 'same_identity_fresh_account_survives_replay': True,
                'database_backup_bytes': backup.stat().st_size}
    finally:
        for engine in engines: engine.dispose()
        for name in databases:
            with admin.connect() as db: db.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()
    report['disposable_cleanup'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__': main()
