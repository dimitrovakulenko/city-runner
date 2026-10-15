"""Deletion receipts cover the external projection and private metadata cleanup."""

import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import text

from backend.app.account_data import delete_account_data, replay_account_deletions, verify_account_deletions
from backend.app.account_deletion_journal import initialize_ledger
from backend.app.analytics_config import AnalyticsConfig
from backend.app.analytics_lifecycle import fence_deletion, process_analytics_cleanup
from backend.app.analytics_store import owner_key, role_name
from backend.app.storage import LocalObjectStore
from backend.tests.integration import test_analytics_projection as fixture


@unittest.skipUnless(os.getenv("POSTGIS_TEST_DATABASE_URL"), "run with disposable PostGIS harness")
class AnalyticsLifecyclePostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AnalyticsProjectionPostgisTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixture.AnalyticsProjectionPostgisTests.tearDownClass()

    def setUp(self):
        self.projection = fixture.AnalyticsProjectionPostgisTests(methodName="runTest")
        self.projection.setUp()
        self.projection.publish()
        self.f = self.projection.fixture
        self.tmp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"ACCOUNT_DELETION_LEDGER": str(Path(self.tmp.name) / "deletions.jsonl")})
        self.environment.start()
        initialize_ledger()
        self.store = LocalObjectStore(Path(self.tmp.name) / "originals")
        self.config = AnalyticsConfig(self.projection.target, fixture.SECRET, "http://127.0.0.1:8091",
                                     "http://127.0.0.1:4183", "http://synthetic-superset.invalid")
        self.configured = patch.object(AnalyticsConfig, "from_env", return_value=self.config)
        self.configured.start()

    def tearDown(self):
        self.configured.stop()
        self.environment.stop()
        self.projection.tearDown()
        self.tmp.cleanup()

    def state(self):
        with self.f.engine.connect() as db:
            return db.execute(text("SELECT state FROM analytics_accounts WHERE account_id=:account"), {"account": self.f.account}).scalar_one()

    def test_metadata_outage_keeps_receipt_pending_and_retry_verifies_all_stores(self):
        receipt = delete_account_data(self.f.engine, self.f.account, self.store)
        self.assertEqual(receipt.status, "cleanup-pending")
        with patch("backend.app.analytics_lifecycle.private_call", side_effect=HTTPException(503, "offline")):
            self.assertTrue(process_analytics_cleanup(self.f.engine, self.config))
        self.assertEqual(self.state(), "deleting")
        with self.projection.target.connect() as db:
            self.assertIsNone(db.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {
                "role": role_name(owner_key(self.f.account)),
            }).first())
        self.assertFalse(verify_account_deletions(self.f.engine, self.store))
        with self.f.engine.begin() as db:
            db.execute(text("UPDATE analytics_accounts SET available_at=clock_timestamp() WHERE account_id=:account"), {"account": self.f.account})
        with patch("backend.app.analytics_lifecycle.private_call", side_effect=lambda _, owner, action: {
                "complete": True, "deleted": True}):
            process_analytics_cleanup(self.f.engine, self.config)
            self.assertEqual(self.state(), "deleted")
            self.assertTrue(verify_account_deletions(self.f.engine, self.store))
            self.assertEqual(replay_account_deletions(self.f.engine, self.store, force_cleanup=True), 1)
            self.assertEqual(self.state(), "deleting")
            self.assertFalse(verify_account_deletions(self.f.engine, self.store))
            process_analytics_cleanup(self.f.engine, self.config)
            self.assertTrue(verify_account_deletions(self.f.engine, self.store))

    def test_journal_poll_does_not_steal_active_cleanup_lease(self):
        lease = uuid.uuid4()
        delete_account_data(self.f.engine, self.f.account, self.store)
        with self.f.engine.begin() as db:
            db.execute(text("UPDATE analytics_accounts SET lease_token=:lease,leased_until=clock_timestamp()+interval '60 seconds' WHERE account_id=:account"), {
                "lease": lease, "account": self.f.account,
            })
            fence_deletion(db, self.f.account)
        with self.f.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT lease_token FROM analytics_accounts WHERE account_id=:account"), {"account": self.f.account}).scalar_one(), lease)
        self.assertFalse(process_analytics_cleanup(self.f.engine, self.config))


if __name__ == "__main__":
    unittest.main()
