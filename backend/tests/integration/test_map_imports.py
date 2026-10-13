import json
import os
import re
import tempfile
import time
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.geography import import_osm_xml
from backend.app.coverage import queue_source_coverage
from backend.app.owner_guard import owner_lock
from backend.app.map_imports import (
    _active_dataset_fence, _ensure_backfill_task, claim_map_task, get_map_task,
    lock_active_datasets_for_enqueue,
    process_map_task_step, submit_osm_import,
)
from backend.tests.integration.test_map_api import _city_fixture


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class MapImportPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("map import tests require the generated disposable PostGIS database")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(cls.config, "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.region = f"map-task-{uuid.uuid4().hex}"
        self.extra_regions = []
        self.account = f"map-task-owner-{uuid.uuid4().hex}"
        self.next_activity_id = 7_000_000_000_000_000 + int(uuid.uuid4().hex[:10], 16)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "objects"
        self.root.mkdir(mode=0o700)
        self.previous_storage_root = os.environ.get("MAP_IMPORT_STORAGE_ROOT")
        os.environ["MAP_IMPORT_STORAGE_ROOT"] = str(self.root)
        self.osm = Path(self.tmp.name) / "city.osm"
        self.osm.write_bytes(_city_fixture())
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": self.account})

    def tearDown(self):
        regions = [self.region, *self.extra_regions]
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM map_import_tasks WHERE region=ANY(:regions)"), {
                "regions": regions,
            })
            db.execute(text("DELETE FROM activities WHERE user_id=:id"), {"id": self.account})
            db.execute(text("DELETE FROM accounts WHERE id=:id"), {"id": self.account})
            db.execute(text("DELETE FROM map_datasets WHERE region=ANY(:regions)"), {"regions": regions})
        self.tmp.cleanup()
        if self.previous_storage_root is None:
            os.environ.pop("MAP_IMPORT_STORAGE_ROOT", None)
        else:
            os.environ["MAP_IMPORT_STORAGE_ROOT"] = self.previous_storage_root

    def _add_sources(self, count):
        inserted = []
        with self.engine.begin() as db:
            for index in range(count):
                activity_id = self.next_activity_id
                self.next_activity_id += 1
                db.execute(text("""INSERT INTO activities
                    (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                    VALUES (:id,:owner,'Synthetic import source','2026-10-13','run',false,0,
                      CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                    "id": activity_id, "owner": self.account,
                    "tracks": json.dumps([[[0.5, 0.5], [0.6, 0.5]]]),
                    "timestamps": json.dumps([[None, None]]),
                })
                source_id = db.execute(text("""INSERT INTO activity_sources
                    (account_id,activity_id,source_kind,status)
                    VALUES (:owner,:activity,'gpx','succeeded') RETURNING id"""), {
                    "owner": self.account, "activity": activity_id,
                }).scalar_one()
                inserted.append((activity_id, int(source_id)))
        return inserted

    def test_submit_is_idempotent_and_worker_activates_complete_import(self):
        before_activation = self._add_sources(1)
        first = submit_osm_import(
            self.engine, self.osm, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="complete", coverage_evidence="Synthetic complete fixture",
            storage_root=self.root,
        )
        replay = submit_osm_import(
            self.engine, self.osm, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="complete", coverage_evidence="Synthetic complete fixture",
            storage_root=self.root,
        )
        self.assertEqual(first["id"], replay["id"])
        with self.engine.connect() as db:
            key = db.execute(text("SELECT object_key FROM map_import_tasks WHERE id=:id"), {
                "id": int(first["id"]),
            }).scalar_one()
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_import_tasks SET available_at=clock_timestamp()-interval '100 years' "
                            "WHERE id=:id"), {
                "id": int(first["id"]),
            })
        self.assertEqual(list(self.root.glob("*.osm")), [self.root / key])

        self.assertTrue(process_map_task_step(self.engine))
        with self.engine.connect() as db:
            task = db.execute(text("SELECT status,phase,dataset_id FROM map_import_tasks WHERE id=:id"), {
                "id": int(first["id"]),
            }).mappings().one()
            dataset = db.execute(text("SELECT status FROM map_datasets WHERE id=:id"), {
                "id": task["dataset_id"],
            }).scalar_one()
        self.assertEqual((task["status"], task["phase"], dataset), ("backfilling", "backfill", "active"))
        self.assertTrue(process_map_task_step(self.engine))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM coverage_source_runs "
                "WHERE dataset_id=:dataset AND source_id=:source"), {
                    "dataset": task["dataset_id"], "source": before_activation[0][1],
                }).scalar_one(), 1)
        after_activation = self._add_sources(1)[0]
        with self.engine.begin() as db:
            owner_lock(db, self.account)
            queue_source_coverage(db, account_id=self.account, source_id=after_activation[1],
                source_revision=1, activity_id=after_activation[0])
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM coverage_source_runs "
                "WHERE dataset_id=:dataset AND source_id=:source"), {
                    "dataset": task["dataset_id"], "source": after_activation[1],
                }).scalar_one(), 1)

    def test_source_backfill_releases_lease_budget_between_pages(self):
        self._add_sources(7)
        imported = import_osm_xml(
            self.engine, self.osm, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-13T00:00:00Z", coverage_mode="complete",
            coverage_evidence="Synthetic complete fixture",
        )
        dataset_id = imported["dataset_id"]
        with self.engine.connect() as db:
            task_id = db.execute(text("SELECT id FROM map_import_tasks WHERE task_type='coverage_backfill' "
                                      "AND dataset_id=:dataset"), {"dataset": dataset_id}).scalar_one()
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_import_tasks SET available_at=clock_timestamp()-interval '100 years' "
                            "WHERE id=:id"), {
                "id": task_id,
            })
        for _ in range(1000):
            process_map_task_step(self.engine, max_sources=1)
            with self.engine.connect() as db:
                state = db.execute(text("SELECT status FROM map_import_tasks WHERE id=:id"), {
                    "id": task_id,
                }).scalar_one()
            if state == "succeeded":
                break
        with self.engine.connect() as db:
            state = db.execute(text("SELECT status,attempts FROM map_import_tasks WHERE id=:id"), {
                "id": task_id,
            }).one()
        self.assertEqual(state.status, "succeeded")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM coverage_source_runs "
                "WHERE dataset_id=:dataset AND account_id=:owner"), {
                    "dataset": dataset_id, "owner": self.account,
                }).scalar_one(), 7)

    def test_stale_import_lease_cannot_commit_and_requeued_snapshot_succeeds(self):
        submitted = submit_osm_import(
            self.engine, self.osm, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="complete", coverage_evidence="Synthetic complete fixture",
            storage_root=self.root,
        )
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_import_tasks SET available_at=clock_timestamp()-interval '100 years' "
                            "WHERE id=:id"), {"id": int(submitted["id"])})
        claimed = claim_map_task(self.engine)
        self.assertEqual(int(claimed["id"]), int(submitted["id"]))
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_import_tasks SET leased_until=clock_timestamp()-interval '1 second' "
                            "WHERE id=:id"), {"id": int(submitted["id"])})
        snapshot_path = self.root / claimed["object_key"]
        with self.assertRaisesRegex(RuntimeError, "map_task_lease_lost"):
            import_osm_xml(self.engine, snapshot_path, region=self.region,
                city_relation_ids=[900], source_timestamp="2026-10-13T00:00:00Z",
                coverage_mode="complete",
                coverage_evidence="Synthetic complete fixture",
                _map_task=(int(submitted["id"]), claimed["lease_token"]),
                _expected_checksum=claimed["source_checksum"].strip())
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM map_datasets WHERE region=:region"), {
                "region": self.region,
            }).scalar_one(), 0)
        self.assertTrue(process_map_task_step(self.engine))
        completed = get_map_task(self.engine, int(submitted["id"]))
        self.assertEqual(completed["phase"], "backfill")
        self.assertIsNotNone(completed["dataset_id"])

    def test_failed_owner_checkpoint_rolls_back_cursor_and_retries(self):
        self._add_sources(1)
        imported = import_osm_xml(self.engine, self.osm, region=self.region,
            city_relation_ids=[900], source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="complete",
            coverage_evidence="Synthetic complete fixture")
        with self.engine.begin() as db:
            task_id = db.execute(text("SELECT id FROM map_import_tasks WHERE dataset_id=:dataset"), {
                "dataset": imported["dataset_id"],
            }).scalar_one()
            db.execute(text("UPDATE map_import_tasks SET available_at=clock_timestamp()-interval '100 years' "
                            "WHERE id=:id"), {"id": task_id})
        with patch("backend.app.coverage.queue_source_coverage", side_effect=RuntimeError("synthetic failure")):
            self.assertTrue(process_map_task_step(self.engine, max_sources=1))
        with self.engine.connect() as db:
            failed_page = db.execute(text("SELECT status,source_cursor,attempts FROM map_import_tasks "
                "WHERE id=:id"), {"id": task_id}).one()
        self.assertEqual(failed_page.status, "backfilling")
        self.assertIsNone(failed_page.source_cursor)
        self.assertEqual(failed_page.attempts, 1)
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_import_tasks SET available_at=clock_timestamp() WHERE id=:id"), {
                "id": task_id,
            })
        self.assertTrue(process_map_task_step(self.engine, max_sources=1))
        with self.engine.connect() as db:
            recovered = db.execute(text("SELECT source_cursor FROM map_import_tasks WHERE id=:id"), {
                "id": task_id,
            }).scalar_one()
            self.assertIsNotNone(recovered)

    def test_activation_and_source_enumeration_share_global_fence_in_both_orders(self):
        from sqlalchemy import event

        first_source = self._add_sources(1)[0]
        first_region = self.region + "-activation-first"
        self.extra_regions.append(first_region)
        first_import = import_osm_xml(self.engine, self.osm, region=first_region,
            city_relation_ids=[900], source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="sampled")
        first_dataset = first_import["dataset_id"]
        self.assertEqual(first_import["status"], "importing")
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_datasets SET coverage_mode='complete', "
                            "coverage_evidence='Synthetic complete fixture' WHERE id=:id"), {
                "id": first_dataset,
            })
        activation_holds = threading.Event()
        release_activation = threading.Event()
        shared_called = threading.Event()
        enumerated = threading.Event()
        enumeration_stmt = "SELECT id FROM map_datasets"

        def observe_query(conn, cursor, statement, parameters, context, executemany):
            if enumeration_stmt in statement:
                enumerated.set()

        def shared_fence(db):
            shared_called.set()
            lock_active_datasets_for_enqueue(db)

        event.listen(self.engine, "before_cursor_execute", observe_query)
        try:
            def activate_first():
                with self.engine.begin() as db:
                    _active_dataset_fence(db, exclusive=True)
                    db.execute(text("UPDATE map_datasets SET status='active' WHERE id=:id"), {
                        "id": first_dataset,
                    })
                    _ensure_backfill_task(db, first_dataset)
                    activation_holds.set()
                    if not release_activation.wait(5):
                        raise TimeoutError("activation test release timed out")

            def queue_after_activation():
                with self.engine.begin() as db:
                    owner_lock(db, self.account)
                    queue_source_coverage(db, account_id=self.account, source_id=first_source[1],
                        source_revision=1, activity_id=first_source[0])

            with ThreadPoolExecutor(max_workers=2) as pool:
                activation = pool.submit(activate_first)
                self.assertTrue(activation_holds.wait(3))
                with patch("backend.app.coverage.lock_active_datasets_for_enqueue",
                           side_effect=shared_fence):
                    queued = pool.submit(queue_after_activation)
                    self.assertTrue(shared_called.wait(3))
                    self.assertFalse(enumerated.is_set())
                    release_activation.set()
                    activation.result(timeout=5)
                    queued.result(timeout=5)
            with self.engine.connect() as db:
                self.assertEqual(db.execute(text("SELECT count(*) FROM coverage_source_runs "
                    "WHERE account_id=:owner AND source_id=:source AND dataset_id=:dataset"), {
                        "owner": self.account, "source": first_source[1], "dataset": first_dataset,
                    }).scalar_one(), 1)
        finally:
            release_activation.set()
            event.remove(self.engine, "before_cursor_execute", observe_query)

        second_source = self._add_sources(1)[0]
        second_region = self.region + "-source-first"
        self.extra_regions.append(second_region)
        second_dataset = import_osm_xml(self.engine, self.osm, region=second_region,
            city_relation_ids=[900], source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="sampled")["dataset_id"]
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_datasets SET coverage_mode='complete', "
                            "coverage_evidence='Synthetic complete fixture' WHERE id=:id"), {
                "id": second_dataset,
            })
        source_holds = threading.Event()
        release_source = threading.Event()
        activation_attempt = threading.Event()
        activation_acquired = threading.Event()

        def queue_before_activation():
            with self.engine.begin() as db:
                owner_lock(db, self.account)
                queue_source_coverage(db, account_id=self.account, source_id=second_source[1],
                    source_revision=1, activity_id=second_source[0])
                source_holds.set()
                if not release_source.wait(5):
                    raise TimeoutError("source test release timed out")

        def activate_after_source():
            with self.engine.begin() as db:
                db.execute(text("SELECT set_config('application_name',:name,true)"), {
                    "name": f"region-activation-{uuid.uuid4().hex}",
                })
                activation_attempt.set()
                _active_dataset_fence(db, exclusive=True)
                activation_acquired.set()
                db.execute(text("UPDATE map_datasets SET status='active' WHERE id=:id"), {
                    "id": second_dataset,
                })
                _ensure_backfill_task(db, second_dataset)

        with ThreadPoolExecutor(max_workers=2) as pool:
            queued = pool.submit(queue_before_activation)
            self.assertTrue(source_holds.wait(3))
            activation = pool.submit(activate_after_source)
            self.assertTrue(activation_attempt.wait(3))
            deadline = time.monotonic() + 3
            waiting = False
            while time.monotonic() < deadline:
                with self.engine.connect() as db:
                    waiting = db.execute(text("SELECT 1 FROM pg_stat_activity "
                        "WHERE application_name LIKE 'region-activation-%' AND wait_event_type='Lock' "
                        "AND query ILIKE '%pg_advisory_xact_lock%' LIMIT 1")).first() is not None
                if waiting:
                    break
                time.sleep(0.01)
            self.assertTrue(waiting)
            self.assertFalse(activation_acquired.is_set())
            release_source.set()
            queued.result(timeout=5)
            activation.result(timeout=5)
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_import_tasks SET available_at=clock_timestamp()-interval '100 years' "
                            "WHERE dataset_id=:id"), {"id": second_dataset})
        self.assertTrue(process_map_task_step(self.engine, max_sources=20))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM coverage_source_runs "
                "WHERE account_id=:owner AND source_id=:source AND dataset_id=:dataset"), {
                    "owner": self.account, "source": second_source[1], "dataset": second_dataset,
                }).scalar_one(), 1)

    def test_claim_skips_expired_task_locked_by_another_worker(self):
        first_region, second_region = self.region + "-locked", self.region + "-candidate"
        first_dataset = import_osm_xml(self.engine, self.osm, region=first_region,
            city_relation_ids=[900], source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="sampled")["dataset_id"]
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_datasets SET coverage_mode='complete', "
                            "coverage_evidence='Synthetic complete fixture' WHERE id=:id"), {
                "id": first_dataset,
            })
        second_dataset = import_osm_xml(self.engine, self.osm, region=second_region,
            city_relation_ids=[900], source_timestamp="2026-10-13T00:00:00Z",
            coverage_mode="sampled")["dataset_id"]
        with self.engine.begin() as db:
            db.execute(text("UPDATE map_datasets SET coverage_mode='complete', "
                            "coverage_evidence='Synthetic complete fixture' WHERE id=:id"), {
                "id": second_dataset,
            })
        with self.engine.begin() as db:
            first = db.execute(text("""INSERT INTO map_import_tasks(task_type,phase,dataset_id,status,
                attempts,lease_token,leased_until) VALUES ('coverage_backfill','backfill',:dataset,
                'running',1,:token,clock_timestamp()-interval '1 second') RETURNING id"""), {
                "dataset": first_dataset, "token": uuid.uuid4().hex,
            }).scalar_one()
            second = db.execute(text("""INSERT INTO map_import_tasks(task_type,phase,dataset_id,status)
                VALUES ('coverage_backfill','backfill',:dataset,'queued') RETURNING id"""), {
                "dataset": second_dataset,
            }).scalar_one()
            db.execute(text("UPDATE map_import_tasks SET available_at=clock_timestamp()-interval '100 years' "
                            "WHERE id=:id"), {"id": second})
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            connection.execute(text("SELECT id FROM map_import_tasks WHERE id=:id FOR UPDATE"), {
                "id": first,
            }).first()
            with ThreadPoolExecutor(max_workers=1) as pool:
                claimed = pool.submit(claim_map_task, self.engine).result(timeout=3)
            self.assertEqual(int(claimed["id"]), second)
        finally:
            transaction.rollback()
            connection.close()
            with self.engine.begin() as db:
                db.execute(text("DELETE FROM map_datasets WHERE id IN (:first_dataset,:second_dataset)"), {
                    "first_dataset": first_dataset, "second_dataset": second_dataset,
                })
