"""Synthetic owner status, retries and transactional refresh revisions."""

import hashlib
import json
import os
import re
import threading
import unittest
import uuid
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
from backend.app.owner_guard import owner_lock
from backend.app.jobs import claim
from backend.app.coverage import requeue_dataset_coverage, process_source_dataset
from backend.app.corrections import delete_activity


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class SyncStatusPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("sync status tests require a disposable PostGIS database")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(config, "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.accounts = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.tokens = [uuid.uuid4().hex, uuid.uuid4().hex]
        with self.engine.begin() as db:
            for account, token in zip(self.accounts, self.tokens):
                db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": account})
                db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                    VALUES (:digest,:account,:expires)"""), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
                    "expires": datetime.now(timezone.utc) + timedelta(hours=1),
                })
        self.client = TestClient(create_app(self.engine))

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id=ANY(:accounts)"), {"accounts": self.accounts})
            db.execute(text("DELETE FROM accounts WHERE id=ANY(:accounts)"), {"accounts": self.accounts})
            db.execute(text("DELETE FROM map_datasets WHERE region LIKE :prefix"), {"prefix": self.accounts[0] + "%"})

    def headers(self, account=0):
        return {"Authorization": "Bearer " + self.tokens[account]}

    def status(self, account=0, **params):
        response = self.client.get("/api/sync/status", headers=self.headers(account), params=params)
        return response

    def add_source(self, account=0, kind="gpx", *, job_status="succeeded", source_status="succeeded",
                   activity_id=None, error=None):
        with self.engine.begin() as db:
            if activity_id is not None:
                db.execute(text("""INSERT INTO activities
                    (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                    VALUES (:id,:account,'Synthetic','2025-04-05','run',true,0,'[]'::json,'[]'::json)"""), {
                    "id": activity_id, "account": self.accounts[account],
                })
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,revision,status,last_error)
                VALUES (:account,:activity,:kind,1,:source_status,:error) RETURNING id"""), {
                "account": self.accounts[account], "activity": activity_id, "kind": kind,
                "source_status": source_status, "error": error,
            }).scalar_one()
            job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts,last_error)
                VALUES (:account,'process_upload',:key,'{}'::jsonb,:status,
                  CASE WHEN CAST(:status AS varchar)='queued' THEN 0 ELSE 1 END,5,:error) RETURNING id"""), {
                "account": self.accounts[account], "key": f"upload:{source_id}:r1",
                "status": job_status, "error": error,
            }).scalar_one()
        return int(source_id), int(job_id)

    def add_dataset(self, *, active=True):
        with self.engine.begin() as db:
            return int(db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'sync-test','selection','complete',
                  'synthetic test fixture',:status) RETURNING id"""), {
                "region": self.accounts[0] + "-region", "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                "status": "active" if active else "importing",
            }).scalar_one())

    def add_failed_coverage(self, source_id):
        account = self.accounts[0]
        dataset_id = self.add_dataset()
        with self.engine.begin() as db:
            payload = json.dumps({"source_id": source_id, "source_revision": 1, "dataset_id": dataset_id})
            job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts,last_error)
                VALUES (:account,'match_coverage',:dedupe,CAST(:payload AS jsonb),'failed',5,5,'coverage_failed')
                RETURNING id"""), {
                "account": account, "dedupe": f"source:{source_id}:r1:dataset:{dataset_id}",
                "payload": payload,
            }).scalar_one()
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,last_error)
                VALUES (:account,:source,1,:dataset,:job,'failed','coverage_failed')"""), {
                "account": account, "source": source_id, "dataset": dataset_id, "job": job_id,
            })
        return int(dataset_id), int(job_id)

    def test_status_aggregates_and_account_bound_unchanged_token(self):
        self.add_source(0, "gpx", job_status="queued", source_status="queued")
        fit_source, _ = self.add_source(0, "fit", activity_id=900001)
        with self.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET updated_at='2099-01-01T00:00:00Z' WHERE id=:id"), {
                "id": fit_source,
            })
        full = self.status()
        self.assertEqual(full.status_code, 200, full.text)
        payload = full.json()
        self.assertEqual(payload["activity_count"], 1)
        self.assertEqual(payload["oldest_activity_date"], "2025-04-05")
        self.assertEqual(payload["files"]["gpx"]["queued"], 1)
        self.assertEqual(payload["files"]["fit"]["imported"], 1)
        self.assertEqual(payload["coverage"]["unavailable"], 1)
        self.assertIsNotNone(payload["last_import_at"])
        self.assertLess(payload["last_import_at"], "2099-01-01T00:00:00+00:00")
        self.assertFalse(payload["providers"][0]["available"])
        self.assertIn("no-store", full.headers["cache-control"])
        # Same numeric revisions must not let one account reuse another's token.
        other = self.status(1, since=payload["change_token"])
        self.assertEqual(other.status_code, 200, other.text)
        statements = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", observe)
        unchanged = self.status(0, since=payload["change_token"])
        event.remove(self.engine, "before_cursor_execute", observe)
        self.assertEqual(unchanged.status_code, 204)
        self.assertEqual(unchanged.content, b"")
        self.assertFalse(any("FROM activities" in statement or "FROM import_batch_items" in statement
                             for statement in statements))
        restarted_client = TestClient(create_app(self.engine))
        try:
            restarted = restarted_client.get("/api/sync/status", headers=self.headers(),
                                             params={"since": payload["change_token"]})
            self.assertEqual(restarted.status_code, 204)
        finally:
            restarted_client.close()

    def test_missing_gps_ingestion_and_wrong_coverage_provenance_stay_unavailable_or_pending(self):
        no_gps_source, no_gps_job = self.add_source(0, "fit", source_status="succeeded")
        missing_job_source, missing_job = self.add_source(0, "gpx", activity_id=900011)
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM jobs WHERE id=:id"), {"id": missing_job})
        bad_match_source, _ = self.add_source(0, "fit", activity_id=900012)
        dataset_id, bad_match_job = self.add_failed_coverage(bad_match_source)
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET payload=jsonb_set(payload,'{dataset_id}','999999'::jsonb) WHERE id=:id"), {
                "id": bad_match_job,
            })
        status = self.status().json()
        self.assertEqual(status["files"]["fit"]["unavailable"], 1)
        self.assertEqual(status["coverage"]["pending"], 1)
        self.assertEqual(status["coverage"]["failed"], 0)
        failures = self.client.get("/api/sync/failures", headers=self.headers()).json()
        self.assertFalse(any(item["source_id"] == str(bad_match_source) for item in failures["items"]))
        self.assertEqual(self.client.post(
            f"/api/uploads/{bad_match_source}/retry-coverage", headers=self.headers()).status_code, 204)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE id=:id"), {
                "id": bad_match_job,
            }).scalar_one(), "failed")

    def test_status_work_budget_returns_safe_503(self):
        with patch("backend.app.sync_status.STATUS_WORK_BUDGET_SECONDS", 0):
            response = self.status()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Sync status exceeded its work limit."})

    def test_failure_page_and_coverage_retry_are_current_and_idempotent(self):
        source_id, _ = self.add_source(0, "fit", activity_id=900002)
        with self.engine.begin() as db:
            db.execute(text("UPDATE activities SET processed=TRUE WHERE id=900002"))
        dataset_id, match_job_id = self.add_failed_coverage(source_id)
        # Current failed-job truth must win over a stale succeeded run marker.
        with self.engine.begin() as db:
            db.execute(text("UPDATE coverage_source_runs SET status='succeeded' WHERE source_id=:source"), {
                "source": source_id,
            })
        status = self.status().json()
        self.assertEqual(status["coverage"]["failed"], 1)
        page = self.client.get("/api/sync/failures", headers=self.headers())
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.headers["cache-control"], "no-store, private")
        self.assertEqual(page.json()["items"][0]["stage"], "coverage")
        self.assertEqual(page.json()["items"][0]["source_id"], str(source_id))
        self.assertEqual(self.client.get("/api/sync/failures", headers=self.headers(1)).json()["total"], 0)
        token = status["change_token"]
        retried = self.client.post(f"/api/uploads/{source_id}/retry-coverage", headers=self.headers())
        self.assertEqual(retried.status_code, 204, retried.text)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE id=:id"), {"id": match_job_id}).scalar_one(), "queued")
            self.assertEqual(db.execute(text("SELECT status FROM coverage_source_runs WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"), {
                "account": self.accounts[0], "source": source_id, "dataset": dataset_id,
            }).scalar_one(), "queued")
            self.assertFalse(db.execute(text("SELECT processed FROM activities WHERE id=900002")).scalar_one())
        self.assertNotEqual(self.status().json()["change_token"], token)
        # A new API worker can claim and finish the queued retry after restart.
        retry_worker = claim(self.engine, kind="match_coverage")
        self.assertIsNotNone(retry_worker)
        process_source_dataset(self.engine, retry_worker)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM coverage_source_runs WHERE source_id=:source"), {
                "source": source_id,
            }).scalar_one(), "succeeded")
        after = self.status().json()["change_token"]
        self.assertEqual(self.client.post(f"/api/uploads/{source_id}/retry-coverage", headers=self.headers()).status_code, 204)
        self.assertEqual(self.status().json()["change_token"], after)

    def test_retry_worker_requeue_and_deletion_interleave_without_resurrection(self):
        source_id, _ = self.add_source(0, "fit", activity_id=900003)
        dataset_id, match_job_id = self.add_failed_coverage(source_id)
        retry_locked = threading.Event()
        release_retry = threading.Event()

        def pause_after_retry_job_lock(conn, cursor, statement, parameters, context, executemany):
            if "ORDER BY job.id FOR UPDATE OF job" in statement:
                retry_locked.set()
                if not release_retry.wait(5):
                    raise TimeoutError("retry interleaving did not resume")

        event.listen(self.engine, "after_cursor_execute", pause_after_retry_job_lock)
        retry_client = TestClient(self.client.app)
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                retry_future = pool.submit(retry_client.post,
                    f"/api/uploads/{source_id}/retry-coverage", headers=self.headers())
                self.assertTrue(retry_locked.wait(2))
                requeue_future = pool.submit(requeue_dataset_coverage, self.engine, dataset_id,
                                             batch_size=1, max_sources=1)
                delete_future = pool.submit(delete_activity, self.engine, self.accounts[0], 900003)
                release_retry.set()
                retried = retry_future.result(timeout=5)
                self.assertEqual(retried.status_code, 204, retried.text)
                requeue_future.result(timeout=5)
                self.assertTrue(delete_future.result(timeout=5))
        finally:
            release_retry.set()
            event.remove(self.engine, "after_cursor_execute", pause_after_retry_job_lock)
            retry_client.close()
        with self.engine.connect() as db:
            self.assertIsNone(db.execute(text("SELECT id FROM activity_sources WHERE id=:source"), {
                "source": source_id,
            }).scalar_one_or_none())
            self.assertEqual(db.execute(text("SELECT count(*) FROM coverage_source_runs WHERE source_id=:source"), {
                "source": source_id,
            }).scalar_one(), 0)

    def test_ingestion_failure_is_reported_and_unimported_coverage_retry_conflicts(self):
        source_id, _ = self.add_source(0, "gpx", job_status="failed", source_status="failed",
                                       error="private/path/exception detail")
        page = self.client.get("/api/sync/failures?page=1&page_size=1", headers=self.headers())
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["items"][0]["stage"], "import")
        self.assertEqual(page.json()["items"][0]["retry_action"], "retry-import")
        self.assertEqual(page.json()["items"][0]["error"], "processing_failed")
        self.assertEqual(self.client.post(f"/api/uploads/{source_id}/retry-coverage", headers=self.headers()).status_code, 409)

    def test_revision_rollback_noop_heartbeat_cleanup_and_geography(self):
        first = self.status().json()["change_token"]
        cleanup_id = self.add_cleanup_job()
        self.assertEqual(self.status().json()["change_token"], first)
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET status='failed',last_error='cleanup_failed' WHERE id=:id"), {"id": cleanup_id})
        self.assertEqual(self.status().json()["change_token"], first)

        source_id, job_id = self.add_source(0, "gpx", job_status="queued", source_status="queued")
        changed = self.status().json()["change_token"]
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET status='running',attempts=1,lease_token=:lease,leased_until=clock_timestamp()+interval '1 minute' WHERE id=:id"), {
                "id": job_id, "lease": uuid.uuid4().hex,
            })
        changed = self.status().json()["change_token"]
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET leased_until=leased_until+interval '1 minute',updated_at=clock_timestamp() WHERE id=:id"), {
                "id": job_id,
            })
        self.assertEqual(self.status().json()["change_token"], changed)
        try:
            with self.engine.begin() as db:
                db.execute(text("UPDATE jobs SET last_error='temporary_error' WHERE id=:id"), {"id": job_id})
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        self.assertEqual(self.status().json()["change_token"], changed)
        self.add_dataset(active=False)
        self.assertNotEqual(self.status().json()["change_token"], changed)
        self.assertEqual(self.status(1, since=changed).status_code, 200)

    def add_cleanup_job(self):
        with self.engine.begin() as db:
            return int(db.execute(text("""INSERT INTO jobs(account_id,kind,dedupe_key,payload)
                VALUES (:account,'delete_private_object',:key,'{}'::jsonb) RETURNING id"""), {
                "account": self.accounts[0], "key": uuid.uuid4().hex,
            }).scalar_one())

    def test_multi_owner_claim_coalesces_counters_and_leaves_no_events(self):
        before = [self.status(index).json()["change_token"] for index in range(2)]
        with self.engine.begin() as db:
            for account in self.accounts:
                db.execute(text("""INSERT INTO jobs
                    (account_id,kind,dedupe_key,payload,status,attempts,max_attempts,lease_token,leased_until)
                    VALUES (:account,'process_upload',:key,'{}'::jsonb,'running',1,3,:lease,
                      clock_timestamp()-interval '1 second')"""), {
                    "account": account, "key": uuid.uuid4().hex, "lease": uuid.uuid4().hex,
                })
        with self.engine.connect() as db:
            before_claim = db.execute(text("SELECT revision FROM sync_account_revisions WHERE account_id=ANY(:accounts) ORDER BY account_id"), {
                "accounts": self.accounts,
            }).scalars().all()
        self.assertIsNotNone(claim(self.engine))
        for account in range(2):
            self.assertNotEqual(self.status(account).json()["change_token"], before[account])
        with self.engine.connect() as db:
            revisions = db.execute(text("SELECT revision FROM sync_account_revisions WHERE account_id=ANY(:accounts) ORDER BY account_id"), {
                "accounts": self.accounts,
            }).scalars().all()
            pending_events = db.execute(text("SELECT count(*) FROM sync_revision_events")).scalar_one()
        self.assertEqual([int(v) for v in revisions], [int(v) + 1 for v in before_claim])
        self.assertEqual(pending_events, 0)

    def test_job_revision_flush_does_not_deadlock_account_delete(self):
        _, job_id = self.add_source(0, "gpx", job_status="queued", source_status="queued")
        locked = threading.Event()
        release = threading.Event()

        def job_writer():
            with self.engine.begin() as db:
                db.execute(text("SELECT id FROM jobs WHERE id=:id FOR UPDATE"), {"id": job_id}).first()
                db.execute(text("""UPDATE jobs SET status='running',attempts=1,lease_token=:lease,
                    leased_until=clock_timestamp()+interval '1 minute' WHERE id=:id"""), {
                    "id": job_id, "lease": uuid.uuid4().hex,
                })
                locked.set()
                if not release.wait(5):
                    raise TimeoutError("delete did not reach job lock")

        def delete_account():
            with self.engine.begin() as db:
                owner_lock(db, self.accounts[0], exclusive=True)
                db.execute(text("SELECT id FROM accounts WHERE id=:account FOR UPDATE"), {
                    "account": self.accounts[0],
                }).first()
                account_locked.set()
                db.execute(text("DELETE FROM accounts WHERE id=:account"), {"account": self.accounts[0]})

        account_locked = threading.Event()

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(job_writer)
            self.assertTrue(locked.wait(2))
            deleter = pool.submit(delete_account)
            self.assertTrue(account_locked.wait(2))
            release.set()
            writer.result(timeout=5)
            deleter.result(timeout=5)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM sync_account_revisions WHERE account_id=:account"), {
                "account": self.accounts[0],
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT count(*) FROM sync_revision_events WHERE account_id=:account"), {
                "account": self.accounts[0],
            }).scalar_one(), 0)
