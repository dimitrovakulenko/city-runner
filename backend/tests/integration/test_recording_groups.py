"""Exact GPX/FIT grouping, source removal and deletion-watermark tests."""

import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
import uuid
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from backend.app.main import create_app
from backend.app import coverage as coverage_module
from backend.app.geography import import_osm_xml
from backend.app.storage import LocalObjectStore
from backend.app.uploads import process_upload
from backend.app.worker import run_once
from backend.tests.test_fit import fit_fixture, semicircles
from backend.app.fit import parse_fit
from backend.app.recording_backfill import backfill_recording_identities
from backend.app.account_deletion_journal import initialize_ledger
from backend.app.jobs import claim, enqueue
from backend.app.recording_identity import canonical_recording
from backend.app.owner_guard import owner_lock
from backend.app.recording_groups import record_identity_deletion


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
FIT_BYTES = fit_fixture(records=((0x10000020, semicircles(50), semicircles(4)),
                                 (0x10000021, semicircles(50.1), semicircles(4.1))))
FIT_PARSED = parse_fit(FIT_BYTES)
GPX_BYTES = (f'''<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
<trk><name>Equivalent run</name><type>running</type><trkseg>
<trkpt lat="{FIT_PARSED.tracks[0][0][1]!r}" lon="{FIT_PARSED.tracks[0][0][0]!r}"><time>1998-07-03T21:24:48Z</time></trkpt>
<trkpt lat="{FIT_PARSED.tracks[0][1][1]!r}" lon="{FIT_PARSED.tracks[0][1][0]!r}"><time>1998-07-03T21:24:49Z</time></trkpt>
</trkseg></trk></gpx>''').encode()


