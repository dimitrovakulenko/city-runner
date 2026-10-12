"""PostGIS checks for upload-history statistics and bounded backfill fencing."""

from __future__ import annotations

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

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from backend.app.activity_statistics import backfill_activity_statistics
from backend.app.coverage import process_source_dataset, queue_source_coverage
from backend.app.corrections import delete_activity
from backend.app.jobs import claim
from backend.app.owner_guard import owner_lock


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "run with the disposable PostGIS integration harness")
class ActivityStatisticsPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("activity statistics tests require a disposable PostGIS database")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(cls.config, "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.account = str(uuid.uuid4())
        self.other_account = str(uuid.uuid4())
        self.token = uuid.uuid4().hex
        self.dataset_ids = []
        with self.engine.begin() as db:
            for account, token in ((self.account, self.token),
                                   (self.other_account, uuid.uuid4().hex)):
                db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": account})
                db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                    VALUES (:digest,:account,:expires)"""), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
                    "expires": datetime.now(timezone.utc) + timedelta(hours=1),
                })

    def tearDown(self):
        with self.engine.begin() as db:
            if self.dataset_ids:
                db.execute(text("DELETE FROM map_datasets WHERE id=ANY(:ids)"), {
                    "ids": self.dataset_ids,
                })
            db.execute(text("DELETE FROM activities WHERE user_id IN (:a,:b)"), {
                "a": self.account, "b": self.other_account,
            })
            db.execute(text("DELETE FROM accounts WHERE id IN (:a,:b)"), {
                "a": self.account, "b": self.other_account,
            })

    def add_legacy_activity(self, account, activity_id, tracks, timestamps):
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,tracks,timestamps)
                VALUES (:id,:account,'Legacy','2025-01-01','run',CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                "id": activity_id, "account": account,
                "tracks": json.dumps(tracks, allow_nan=False),
                "timestamps": json.dumps(timestamps, allow_nan=False),
            })

    def test_signed_bigint_keyset_resume_is_idempotent_and_owner_scoped(self):
        negative_id = -9_007_199_254_740_993
        large_id = 9_223_372_036_854_770_000
        self.add_legacy_activity(self.account, negative_id, [[[0, 0], [0.001, 0]]],
                                 [["2026-01-01T01:00:00+01:00", "2026-01-01T00:00:10Z"]])
        self.add_legacy_activity(self.account, large_id, [[[True, 0], [1, 0]]],
                                 [["2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z"]])
        self.add_legacy_activity(self.other_account, 400, [[[0, 0], [1, 0]]],
                                 [["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"]])

        first = backfill_activity_statistics(self.engine, self.account, batch_size=1, max_activities=1)
        self.assertEqual(first, {"processed": 1, "unavailable": 0, "after_id": negative_id})
        with self.engine.connect() as db:
            first_row = db.execute(text("""SELECT distance_m,duration_s,pace_s_per_km,statistics_version
                FROM activities WHERE id=:id"""), {"id": negative_id}).one()
        self.assertAlmostEqual(first_row.distance_m, 111.1949266, places=4)
        self.assertEqual(first_row.duration_s, 10)
        self.assertEqual(first_row.statistics_version, 1)

        second = backfill_activity_statistics(self.engine, self.account, after_id=first["after_id"],
                                              batch_size=1, max_activities=1)
        self.assertEqual(second, {"processed": 1, "unavailable": 1, "after_id": large_id})
        self.assertEqual(backfill_activity_statistics(self.engine, self.account),
                         {"processed": 0, "unavailable": 0, "after_id": None})
        with self.engine.connect() as db:
            other_version = db.execute(text("SELECT statistics_version FROM activities WHERE id=400")).scalar_one()
            unavailable = db.execute(text("""SELECT distance_m,duration_s,pace_s_per_km,statistics_version
                FROM activities WHERE id=:id"""), {"id": large_id}).one()
        self.assertEqual(other_version, 0)
        self.assertEqual((unavailable.distance_m, unavailable.duration_s, unavailable.pace_s_per_km,
                          unavailable.statistics_version), (None, None, None, 1))

    def test_statistics_revision_is_idempotent_and_rolls_back_with_the_row(self):
        activity_id = -9_007_199_254_740_987
        self.add_legacy_activity(self.account, activity_id, [[[0, 0], [0.001, 0]]],
                                 [["2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z"]])

        def revision():
            with self.engine.connect() as db:
                return int(db.execute(text("""SELECT COALESCE(
                    (SELECT revision FROM sync_account_revisions WHERE account_id=:account),0)"""), {
                    "account": self.account,
                }).scalar_one())

        before = revision()
        result = backfill_activity_statistics(self.engine, self.account, max_activities=1)
        self.assertEqual(result["processed"], 1)
        calculated = revision()
        self.assertEqual(calculated, before + 1)
        self.assertEqual(backfill_activity_statistics(self.engine, self.account, max_activities=1)["processed"], 0)
        self.assertEqual(revision(), calculated)

        with self.engine.begin() as db:
            owner_lock(db, self.account)
            db.execute(text("UPDATE activities SET distance_m=distance_m WHERE id=:id"), {
                "id": activity_id,
            })
        self.assertEqual(revision(), calculated)

        with self.assertRaisesRegex(RuntimeError, "rollback synthetic stats update"):
            with self.engine.begin() as db:
                owner_lock(db, self.account)
                db.execute(text("UPDATE activities SET distance_m=distance_m+1 WHERE id=:id"), {
                    "id": activity_id,
                })
                raise RuntimeError("rollback synthetic stats update")
        self.assertEqual(revision(), calculated)

        with self.engine.begin() as db:
            owner_lock(db, self.account)
            db.execute(text("UPDATE activities SET distance_m=distance_m+1 WHERE id=:id"), {
                "id": activity_id,
            })
        changed = revision()
        self.assertEqual(changed, calculated + 1)
        self.assertTrue(delete_activity(self.engine, self.account, activity_id))
        with self.engine.connect() as db:
            self.assertIsNone(db.execute(text("SELECT id FROM activities WHERE id=:id"), {
                "id": activity_id,
            }).scalar_one_or_none())
        self.assertEqual(revision(), changed + 1)

    def test_backfill_and_concurrent_activity_deletion_do_not_resurrect(self):
        activity_id = -9_007_199_254_740_991
        self.add_legacy_activity(self.account, activity_id, [[[0, 0], [0.001, 0]]],
                                 [["2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z"]])
        row_locked = threading.Event()
        delete_waiting = threading.Event()
        release_backfill = threading.Event()

        def pause_backfill(conn, cursor, statement, parameters, context, executemany):
            if "SELECT tracks,timestamps,statistics_version" in statement:
                row_locked.set()
                if not release_backfill.wait(5):
                    raise TimeoutError("backfill interleaving did not resume")

        def observe_delete(conn, cursor, statement, parameters, context, executemany):
            if "SELECT id FROM activities" in statement and "FOR UPDATE" in statement:
                delete_waiting.set()

        event.listen(self.engine, "after_cursor_execute", pause_backfill)
        event.listen(self.engine, "before_cursor_execute", observe_delete)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                backfill = pool.submit(backfill_activity_statistics, self.engine, self.account,
                                       max_activities=1)
                self.assertTrue(row_locked.wait(2))
                deletion = pool.submit(delete_activity, self.engine, self.account, activity_id)
                self.assertTrue(delete_waiting.wait(2))
                release_backfill.set()
                self.assertEqual(backfill.result(timeout=5)["processed"], 1)
                self.assertTrue(deletion.result(timeout=5))
        finally:
            release_backfill.set()
            event.remove(self.engine, "after_cursor_execute", pause_backfill)
            event.remove(self.engine, "before_cursor_execute", observe_delete)
        with self.engine.connect() as db:
            self.assertIsNone(db.execute(text("SELECT id FROM activities WHERE id=:id"), {
                "id": activity_id,
            }).scalar_one_or_none())

    def test_backfill_serializes_with_coverage_worker_without_changing_gps_support(self):
        activity_id = -9_007_199_254_740_989
        self.add_legacy_activity(self.account, activity_id, [[[0, 0], [0.001, 0]]],
                                 [["2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z"]])
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'stats-test','selection','complete',
                  'synthetic statistics worker fixture','active') RETURNING id"""), {
                "region": "stats-" + uuid.uuid4().hex,
                "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            self.dataset_ids.append(int(dataset_id))
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,revision,status)
                VALUES (:account,:activity,'gpx',1,'succeeded') RETURNING id"""), {
                "account": self.account, "activity": activity_id,
            }).scalar_one()
            owner_lock(db, self.account)
            queued = queue_source_coverage(db, account_id=self.account, source_id=int(source_id),
                source_revision=1, activity_id=activity_id, dataset_ids=[int(dataset_id)])
        job = claim(self.engine, kind="match_coverage")
        self.assertEqual(int(job["id"]), int(queued["jobs"][0]["job_id"]))

        row_locked = threading.Event()
        worker_waiting = threading.Event()
        release_backfill = threading.Event()

        def pause_backfill(conn, cursor, statement, parameters, context, executemany):
            if "SELECT tracks,timestamps,statistics_version" in statement:
                row_locked.set()
                if not release_backfill.wait(5):
                    raise TimeoutError("backfill/coverage interleaving did not resume")

        def observe_worker(conn, cursor, statement, parameters, context, executemany):
            if "SELECT tracks FROM activities" in statement and "FOR SHARE" in statement:
                worker_waiting.set()

        event.listen(self.engine, "after_cursor_execute", pause_backfill)
        event.listen(self.engine, "before_cursor_execute", observe_worker)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                backfill = pool.submit(backfill_activity_statistics, self.engine, self.account,
                                       max_activities=1)
                self.assertTrue(row_locked.wait(2))
                worker = pool.submit(process_source_dataset, self.engine, job)
                self.assertTrue(worker_waiting.wait(2))
                release_backfill.set()
                self.assertEqual(backfill.result(timeout=5)["processed"], 1)
                worker.result(timeout=5)
        finally:
            release_backfill.set()
            event.remove(self.engine, "after_cursor_execute", pause_backfill)
            event.remove(self.engine, "before_cursor_execute", observe_worker)
        with self.engine.connect() as db:
            saved = db.execute(text("""SELECT distance_m,statistics_version,processed,unmapped_points
                FROM activities WHERE id=:id"""), {"id": activity_id}).one()
            support_count = db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": source_id,
            }).scalar_one()
            run_status = db.execute(text("""SELECT status FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"""), {
                "account": self.account, "source": source_id, "dataset": dataset_id,
            }).scalar_one()
        self.assertGreater(saved.distance_m, 0)
        self.assertEqual(saved.statistics_version, 1)
        self.assertTrue(saved.processed)
        self.assertEqual(run_status, "succeeded")
        self.assertEqual(support_count, 0)


if __name__ == "__main__":
    unittest.main()
