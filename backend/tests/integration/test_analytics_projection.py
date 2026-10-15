"""Cross-database publication, role isolation and invalidation races."""

import os
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from backend.app.analytics_datasets import read_snapshot
from backend.app.analytics_projection import current_projection, refresh_projection, register_account
from backend.app.analytics_store import initialize_store, owner_key, provision_role, remove_owner, role_name, role_password, stage_snapshot
from backend.app.owner_guard import owner_lock
from backend.tests.integration import test_activity_impact as fixture_module
from backend.tests.integration.test_explorer import TINY_NODES

SECRET = b"synthetic-projection-secret-32-bytes"


@unittest.skipUnless(os.getenv("POSTGIS_TEST_DATABASE_URL"), "run with scripts/dev/postgis-test.sh")
class AnalyticsProjectionPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_module.ActivityImpactPostgisTests.setUpClass()
        url = make_url(os.environ["POSTGIS_TEST_DATABASE_URL"])
        cls.admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        cls.target_database = "city_runner_analytics_test_" + uuid.uuid4().hex[:12]
        with cls.admin.connect() as db:
            db.exec_driver_sql(f'CREATE DATABASE "{cls.target_database}"')
        cls.target_url = url.set(database=cls.target_database)
        cls.target = create_engine(cls.target_url)
        initialize_store(cls.target)

    @classmethod
    def tearDownClass(cls):
        cls.target.dispose()
        with cls.admin.connect() as db:
            db.exec_driver_sql(f'DROP DATABASE "{cls.target_database}" WITH (FORCE)')
        cls.admin.dispose()
        fixture_module.ActivityImpactPostgisTests.tearDownClass()

    def setUp(self):
        self.fixture = fixture_module.ActivityImpactPostgisTests(methodName="runTest")
        self.fixture.setUp()
        f = self.fixture
        f.add_activity(fixture_module.BASE_ID, "2026-10-01")
        f.add_source(fixture_module.BASE_ID, TINY_NODES)
        with f.engine.begin() as db:
            register_account(db, f.account)
            db.execute(text("UPDATE analytics_accounts SET selected_dataset_id=:dataset WHERE account_id=:account"), {
                "dataset": f.dataset, "account": f.account,
            })

    def tearDown(self):
        f = self.fixture
        with self.target.begin() as db:
            for account in (f.account, f.other):
                remove_owner(db, owner_key(account))
                db.execute(text("DELETE FROM control.owners WHERE owner_key=:owner"), {"owner": owner_key(account)})
        with f.engine.begin() as db:
            db.execute(text("DELETE FROM analytics_accounts WHERE account_id IN (:a,:b)"), {"a": f.account, "b": f.other})
        f.tearDown()

    def publish(self):
        f = self.fixture
        return refresh_projection(f.engine, self.target, f.account, SECRET)

    def current(self):
        with self.fixture.engine.begin() as db:
            return current_projection(db, self.target, self.fixture.account)

    def test_role_cannot_cross_owner_or_reach_control_and_source_changes_withhold(self):
        f = self.fixture
        self.assertTrue(self.publish())
        self.assertIsNotNone(self.current())
        original = f.account
        try:
            f.account = f.other
            f.add_activity(fixture_module.BASE_ID + 1, "2026-10-02")
            f.add_source(fixture_module.BASE_ID + 1, TINY_NODES)
            with f.engine.begin() as db:
                register_account(db, f.account)
            self.publish()
        finally:
            f.account = original
        owner = owner_key(f.account)
        reader = create_engine(self.target_url.set(host=self.target_url.host or "127.0.0.1",
            port=self.target_url.port or 5432, username=role_name(owner), password=role_password(SECRET, owner)))
        try:
            with reader.connect() as db:
                self.assertEqual(db.execute(text("SELECT activity_id FROM analytics.activities")).scalars().all(), [fixture_module.BASE_ID])
                db.execute(text("SELECT set_config('city_runner.account_id',:other,false)"), {"other": owner_key(f.other)})
                self.assertEqual(db.execute(text("SELECT activity_id FROM analytics.activities")).scalars().all(), [fixture_module.BASE_ID])
            def read_during_stage(*args, **kwargs):
                stage_snapshot(*args, **kwargs)
                with reader.connect() as read:
                    read.exec_driver_sql("SET statement_timeout='250ms'")
                    self.assertEqual(read.execute(text("SELECT count(*) FROM analytics.activities")).scalar_one(), 1)
            with patch("backend.app.analytics_projection.stage_snapshot", side_effect=read_during_stage):
                self.assertTrue(self.publish())
            for sql in ("SELECT * FROM control.owners", "CREATE TABLE public.unwanted(id int)",
                        "UPDATE analytics.activities SET distance_m=1", "SET ROLE postgres"):
                with self.assertRaises(DBAPIError), reader.begin() as db:
                    db.exec_driver_sql(sql)
        finally:
            reader.dispose()
        with f.engine.begin() as db:
            db.execute(text("UPDATE activities SET name='corrected' WHERE id=:id"), {"id": fixture_module.BASE_ID})
        self.assertIsNone(self.current())
        self.assertTrue(self.publish())
        self.assertIsNotNone(self.current())

    def test_source_edit_between_snapshot_and_publish_discards_entire_stage(self):
        f = self.fixture
        self.publish()
        with self.target.connect() as db:
            original = db.execute(text("SELECT generation FROM control.owners WHERE owner_key=:owner"), {"owner": owner_key(f.account)}).scalar_one()
        def racing_snapshot(*args, **kwargs):
            result = read_snapshot(*args, **kwargs)
            with f.engine.begin() as db:
                db.execute(text("UPDATE activities SET name='racing correction' WHERE id=:id"), {"id": fixture_module.BASE_ID})
            return result
        with patch("backend.app.analytics_projection.read_snapshot", side_effect=racing_snapshot):
            with self.assertRaises(HTTPException) as raised:
                self.publish()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIsNone(self.current())
        with self.target.connect() as db:
            self.assertEqual(db.execute(text("SELECT generation FROM control.owners WHERE owner_key=:owner"), {"owner": owner_key(f.account)}).scalar_one(), original)

    def test_deleted_account_cannot_publish_and_external_generation_mismatch_fails_closed(self):
        f = self.fixture
        self.publish()
        with self.target.begin() as db:
            db.execute(text("UPDATE control.owners SET generation=:generation WHERE owner_key=:owner"), {"generation": uuid.uuid4(), "owner": owner_key(f.account)})
        self.assertIsNone(self.current())
        def deleted_snapshot(*args, **kwargs):
            result = read_snapshot(*args, **kwargs)
            with f.engine.begin() as db:
                db.execute(text("UPDATE analytics_accounts SET state='deleting' WHERE account_id=:account"), {"account": f.account})
                db.execute(text("DELETE FROM activities WHERE user_id=:account"), {"account": f.account})
                db.execute(text("DELETE FROM accounts WHERE id=:account"), {"account": f.account})
            return result
        with patch("backend.app.analytics_projection.read_snapshot", side_effect=deleted_snapshot):
            with self.assertRaises(HTTPException):
                self.publish()
        with f.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT state FROM analytics_accounts WHERE account_id=:account"), {"account": f.account}).scalar_one(), "deleting")

    def test_failure_fence_survives_new_projection(self):
        f = self.fixture
        self.publish()
        with self.target.begin() as db:
            db.execute(text("UPDATE control.owners SET fenced=true WHERE owner_key=:owner"), {"owner": owner_key(f.account)})
        self.assertIsNone(self.current())
        self.publish()
        self.assertIsNone(self.current())

    def test_role_setup_keeps_account_deletion_outside_its_commit(self):
        f = self.fixture
        def guarded_setup(*args, **kwargs):
            with self.assertRaises(DBAPIError), f.engine.begin() as deletion:
                deletion.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                owner_lock(deletion, f.account, exclusive=True)
            return provision_role(*args, **kwargs)
        with patch("backend.app.analytics_projection.provision_role", side_effect=guarded_setup):
            self.assertTrue(self.publish())

    def test_final_validation_fences_source_and_geography_commits(self):
        f = self.fixture
        self.publish()
        with f.engine.begin() as validation:
            self.assertIsNotNone(current_projection(validation, self.target, f.account))
            with self.assertRaises(DBAPIError), f.engine.begin() as writer:
                writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                owner_lock(writer, f.account)
                writer.execute(text("UPDATE activities SET name='blocked' WHERE id=:id"), {"id": fixture_module.BASE_ID})
            with self.assertRaises(DBAPIError), f.engine.begin() as geography:
                geography.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                geography.exec_driver_sql("UPDATE sync_geography_revision SET revision=revision+1 WHERE singleton=true")
        with f.engine.begin() as writer:
            owner_lock(writer, f.account)
            writer.execute(text("UPDATE activities SET name='committed' WHERE id=:id"), {"id": fixture_module.BASE_ID})
        self.assertIsNone(self.current())


if __name__ == "__main__":
    unittest.main()
