import json
import os
import re
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.geography import import_osm_xml
from backend.app.map_imports import (
    _advance_backfill_source, claim_map_task, process_map_task_step, submit_osm_import,
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
        self.account = f"map-task-owner-{uuid.uuid4().hex}"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "objects"
        self.root.mkdir(mode=0o700)
        self.osm = Path(self.tmp.name) / "city.osm"
        self.osm.write_bytes(_city_fixture())
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": self.account})

    def tearDown(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM map_import_tasks WHERE region=:region OR dataset_id IN "
                            "(SELECT id FROM map_datasets WHERE region=:region)"), {"region": self.region})
            db.execute(text("DELETE FROM accounts WHERE id=:id"), {"id": self.account})
            db.execute(text("DELETE FROM map_datasets WHERE region=:region"), {"region": self.region})
        self.tmp.cleanup()

    def _add_sources(self, count):
        with self.engine.begin() as db:
            for index in range(count):
                activity_id = 7000000000000000 + index
                db.execute(text("""INSERT INTO activities
                    (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                    VALUES (:id,:owner,'Synthetic import source','2026-10-13','run',false,0,
                      CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                    "id": activity_id, "owner": self.account,
                    "tracks": json.dumps([[[0.5, 0.5], [0.6, 0.5]]]),
                    "timestamps": json.dumps([[None, None]]),
                })
                db.execute(text("""INSERT INTO activity_sources
                    (account_id,activity_id,source_kind,status)
                    VALUES (:owner,:activity,'gpx','succeeded')"""), {
                    "owner": self.account, "activity": activity_id,
                })

    def test_submit_is_idempotent_and_worker_activates_complete_import(self):
        first = submit_osm_import(
            self.engine, self.osm, region=self.region, city_relation_ids=[900],
            coverage_mode="complete", coverage_evidence="Synthetic complete fixture",
            storage_root=self.root,
        )
        replay = submit_osm_import(
            self.engine, self.osm, region=self.region, city_relation_ids=[900],
            coverage_mode="complete", coverage_evidence="Synthetic complete fixture",
            storage_root=self.root,
        )
        self.assertEqual(first["id"], replay["id"])
        with self.engine.connect() as db:
            key = db.execute(text("SELECT object_key FROM map_import_tasks WHERE id=:id"), {
                "id": int(first["id"]),
            }).scalar_one()
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
        lease = uuid.uuid4().hex
        with self.engine.begin() as db:
            db.execute(text("""UPDATE map_import_tasks SET status='running',attempts=1,
                lease_token=:lease,leased_until=clock_timestamp()+interval '1 hour'
                WHERE id=:id"""), {"id": task_id, "lease": lease})
        for _ in range(20):
            if not _advance_backfill_source(self.engine, task_id, lease):
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

    def test_claim_skips_expired_task_locked_by_another_worker(self):
        first_dataset, second_dataset = -int(uuid.uuid4().hex[:10], 16), -int(uuid.uuid4().hex[:10], 16)
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
                db.execute(text("DELETE FROM map_import_tasks WHERE id IN (:first,:second)"), {
                    "first": first, "second": second,
                })
