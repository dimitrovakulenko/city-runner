import os
import re
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.jobs import cancel, claim, complete, enqueue, extend_lease, fail
from backend.app.worker import run_once


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class DurableJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("jobs integration tests require the generated disposable test database")
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        try:
            command.upgrade(cls.config, "head")
            cls.engine = create_engine(DATABASE_URL)
        except BaseException:
            if cls.previous_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = cls.previous_database_url
            raise

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.account_id = f"job-test-{uuid.uuid4().hex}"
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts (id) VALUES (:id)"), {"id": self.account_id})

    def tearDown(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM accounts WHERE id=:id"), {"id": self.account_id})

    def add_job(self, key="one", *, kind="test.work", priority=100, max_attempts=5):
        return enqueue(self.engine, account_id=self.account_id, kind=kind, dedupe_key=key,
                       payload={"key": key}, priority=priority, max_attempts=max_attempts)

    def expire(self, job_id):
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET leased_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": job_id})

    def row(self, job_id):
        with self.engine.connect() as db:
            return dict(db.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": job_id}).mappings().one())

    def test_priority_dedupe_and_worker_runs_outside_claim_transaction(self):
        history = self.add_job("history", kind="history", priority=10)
        duplicate = enqueue(self.engine, account_id=self.account_id, kind="history", dedupe_key="history",
                            payload={"ignored": True}, priority=100)
        self.assertEqual(duplicate["id"], history["id"])
        new_work = self.add_job("new", priority=100)
        claimed = claim(self.engine)
        self.assertEqual(claimed["id"], new_work["id"])
        self.assertTrue(complete(self.engine, job_id=claimed["id"], lease_token=claimed["lease_token"]))
        self.assertFalse(complete(self.engine, job_id=claimed["id"], lease_token=claimed["lease_token"]))

        seen = []

        def handler(job):
            with self.engine.connect() as db:
                row = db.execute(text("SELECT status FROM jobs WHERE id=:id"), {"id": history["id"]}).mappings().one()
            seen.append((job["payload"], row["status"], job["account_id"], job["lease_token"]))

        self.assertTrue(run_once(self.engine, {"history": handler}))
        self.assertEqual(seen[0][:3], ({"key": "history"}, "running", self.account_id))
        self.assertTrue(seen[0][3])
        self.assertEqual(self.row(history["id"])["status"], "succeeded")

    def test_enqueue_uses_callers_transaction(self):
        try:
            with self.engine.begin() as db:
                job = enqueue(db, account_id=self.account_id, kind="test.rollback", dedupe_key="rollback", payload={})
                raise RuntimeError("rollback fixture")
        except RuntimeError as error:
            self.assertEqual(str(error), "rollback fixture")
        with self.engine.connect() as db:
            count = db.execute(text("SELECT count(*) FROM jobs WHERE id=:id"), {"id": job["id"]}).scalar_one()
        self.assertEqual(count, 0)

    def test_two_workers_never_claim_the_same_job(self):
        self.add_job("one")
        self.add_job("two")
        barrier = threading.Barrier(2)

        def claim_at_once():
            barrier.wait(timeout=5)
            job = claim(self.engine)
            return job["id"] if job else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: claim_at_once(), range(2)))
        self.assertEqual(len(set(claims)), 2)
        self.assertNotIn(None, claims)

    def test_expired_lease_recovery_fences_stale_acknowledgements(self):
        queued = self.add_job()
        first = claim(self.engine, lease_seconds=120)
        self.assertEqual(first["id"], queued["id"])
        self.expire(first["id"])
        second = claim(self.engine, lease_seconds=120)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["attempts"], 2)
        self.assertNotEqual(second["lease_token"], first["lease_token"])
        self.assertFalse(complete(self.engine, job_id=first["id"], lease_token=first["lease_token"]))
        before_extend = self.row(second["id"])["leased_until"]
        self.assertTrue(extend_lease(self.engine, job_id=second["id"], lease_token=second["lease_token"], lease_seconds=30))
        self.assertGreaterEqual(self.row(second["id"])["leased_until"], before_extend)
        self.expire(second["id"])
        self.assertFalse(extend_lease(self.engine, job_id=second["id"], lease_token=second["lease_token"]))

    def test_expired_recovery_is_bounded_and_skips_locked_rows(self):
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO jobs
                (account_id, kind, dedupe_key, status, attempts, lease_token, leased_until)
                SELECT :account_id, 'test.expired', n::text, 'running', 1,
                    lpad(to_hex(n), 32, '0'), clock_timestamp()-interval '1 second'
                FROM generate_series(1, 102) AS n"""), {"account_id": self.account_id})
            first_id, last_id = db.execute(text("""SELECT min(id), max(id) FROM jobs
                WHERE account_id=:account_id AND kind='test.expired'"""), {"account_id": self.account_id}).one()

        with self.engine.connect() as locked_db:
            transaction = locked_db.begin()
            locked_db.execute(text("SELECT id FROM jobs WHERE id=:id FOR UPDATE"), {"id": first_id})
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(claim, self.engine)
                claimed = future.result(timeout=5)
            transaction.rollback()

        self.assertIsNotNone(claimed)
        self.assertNotEqual(claimed["id"], first_id)
        with self.engine.connect() as db:
            still_expired = db.execute(text("""SELECT count(*) FROM jobs
                WHERE account_id=:account_id AND kind='test.expired' AND status='running'
                  AND leased_until <= clock_timestamp()"""), {"account_id": self.account_id}).scalar_one()
            last_status = db.execute(text("SELECT status FROM jobs WHERE id=:id"), {"id": last_id}).scalar_one()
        self.assertEqual(still_expired, 2)
        self.assertEqual(last_status, "running")

    def test_retry_backoff_exhaustion_and_permanent_failure(self):
        job = self.add_job(max_attempts=2)
        first = claim(self.engine)
        self.assertTrue(fail(self.engine, job_id=job["id"], lease_token=first["lease_token"], error_code="bad secret=xyz"))
        retried = self.row(job["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["last_error"], "handler_error")
        self.assertGreater((retried["available_at"] - retried["updated_at"]).total_seconds(), 0)
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET available_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": job["id"]})
        second = claim(self.engine)
        self.assertEqual(second["attempts"], 2)
        self.expire(job["id"])
        self.assertIsNone(claim(self.engine))
        exhausted = self.row(job["id"])
        self.assertEqual(exhausted["status"], "failed")
        self.assertEqual(exhausted["last_error"], "lease_expired")
        self.assertFalse(complete(self.engine, job_id=job["id"], lease_token=second["lease_token"]))

        permanent = self.add_job("permanent")
        claimed = claim(self.engine)
        self.assertEqual(claimed["id"], permanent["id"])
        self.assertTrue(fail(self.engine, job_id=permanent["id"], lease_token=claimed["lease_token"],
                             error_code="invalid_payload", permanent=True))
        self.assertEqual(self.row(permanent["id"])["status"], "failed")

    def test_cancel_is_account_scoped_and_invalidates_running_lease(self):
        job = self.add_job()
        self.assertFalse(cancel(self.engine, job_id=job["id"], account_id="another-account"))
        claimed = claim(self.engine)
        self.assertTrue(cancel(self.engine, job_id=job["id"], account_id=self.account_id))
        self.assertFalse(complete(self.engine, job_id=job["id"], lease_token=claimed["lease_token"]))
        self.assertFalse(cancel(self.engine, job_id=job["id"], account_id=self.account_id))
        self.assertEqual(self.row(job["id"])["status"], "cancelled")
        queued = self.add_job("queued")
        self.assertTrue(cancel(self.engine, job_id=queued["id"], account_id=self.account_id))
        self.assertEqual(self.row(queued["id"])["status"], "cancelled")

    def test_worker_logs_only_sanitized_error_code(self):
        job = self.add_job(kind="test.secret")

        def private_failure(_job):
            raise ValueError("access_token=private-value")

        with self.assertLogs("backend.app.worker", level="ERROR") as captured:
            self.assertTrue(run_once(self.engine, {"test.secret": private_failure}))
        self.assertNotIn("private-value", "\n".join(captured.output))
        self.assertEqual(self.row(job["id"])["last_error"], "valueerror")


if __name__ == "__main__":
    unittest.main()
