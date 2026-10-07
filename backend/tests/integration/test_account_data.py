"""Synthetic account archive, tombstone, cleanup and owner-fence tests on disposable PostGIS."""

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import unittest
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import make_url

from backend.app.account_data import process_account_deletion_cleanup, replay_account_deletions
from backend.app.account_deletion_journal import JournalError, initialize_ledger, read_records
from backend.app.main import create_app
from backend.app.jobs import claim
from backend.app.owner_guard import exclusive_owner_snapshot
from backend.app.storage import LocalObjectStore
from backend.app.uploads import process_upload

DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
SAMPLE_GPX = b'''<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
  <trk><name>Export run</name><trkseg><trkpt lat="50.1" lon="4.2"><time>2026-10-06T08:00:00Z</time></trkpt>
  <trkpt lat="50.2" lon="4.3"><time>2026-10-06T08:01:00Z</time></trkpt></trkseg></trk></gpx>'''


@unittest.skipUnless(DATABASE_URL, "run with disposable PostGIS harness")
class AccountDataIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not re.fullmatch(r"city_runner_test_[0-9a-f]{12}", make_url(DATABASE_URL).database or ""):
            raise RuntimeError("account data tests require generated disposable PostGIS")
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(config, "head")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.account = str(uuid.uuid4())
        self.token = uuid.uuid4().hex
        self.temp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self.temp.name) / "uploads"
        self.ledger = Path(self.temp.name) / "private" / "ledger.jsonl"
        self.env = patch.dict(os.environ, {
            "UPLOAD_STORAGE_DIR": str(self.store_dir),
            "ACCOUNT_DELETION_LEDGER": str(self.ledger),
        })
        self.env.start()
        initialize_ledger()
        self.store = LocalObjectStore(self.store_dir)
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": self.account})
            db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                VALUES (:digest,:account,:expires)"""), {
                "digest": hashlib.sha256(self.token.encode()).hexdigest(), "account": self.account,
                "expires": datetime.now(timezone.utc) + timedelta(hours=1),
            })
        self.client = TestClient(create_app(self.engine))
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id=:account"), {"account": self.account})
            db.execute(text("DELETE FROM accounts WHERE id=:account"), {"account": self.account})
        self.env.stop()
        self.temp.cleanup()

    def upload(self):
        response = self.client.post("/api/uploads", headers=self.headers,
                                    files={"file": ("run.gpx", SAMPLE_GPX, "application/gpx+xml")})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_export_is_private_zip_with_original_and_allowlisted_ids(self):
        result = self.upload()
        job = claim(self.engine, kind="process_upload")
        process_upload(self.engine, job, self.store)
        handles = []
        real_temporary_file = tempfile.TemporaryFile

        def track_temporary_file(*args, **kwargs):
            handle = real_temporary_file(*args, **kwargs)
            handles.append(handle)
            return handle

        with patch("backend.app.account_data.tempfile.TemporaryFile", side_effect=track_temporary_file):
            response = self.client.get("/api/account/export", headers=self.headers)
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            sources = json.loads(archive.read("data/sources.json"))
            activities = json.loads(archive.read("data/activities.json"))
            original = f"originals/source-{result['id']}.gpx"
            self.assertEqual(archive.read(original), SAMPLE_GPX)
            self.assertIn(original, manifest["entries"])
            self.assertEqual(sources[0]["id"], result["id"])
            self.assertEqual(activities[0]["id"], self._activity_id(result["id"]))
            self.assertEqual(activities[0]["tracks"], [[[4.2, 50.1], [4.3, 50.2]]])
            self.assertEqual(activities[0]["timestamps"], [["2026-10-06T08:00:00Z", "2026-10-06T08:01:00Z"]])
            all_json = b"".join(archive.read(name) for name in archive.namelist() if name.endswith(".json"))
            self.assertNotIn(b"private_object_key", all_json)
            self.assertNotIn(b"lease_token", all_json)
            self.assertNotIn(b"token_digest", all_json)

    def test_export_missing_original_fails_and_removes_temporary_archive(self):
        result = self.upload()
        self.store.delete(self._key(result["id"]), owner_id=self.account)
        handles = []
        real_temporary_file = tempfile.TemporaryFile

        def track_temporary_file(*args, **kwargs):
            handle = real_temporary_file(*args, **kwargs)
            handles.append(handle)
            return handle

        with patch("backend.app.account_data.tempfile.TemporaryFile", side_effect=track_temporary_file):
            response = self.client.get("/api/account/export", headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)

    def test_replay_refuses_unsynced_intent_until_fsync_succeeds(self):
        self.upload()
        with patch("backend.app.account_deletion_journal.os.fsync", side_effect=OSError("synthetic fsync failure")):
            response = self.client.request("DELETE", "/api/account", headers=self.headers,
                                           json={"confirmation": "DELETE"})
        self.assertEqual(response.status_code, 503, response.text)
        record = read_records()[0]
        with patch("backend.app.account_deletion_journal.os.fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(JournalError):
                replay_account_deletions(self.engine, self.store)
        with self.engine.connect() as db:
            self.assertIsNotNone(db.execute(text("SELECT 1 FROM accounts WHERE id=:id"), {
                "id": self.account,
            }).first())
        self.assertEqual(replay_account_deletions(self.engine, self.store), 1)
        with self.engine.connect() as db:
            self.assertIsNone(db.execute(text("SELECT 1 FROM accounts WHERE id=:id"), {
                "id": self.account,
            }).first())
        self.assertEqual(record["account_id"], self.account)

    def test_snapshot_invalidates_pool_connection_after_lock_commit_failure(self):
        single_connection_engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0,
                                                 pool_timeout=2, pool_pre_ping=True)
        original_commit = Connection.commit
        failed = False

        def fail_first_commit(connection):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("synthetic commit outcome uncertainty")
            return original_commit(connection)

        try:
            with patch.object(Connection, "commit", fail_first_commit):
                with self.assertRaises(OSError):
                    with exclusive_owner_snapshot(single_connection_engine, self.account):
                        pass
            self.assertTrue(failed)
            with single_connection_engine.connect() as db:
                acquired = db.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"), {
                    "key": f"city-runner:owner:{self.account}",
                }).scalar_one()
                self.assertTrue(acquired)
                db.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"), {
                    "key": f"city-runner:owner:{self.account}",
                })
        finally:
            single_connection_engine.dispose()

    def test_export_and_delete_preserve_other_account_fit_original(self):
        self.upload()
        other = str(uuid.uuid4())
        fit_bytes = b"synthetic FIT bytes owned by another account"
        fit_key = self.store.write(fit_bytes, extension="fit", owner_id=other)
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": other})
            db.execute(text("""INSERT INTO activity_sources(account_id,source_kind,content_hash,private_object_key,status)
                VALUES (:account,'fit',:hash,:key,'queued')"""), {
                "account": other, "hash": hashlib.sha256(fit_bytes).hexdigest(), "key": fit_key,
            })
        response = self.client.get("/api/account/export", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(len([name for name in archive.namelist() if name.startswith("originals/")]), 1)
        deleted = self.client.request("DELETE", "/api/account", headers=self.headers,
                                      json={"confirmation": "DELETE"})
        self.assertEqual(deleted.status_code, 202, deleted.text)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"), {
                "id": other,
            }).scalar_one(), 1)
        self.assertEqual(self.store.read(fit_key, owner_id=other), fit_bytes)
        self.store.delete(fit_key, owner_id=other)
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM accounts WHERE id=:id"), {"id": other})

    def test_export_cap_fails_before_successful_partial_download(self):
        self.upload()
        with patch("backend.app.account_data.MAX_EXPORT_UNCOMPRESSED", 4):
            response = self.client.get("/api/account/export", headers=self.headers)
        self.assertEqual(response.status_code, 413)

    def test_export_uses_one_connection_with_pool_size_one(self):
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=2)
        probe_engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=2)
        try:
            with TestClient(create_app(engine)) as client:
                response = client.get("/api/account/export", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            with probe_engine.connect() as db:
                acquired = db.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"), {
                    "key": f"city-runner:owner:{self.account}",
                }).scalar_one()
                self.assertTrue(acquired, "account export must release its session lock")
                db.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"), {
                    "key": f"city-runner:owner:{self.account}",
                }).scalar_one()
        finally:
            engine.dispose()
            probe_engine.dispose()

    def test_delete_requires_configured_ledger_and_typed_confirmation(self):
        delete = lambda confirmation: self.client.request("DELETE", "/api/account", headers=self.headers,
                                                           json={"confirmation": confirmation})
        self.assertEqual(delete("delete").status_code, 422)
        with patch.dict(os.environ, {"ACCOUNT_DELETION_LEDGER": ""}):
            self.assertEqual(delete("DELETE").status_code, 503)
        result = delete("DELETE")
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()["status"], "complete")
        self.assertEqual(self.client.get("/api/me", headers=self.headers).status_code, 401)
        record = read_records()[0]
        self.assertEqual(record["account_id"], self.account)
        self.assertEqual(delete("DELETE").status_code, 401)

    def test_deletion_captures_owned_files_and_old_cleanup_jobs_then_worker_finishes(self):
        imported = self.upload()
        source_key = self._key(imported["id"])
        legacy_key = self.store.write(b"synthetic legacy original")
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO jobs(account_id,kind,dedupe_key,payload,priority,status)
                VALUES (:account,'delete_private_object','legacy-cleanup',CAST(:payload AS jsonb),10,'failed')"""), {
                "account": self.account, "payload": json.dumps({"object_key": legacy_key}),
            })
        deleted = self.client.request("DELETE", "/api/account", headers=self.headers,
                                      json={"confirmation": "DELETE"}).json()
        record = read_records()[0]
        self.assertEqual(set(record["object_keys"]), {source_key, legacy_key})
        self.assertEqual(deleted["status"], "cleanup-pending")
        with self.engine.connect() as db:
            outbox = db.execute(text("SELECT object_key FROM account_deletion_outbox WHERE deletion_id=:id"), {
                "id": deleted["id"],
            }).scalars().all()
        self.assertEqual(set(outbox), {source_key, legacy_key})
        self.assertTrue(process_account_deletion_cleanup(self.engine, self.store))
        self.assertTrue(process_account_deletion_cleanup(self.engine, self.store))
        self.assertFalse(process_account_deletion_cleanup(self.engine, self.store))
        with self.engine.connect() as db:
            status = db.execute(text("SELECT status FROM account_deletions WHERE id=:id"), {
                "id": deleted["id"],
            }).scalar_one()
            self.assertEqual(db.execute(text("SELECT count(*) FROM account_deletion_outbox WHERE deletion_id=:id AND status='complete'"), {
                "id": deleted["id"],
            }).scalar_one(), 2)
        self.assertEqual(status, "complete")
        self.assertFalse((self.store._owner_dir(self.account) / source_key).exists())
        self.assertFalse((self.store.root / legacy_key).exists())

    def test_cleanup_failure_retries_after_worker_restart_and_completes(self):
        self.upload()
        deleted = self.client.request("DELETE", "/api/account", headers=self.headers,
                                      json={"confirmation": "DELETE"}).json()
        original_delete = self.store.delete
        fail_once = True

        def delete_once(key, **kwargs):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise OSError("synthetic filesystem failure")
            return original_delete(key, **kwargs)

        with patch.object(self.store, "delete", delete_once):
            self.assertTrue(process_account_deletion_cleanup(self.engine, self.store))
        with self.engine.begin() as db:
            db.execute(text("UPDATE account_deletion_outbox SET available_at=clock_timestamp()-interval '1 second' WHERE deletion_id=:id"), {
                "id": deleted["id"],
            })
        self.assertTrue(process_account_deletion_cleanup(self.engine, self.store))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM account_deletion_outbox WHERE deletion_id=:id"), {
                "id": deleted["id"],
            }).scalar_one(), "complete")

    def test_expired_cleanup_lease_cannot_ack_over_a_new_worker(self):
        self.upload()
        deleted = self.client.request("DELETE", "/api/account", headers=self.headers,
                                      json={"confirmation": "DELETE"}).json()
        entered, release = threading.Event(), threading.Event()
        original_delete = self.store.delete
        calls = 0
        call_lock = threading.Lock()

        def delay_first_delete(key, **kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
                number = calls
            if number == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("synthetic cleanup gate timed out")
            return original_delete(key, **kwargs)

        with patch.object(self.store, "delete", delay_first_delete):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(process_account_deletion_cleanup, self.engine, self.store)
                self.assertTrue(entered.wait(5))
                with self.engine.begin() as db:
                    db.execute(text("UPDATE account_deletion_outbox SET leased_until=clock_timestamp()-interval '1 second' WHERE deletion_id=:id AND status='running'"), {
                        "id": deleted["id"],
                    })
                second = pool.submit(process_account_deletion_cleanup, self.engine, self.store)
                self.assertTrue(second.result(timeout=10))
                release.set()
                first.result(timeout=10)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM account_deletion_outbox WHERE deletion_id=:id"), {
                "id": deleted["id"],
            }).scalar_one(), "complete")

    def test_export_waits_for_shared_upload_commit(self):
        reached_write, release_write = threading.Event(), threading.Event()
        reached_export = threading.Event()
        original_write = LocalObjectStore.write
        from backend.app import account_data
        original_snapshot = account_data.exclusive_owner_snapshot

        def delayed_write(store, content, **kwargs):
            key = original_write(store, content, **kwargs)
            reached_write.set()
            if not release_write.wait(5):
                raise TimeoutError("synthetic upload gate timed out")
            return key

        def observe_snapshot(engine, account_id):
            reached_export.set()
            return original_snapshot(engine, account_id)

        with patch.object(LocalObjectStore, "write", delayed_write), \
             patch.object(account_data, "exclusive_owner_snapshot", observe_snapshot):
            with ThreadPoolExecutor(max_workers=2) as pool:
                upload = pool.submit(self.upload)
                self.assertTrue(reached_write.wait(5))
                export = pool.submit(self.client.get, "/api/account/export", headers=self.headers)
                self.assertTrue(reached_export.wait(5))
                release_write.set()
                result = upload.result(timeout=10)
                response = export.result(timeout=10)
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn(f"originals/source-{result['id']}.gpx", archive.namelist())

    def test_cleanup_exhaustion_is_visible_and_operator_retry_resets(self):
        from backend.app.account_data import retry_account_deletion_cleanup

        self.upload()
        deleted = self.client.request("DELETE", "/api/account", headers=self.headers,
                                      json={"confirmation": "DELETE"}).json()
        with self.engine.begin() as db:
            db.execute(text("UPDATE account_deletion_outbox SET max_attempts=1 WHERE deletion_id=:id"), {
                "id": deleted["id"],
            })
        with patch.object(self.store, "delete", side_effect=OSError("synthetic disk failure")):
            self.assertTrue(process_account_deletion_cleanup(self.engine, self.store))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM account_deletion_outbox WHERE deletion_id=:id"), {
                "id": deleted["id"],
            }).scalar_one(), "failed")
        self.assertEqual(retry_account_deletion_cleanup(self.engine, deleted["id"]), 1)
        self.assertTrue(process_account_deletion_cleanup(self.engine, self.store))

    def test_corrupt_configured_journal_fails_auth_closed(self):
        self.ledger.write_bytes(b"partial journal")
        response = self.client.get("/api/me", headers=self.headers)
        self.assertEqual(response.status_code, 503)

    def test_journal_commit_point_rolls_forward_after_database_commit_failure(self):
        self.upload()
        fired = False

        def fail_commit(connection):
            nonlocal fired
            if not fired:
                fired = True
                raise RuntimeError("synthetic post-intent DB failure")

        event.listen(self.engine, "commit", fail_commit)
        try:
            with TestClient(create_app(self.engine), raise_server_exceptions=False) as client:
                response = client.request("DELETE", "/api/account", headers=self.headers,
                                          json={"confirmation": "DELETE"})
        finally:
            event.remove(self.engine, "commit", fail_commit)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(read_records()[0]["account_id"], self.account)
        self.assertEqual(self.client.get("/api/me", headers=self.headers).status_code, 401)
        self.assertEqual(replay_account_deletions(self.engine, self.store), 1)
        with self.engine.connect() as db:
            self.assertIsNone(db.execute(text("SELECT 1 FROM accounts WHERE id=:id"), {"id": self.account}).first())
            self.assertEqual(db.execute(text("SELECT count(*) FROM account_deletion_outbox WHERE status='queued' AND account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 1)

    def test_exclusive_deletion_waits_for_inflight_original_creation(self):
        reached_write, release_write = threading.Event(), threading.Event()
        original_write = LocalObjectStore.write

        def delayed_write(store, content, **kwargs):
            key = original_write(store, content, **kwargs)
            reached_write.set()
            if not release_write.wait(5):
                raise TimeoutError("synthetic storage gate timed out")
            return key

        with patch.object(LocalObjectStore, "write", delayed_write):
            with ThreadPoolExecutor(max_workers=2) as pool:
                upload = pool.submit(self.upload)
                self.assertTrue(reached_write.wait(5))
                deletion = pool.submit(self.client.request, "DELETE", "/api/account", headers=self.headers,
                                       json={"confirmation": "DELETE"})
                release_write.set()
                uploaded = upload.result(timeout=10)
                deleted = deletion.result(timeout=10)
        self.assertEqual(uploaded["id"], str(int(uploaded["id"])))
        self.assertEqual(deleted.status_code, 202, deleted.text)
        self.assertEqual(self.client.get("/api/me", headers=self.headers).status_code, 401)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT count(*) FROM account_deletion_outbox WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 1)

    def test_worker_cannot_commit_ingest_after_account_removal(self):
        self.upload()
        job = claim(self.engine, kind="process_upload")
        entered, release = threading.Event(), threading.Event()
        from backend.app import uploads
        parser = uploads.parse_gpx

        def pause_parse(content):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("synthetic parser gate timed out")
            return parser(content)

        errors = []
        with patch.object(uploads, "parse_gpx", pause_parse):
            with ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(self._process_upload, job, errors)
                self.assertTrue(entered.wait(5))
                deleted = self.client.request("DELETE", "/api/account", headers=self.headers,
                                              json={"confirmation": "DELETE"})
                self.assertEqual(deleted.status_code, 202, deleted.text)
                release.set()
                worker.result(timeout=10)
        self.assertEqual(errors, [410])
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities WHERE user_id=:id"), {
                "id": self.account,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT count(*) FROM account_deletion_outbox WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 1)

    def test_stale_match_worker_cannot_mutate_tombstoned_owner(self):
        from backend.app.coverage import process_source_dataset

        deleted = self.client.request("DELETE", "/api/account", headers=self.headers,
                                      json={"confirmation": "DELETE"})
        self.assertEqual(deleted.status_code, 202, deleted.text)
        stale_job = {"id": 1, "account_id": self.account, "lease_token": str(uuid.uuid4()),
                     "payload": {"source_id": 1, "source_revision": 1, "dataset_id": 1}}
        with self.assertRaises(Exception) as raised:
            process_source_dataset(self.engine, stale_job)
        self.assertEqual(getattr(raised.exception, "status_code", None), 410)

    def _key(self, source_id):
        with self.engine.connect() as db:
            return db.execute(text("SELECT private_object_key FROM activity_sources WHERE id=:id"), {
                "id": int(source_id),
            }).scalar_one()

    def _activity_id(self, source_id):
        with self.engine.connect() as db:
            return str(db.execute(text("SELECT activity_id FROM activity_sources WHERE id=:id"), {
                "id": int(source_id),
            }).scalar_one())

    def _process_upload(self, job, errors):
        from fastapi import HTTPException
        try:
            process_upload(self.engine, job, self.store)
        except HTTPException as error:
            errors.append(error.status_code)


if __name__ == "__main__":
    unittest.main()