@unittest.skipUnless(DATABASE_URL, "run with disposable PostGIS integration harness")
class RecordingGroupsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not re.fullmatch(r"city_runner_test_[0-9a-f]{12}", make_url(DATABASE_URL).database or ""):
            raise RuntimeError("recording group tests require generated disposable PostGIS")
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
        self.other_account = str(uuid.uuid4())
        self.token = uuid.uuid4().hex
        self.other_token = uuid.uuid4().hex
        self.dataset_ids = []
        self.temp = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp.name) / "uploads"
        self.storage_env = patch.dict(os.environ, {"UPLOAD_STORAGE_DIR": str(self.storage_root)})
        self.storage_env.start()
        self.ledger_env = patch.dict(os.environ, {
            "ACCOUNT_DELETION_LEDGER": str(Path(self.temp.name) / "private" / "deletions.jsonl"),
        })
        self.ledger_env.start()
        initialize_ledger()
        self.store = LocalObjectStore(self.storage_root)
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        other_digest = hashlib.sha256(self.other_token.encode()).hexdigest()
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:id),(:other)"), {
                "id": self.account, "other": self.other_account,
            })
            for account, token_hash in ((self.account, digest), (self.other_account, other_digest)):
                db.execute(text("INSERT INTO sessions(token_digest,account_id,expires_at) VALUES (:hash,:id,:expires)"), {
                    "hash": token_hash, "id": account,
                    "expires": datetime.now(timezone.utc) + timedelta(hours=1),
                })
        self.client = TestClient(create_app(self.engine))
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.other_headers = {"Authorization": f"Bearer {self.other_token}"}

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id=:id"), {"id": self.account})
            db.execute(text("DELETE FROM activities WHERE user_id=:id"), {"id": self.other_account})
            if self.dataset_ids:
                db.execute(text("DELETE FROM map_datasets WHERE id=ANY(:ids)"), {"ids": self.dataset_ids})
            db.execute(text("DELETE FROM accounts WHERE id IN (:id,:other)"), {
                "id": self.account, "other": self.other_account,
            })
        self.storage_env.stop()
        self.ledger_env.stop()
        self.temp.cleanup()

    def upload(self, content: bytes, filename: str):
        response = self.client.post("/api/uploads", headers=self.headers,
                                    files={"file": (filename, content, "application/octet-stream")})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def process_uploads(self):
        return run_once(self.engine, {"process_upload": lambda job: process_upload(self.engine, job, self.store)})

    def batch(self, name="run.gpx", file_format="gpx"):
        response = self.client.post("/api/import-batches", headers=self.headers, json={
            "request_id": str(uuid.uuid4()), "files": [{"name": name, "format": file_format}],
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def batch_upload(self, batch, content=GPX_BYTES, *, headers=None, expected=202):
        item = batch["items"][0]
        response = self.client.post(f"/api/import-batches/{batch['id']}/items/{item['id']}/upload",
            headers=headers or self.headers, files={"file": (item["name"], content, "application/octet-stream")})
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def test_gpx_fit_exact_group_source_delete_and_explicit_reimport(self):
        gpx = self.upload(GPX_BYTES, "first.gpx")
        fit = self.upload(FIT_BYTES, "second.fit")
        self.assertTrue(self.process_uploads())
        self.assertTrue(self.process_uploads())
        gpx_status = self.client.get(f"/api/uploads/{gpx['id']}", headers=self.headers).json()
        fit_status = self.client.get(f"/api/uploads/{fit['id']}", headers=self.headers).json()
        self.assertEqual(gpx_status["activity_id"], fit_status["activity_id"])
        activity_id = gpx_status["activity_id"]
        sources = self.client.get(f"/api/activities/{activity_id}/sources", headers=self.headers)
        self.assertEqual(sources.status_code, 200, sources.text)
        self.assertEqual(sources.json()["total"], 2)
        self.assertEqual({item["source_kind"] for item in sources.json()["items"]}, {"gpx", "fit"})

        remove_fit = self.client.delete(f"/api/activities/{activity_id}/sources/{fit['id']}", headers=self.headers)
        self.assertEqual(remove_fit.status_code, 204, remove_fit.text)
        self.assertEqual(self.client.get(f"/api/activities/{activity_id}", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/api/activities/{activity_id}/sources", headers=self.headers).json()["total"], 1)

        remove_last = self.client.delete(f"/api/activities/{activity_id}/sources/{gpx['id']}", headers=self.headers)
        self.assertEqual(remove_last.status_code, 204, remove_last.text)
        self.assertEqual(self.client.get(f"/api/activities/{activity_id}", headers=self.headers).status_code, 404)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM recording_identity_deletions WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 1)

        reimport = self.upload(GPX_BYTES, "again.gpx")
        self.assertTrue(self.process_uploads())
        reimported = self.client.get(f"/api/uploads/{reimport['id']}", headers=self.headers).json()
        self.assertNotEqual(reimported["activity_id"], activity_id)

    def test_predelete_accepted_upload_is_terminal_and_not_resurrected(self):
        pending = self.upload(GPX_BYTES, "pending.gpx")
        with self.engine.begin() as db:
            activity_id = db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,tracks,timestamps)
                VALUES (:account,'Legacy','1998-07-03','running',CAST(:tracks AS json),CAST(:times AS json))
                RETURNING id"""), {
                "account": self.account,
                "tracks": json.dumps(FIT_PARSED.tracks, separators=(",", ":")),
                "times": json.dumps(FIT_PARSED.timestamps, separators=(",", ":")),
            }).scalar_one()
        deleted = self.client.delete(f"/api/activities/{activity_id}", headers=self.headers)
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertTrue(self.process_uploads())
        self.assertEqual(self.client.get(f"/api/uploads/{pending['id']}", headers=self.headers).status_code, 404)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities WHERE user_id=:id"), {
                "id": self.account,
            }).scalar_one(), 0)

    def test_concurrent_gpx_fit_worker_commits_form_one_group(self):
        gpx = self.upload(GPX_BYTES, "parallel.gpx")
        fit = self.upload(FIT_BYTES, "parallel.fit")
        jobs = [claim(self.engine, kind="process_upload"), claim(self.engine, kind="process_upload")]
        self.assertTrue(all(jobs))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(process_upload, self.engine, job, self.store) for job in jobs]
            for future in futures:
                future.result(timeout=10)
        gpx_activity = self.client.get(f"/api/uploads/{gpx['id']}", headers=self.headers).json()["activity_id"]
        fit_activity = self.client.get(f"/api/uploads/{fit['id']}", headers=self.headers).json()["activity_id"]
        self.assertEqual(gpx_activity, fit_activity)
        self.assertEqual(self.client.get(f"/api/activities/{gpx_activity}/sources", headers=self.headers).json()["total"], 2)

    def test_paused_worker_cannot_resurrect_activity_deleted_during_parse(self):
        with self.engine.begin() as db:
            activity_id = db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,tracks,timestamps)
                VALUES (:account,'Legacy','1998-07-03','running',CAST(:tracks AS json),CAST(:times AS json))
                RETURNING id"""), {
                "account": self.account,
                "tracks": json.dumps(FIT_PARSED.tracks, separators=(",", ":")),
                "times": json.dumps(FIT_PARSED.timestamps, separators=(",", ":")),
            }).scalar_one()
        batch = self.batch()
        accepted = self.batch_upload(batch).json()
        job = claim(self.engine, kind="process_upload")
        started, release = threading.Event(), threading.Event()
        from backend.app.gpx import parse_gpx as real_parse_gpx

        def blocked_parse(content):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test parser release timed out")
            return real_parse_gpx(content)

        with patch("backend.app.uploads.parse_gpx", side_effect=blocked_parse):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(process_upload, self.engine, job, self.store)
                self.assertTrue(started.wait(5))
                deleted = self.client.delete(f"/api/activities/{activity_id}", headers=self.headers)
                self.assertEqual(deleted.status_code, 204, deleted.text)
                release.set()
                future.result(timeout=10)
        status = self.client.get(f"/api/import-batches/{batch['id']}", headers=self.headers).json()
        self.assertEqual(status["items"][0]["status"], "deleted")
        self.assertEqual(status["items"][0]["source_id"], accepted["source_id"])
        self.assertEqual(self.client.get(f"/api/uploads/{accepted['source_id']}", headers=self.headers).status_code, 404)
        with self.engine.connect() as db:
            row = db.execute(text("SELECT status FROM jobs WHERE id=:id"), {"id": job["id"]}).scalar_one()
        self.assertEqual(row, "cancelled")

    def test_duplicate_bytes_wait_for_predelete_worker_then_retry_same_manifest(self):
        with self.engine.begin() as db:
            activity_id = db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,tracks,timestamps)
                VALUES (:account,'Legacy','1998-07-03','running',CAST(:tracks AS json),CAST(:times AS json))
                RETURNING id"""), {
                "account": self.account,
                "tracks": json.dumps(FIT_PARSED.tracks, separators=(",", ":")),
                "times": json.dumps(FIT_PARSED.timestamps, separators=(",", ":")),
            }).scalar_one()
        old_batch = self.batch()
        old_item = self.batch_upload(old_batch).json()
        self.assertEqual(self.client.delete(f"/api/activities/{activity_id}", headers=self.headers).status_code, 204)
        with self.engine.begin() as db:
            db.execute(text("""UPDATE jobs SET status='failed',lease_token=NULL,leased_until=NULL
                WHERE account_id=:account AND kind='process_upload'
                  AND dedupe_key=concat('upload:',CAST(:source AS text),chr(58),'r1')"""), {
                "account": self.account, "source": old_item["source_id"],
            })
            db.execute(text("UPDATE activity_sources SET status='processing' WHERE id=:id AND account_id=:account"), {
                "id": old_item["source_id"], "account": self.account,
            })
        fresh_batch = self.batch("retry.gpx")
        blocked = self.batch_upload(fresh_batch, expected=409)
        self.assertEqual(blocked.status_code, 409)
        with self.engine.begin() as db:
            db.execute(text("""UPDATE jobs SET status='cancelled'
                WHERE account_id=:account AND kind='process_upload'
                  AND dedupe_key=concat('upload:',CAST(:source AS text),chr(58),'r1')"""), {
                "account": self.account, "source": old_item["source_id"],
            })
        direct = self.client.post("/api/uploads", headers=self.headers,
            files={"file": ("same.gpx", GPX_BYTES, "application/gpx+xml")})
        self.assertEqual(direct.status_code, 409, direct.text)
        before_retry = self.client.get(f"/api/import-batches/{fresh_batch['id']}", headers=self.headers).json()
        self.assertEqual(before_retry["items"][0]["status"], "awaiting_upload")
        with self.engine.connect() as db:
            blocked_item = db.execute(text("""SELECT source_id,content_hash FROM import_batch_items
                WHERE account_id=:account AND id=:item"""), {
                "account": self.account, "item": fresh_batch["items"][0]["id"],
            }).one()
        self.assertIsNone(blocked_item.source_id)
        self.assertIsNone(blocked_item.content_hash)
        with self.engine.begin() as db:
            db.execute(text("""DELETE FROM jobs WHERE account_id=:account AND kind='process_upload'
                AND dedupe_key=concat('upload:',CAST(:source AS text),chr(58),'r1')"""), {
                "account": self.account, "source": old_item["source_id"],
            })
        missing_job_batch = self.batch("missing-job.gpx")
        self.assertEqual(self.batch_upload(missing_job_batch, expected=409).status_code, 409)
        missing_job_raw = self.client.post("/api/uploads", headers=self.headers,
            files={"file": ("same.gpx", GPX_BYTES, "application/gpx+xml")})
        self.assertEqual(missing_job_raw.status_code, 409, missing_job_raw.text)
        with self.engine.connect() as db:
            missing_job_item = db.execute(text("""SELECT source_id,content_hash FROM import_batch_items
                WHERE account_id=:account AND id=:item"""), {
                "account": self.account, "item": missing_job_batch["items"][0]["id"],
            }).one()
        self.assertIsNone(missing_job_item.source_id)
        self.assertIsNone(missing_job_item.content_hash)
        with self.engine.begin() as db:
            enqueue(db, account_id=self.account, kind="process_upload",
                    dedupe_key=f"upload:{old_item['source_id']}:r1",
                    payload={"source_id": int(old_item["source_id"]), "revision": 1}, priority=100)
            db.execute(text("UPDATE activity_sources SET status='queued' WHERE id=:id AND account_id=:account"), {
                "id": old_item["source_id"], "account": self.account,
            })
        self.assertTrue(self.process_uploads())
        old_detail = self.client.get(f"/api/import-batches/{old_batch['id']}", headers=self.headers).json()
        self.assertEqual(old_detail["items"][0]["status"], "deleted")
        accepted = self.batch_upload(fresh_batch).json()
        self.assertNotEqual(accepted["source_id"], old_item["source_id"])
        self.assertTrue(self.process_uploads())
        new_status = self.client.get(f"/api/uploads/{accepted['source_id']}", headers=self.headers).json()
        self.assertNotEqual(new_status["activity_id"], str(activity_id))

    def test_upload_waiting_on_owner_guard_uses_postguard_acceptance_time(self):
        owner_key = f"city-runner:owner:{self.account}"
        waiting = threading.Event()

        def observe_shared_owner_lock(_connection, _cursor, statement, parameters, _context, _executemany):
            if "pg_advisory_xact_lock_shared" in statement and owner_key in str(parameters):
                waiting.set()

        event.listen(self.engine, "before_cursor_execute", observe_shared_owner_lock)
        try:
            with self.engine.connect() as db:
                transaction = db.begin()
                owner_lock(db, self.account, exclusive=True)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.upload, GPX_BYTES, "after-delete-lock.gpx")
                    self.assertTrue(waiting.wait(5))
                    identity = canonical_recording(FIT_PARSED.tracks, FIT_PARSED.timestamps,
                                                  FIT_PARSED.activity_type)
                    record_identity_deletion(db, self.account, identity)
                    transaction.commit()
                    accepted = future.result(timeout=10)
        finally:
            event.remove(self.engine, "before_cursor_execute", observe_shared_owner_lock)
        self.assertFalse(accepted["duplicate"])
        self.assertTrue(self.process_uploads())
        status = self.client.get(f"/api/uploads/{accepted['id']}", headers=self.headers).json()
        self.assertIsNotNone(status["activity_id"])


    def test_source_api_is_owner_scoped_for_signed_legacy_activity_id(self):
        legacy_id = -9_007_199_254_740_993
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,tracks,timestamps)
                VALUES (:id,:account,'Signed legacy','1998-07-03','running',CAST(:tracks AS json),CAST(:times AS json))"""), {
                "id": legacy_id, "account": self.account,
                "tracks": json.dumps(FIT_PARSED.tracks, separators=(",", ":")),
                "times": json.dumps(FIT_PARSED.timestamps, separators=(",", ":")),
            })
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,revision,status,created_at)
                VALUES (:account,:activity,'fit',1,'succeeded',clock_timestamp()) RETURNING id"""), {
                "account": self.account, "activity": legacy_id,
            }).scalar_one()
        path = f"/api/activities/{legacy_id}/sources"
        self.assertEqual(self.client.get(path, headers=self.other_headers).status_code, 404)
        self.assertEqual(self.client.get(path, headers=self.headers).json()["items"][0]["id"], str(source_id))
        removed = self.client.delete(f"{path}/{source_id}", headers=self.other_headers)
        self.assertEqual(removed.status_code, 404)
        self.assertEqual(self.client.delete(f"{path}/{source_id}", headers=self.headers).status_code, 204)

    def test_account_export_preserves_group_originals_and_deletion_identity(self):
        gpx = self.upload(GPX_BYTES, "archive.gpx")
        fit = self.upload(FIT_BYTES, "archive.fit")
        self.assertTrue(self.process_uploads())
        self.assertTrue(self.process_uploads())
        activity_id = self.client.get(f"/api/uploads/{gpx['id']}", headers=self.headers).json()["activity_id"]
        with self.client.stream("GET", "/api/account/export", headers=self.headers) as response:
            self.assertEqual(response.status_code, 200)
            payload = b"".join(response.iter_bytes())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            sources = json.loads(archive.read("data/sources.json"))
            activities = json.loads(archive.read("data/activities.json"))
            self.assertEqual(len(sources), 2)
            self.assertEqual(activities[0]["id"], activity_id)
            self.assertEqual(activities[0]["recording_fingerprint_version"], 1)
            self.assertEqual(len([name for name in archive.namelist() if name.startswith("originals/")]), 2)
        self.assertEqual(self.client.delete(f"/api/activities/{activity_id}", headers=self.headers).status_code, 204)
        with self.client.stream("GET", "/api/account/export", headers=self.headers) as response:
            self.assertEqual(response.status_code, 200)
            payload = b"".join(response.iter_bytes())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            deletions = json.loads(archive.read("data/recording_identity_deletions.json"))
            self.assertEqual(len(deletions), 1)
            self.assertEqual(deletions[0]["fingerprint_version"], 1)

    def test_signed_id_backfill_is_bounded_and_never_rewires_legacy_duplicates(self):
        with self.engine.begin() as db:
            for activity_id in (-9_007_199_254_740_993, 17):
                db.execute(text("""INSERT INTO activities
                    (id,user_id,name,date,activity_type,tracks,timestamps)
                    VALUES (:id,:account,'Legacy','1998-07-03','running',CAST(:tracks AS json),CAST(:times AS json))"""), {
                    "id": activity_id, "account": self.account,
                    "tracks": json.dumps(FIT_PARSED.tracks, separators=(",", ":")),
                    "times": json.dumps(FIT_PARSED.timestamps, separators=(",", ":")),
                })
        first = backfill_recording_identities(self.engine, self.account, batch_size=1, max_activities=1)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(first["after_id"], -9_007_199_254_740_993)
        second = backfill_recording_identities(self.engine, self.account, after_id=first["after_id"],
                                               batch_size=1, max_activities=1)
        self.assertEqual(second["processed"], 1)
        self.assertEqual(second["conflicts"], 1)
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT id,recording_fingerprint,recording_fingerprint_version
                FROM activities WHERE user_id=:account ORDER BY id"""), {
                "account": self.account,
            }).mappings().all()
        self.assertEqual(rows[0]["recording_fingerprint_version"], 1)
        self.assertIsNotNone(rows[0]["recording_fingerprint"])
        self.assertEqual(rows[1]["recording_fingerprint_version"], 1)
        self.assertIsNone(rows[1]["recording_fingerprint"])

        identity = canonical_recording(FIT_PARSED.tracks, FIT_PARSED.timestamps, FIT_PARSED.activity_type)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,tracks,timestamps,recording_fingerprint,recording_fingerprint_version)
                VALUES (100,:account,'Corrupt hash candidate','1998-07-03','running',
                    '[[[10,10],[11,11]]]'::json,'[[\"1998-07-03T21:24:48Z\",\"1998-07-03T21:24:49Z\"]]'::json,
                    :fingerprint,1)"""), {"account": self.other_account, "fingerprint": identity.fingerprint})
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,tracks,timestamps)
                VALUES (101,:account,'Legacy target','1998-07-03','running',CAST(:tracks AS json),CAST(:times AS json))"""), {
                "account": self.other_account,
                "tracks": json.dumps(FIT_PARSED.tracks, separators=(",", ":")),
                "times": json.dumps(FIT_PARSED.timestamps, separators=(",", ":")),
            })
        collision = backfill_recording_identities(self.engine, self.other_account, after_id=100,
                                                   batch_size=1, max_activities=1)
        self.assertEqual(collision["conflicts"], 1)
        with self.engine.connect() as db:
            collision_target = db.execute(text("""SELECT recording_fingerprint,recording_fingerprint_version
                FROM activities WHERE id=101 AND user_id=:account"""), {"account": self.other_account}).one()
        self.assertIsNone(collision_target.recording_fingerprint)
        self.assertEqual(collision_target.recording_fingerprint_version, 1)

    def test_source_delete_waits_for_coverage_lease_and_rebuilds_group(self):
        from backend.tests.integration.test_coverage import fixture_xml

        osm = Path(self.temp.name) / "recording-coverage.osm"
        osm.write_bytes(fixture_xml())
        imported = import_osm_xml(self.engine, osm, region=f"recording-{uuid.uuid4().hex}",
            city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z",
            coverage_mode="complete", coverage_evidence="Synthetic coverage fixture")
        self.dataset_ids.append(imported["dataset_id"])
        covered_fit = fit_fixture(records=((0x10000020, semicircles(0), semicircles(0)),
                                           (0x10000021, semicircles(0), semicircles(0.0014))))
        parsed = parse_fit(covered_fit)
        covered_gpx = (f'''<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
<trk><name>Covered run</name><type>running</type><trkseg>
<trkpt lat="{parsed.tracks[0][0][1]!r}" lon="{parsed.tracks[0][0][0]!r}"><time>1998-07-03T21:24:48Z</time></trkpt>
<trkpt lat="{parsed.tracks[0][1][1]!r}" lon="{parsed.tracks[0][1][0]!r}"><time>1998-07-03T21:24:49Z</time></trkpt>
</trkseg></trk></gpx>''').encode()
        gpx = self.upload(covered_gpx, "covered.gpx")
        fit = self.upload(covered_fit, "covered.fit")
        self.assertTrue(self.process_uploads())
        self.assertTrue(self.process_uploads())
        activity_id = self.client.get(f"/api/uploads/{gpx['id']}", headers=self.headers).json()["activity_id"]
        source_ids = (int(gpx["id"]), int(fit["id"]))
        with self.engine.connect() as db:
            job_id = db.execute(text("""SELECT job_id FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"""), {
                "account": self.account, "source": source_ids[0], "dataset": imported["dataset_id"],
            }).scalar_one()
        match_job = claim(self.engine, kind="match_coverage")
        self.assertEqual(match_job["id"], job_id)

        inserted, release, delete_lock_requested = threading.Event(), threading.Event(), threading.Event()
        owner_key = f"city-runner:owner:{self.account}"
        errors = []
        original_insert = coverage_module._insert_batch

        def pause_after_insert(*args, **kwargs):
            original_insert(*args, **kwargs)
            inserted.set()
            if not release.wait(5):
                raise RuntimeError("coverage delete test timed out")

        def observe_owner_lock(_connection, _cursor, statement, parameters, _context, _executemany):
            if "pg_advisory_xact_lock(" in statement and owner_key in str(parameters):
                delete_lock_requested.set()

        def run_match():
            try:
                coverage_module.process_source_dataset(self.engine, match_job)
            except BaseException as exc:
                errors.append(exc)

        event.listen(self.engine, "before_cursor_execute", observe_owner_lock)
        try:
            with patch.object(coverage_module, "_insert_batch", side_effect=pause_after_insert):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    matcher = pool.submit(run_match)
                    self.assertTrue(inserted.wait(5))
                    deleting = pool.submit(self.client.delete,
                        f"/api/activities/{activity_id}/sources/{source_ids[0]}", headers=self.headers)
                    self.assertTrue(delete_lock_requested.wait(5))
                    self.assertFalse(deleting.done())
                    release.set()
                    matcher.result(timeout=10)
                    deleted = deleting.result(timeout=10)
        finally:
            release.set()
            event.remove(self.engine, "before_cursor_execute", observe_owner_lock)
        self.assertEqual(errors, [])
        self.assertEqual(deleted.status_code, 204, deleted.text)
        remaining_job = claim(self.engine, kind="match_coverage")
        self.assertIsNotNone(remaining_job)
        coverage_module.process_source_dataset(self.engine, remaining_job)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": source_ids[0],
            }).scalar_one(), 0)
            self.assertGreater(db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": source_ids[1],
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT processed FROM activities WHERE user_id=:account AND id=:id"), {
                "account": self.account, "id": activity_id,
            }).scalar_one(), True)
            self.assertEqual(db.execute(text("""SELECT status FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"""), {
                "account": self.account, "source": source_ids[1], "dataset": imported["dataset_id"],
            }).scalar_one(), "succeeded")
