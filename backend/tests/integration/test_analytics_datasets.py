"""Analytics snapshot parity against existing synthetic PostGIS impact fixtures."""

import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import text

from backend.app.analytics_datasets import read_snapshot
from backend.tests.integration import test_activity_impact as impact_fixture
from backend.tests.integration.test_explorer import TINY_NODES, LONG_NODES

BASE_ID = impact_fixture.BASE_ID


@unittest.skipUnless(os.getenv("POSTGIS_TEST_DATABASE_URL"), "run with scripts/dev/postgis-test.sh")
class AnalyticsDatasetPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        impact_fixture.ActivityImpactPostgisTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        impact_fixture.ActivityImpactPostgisTests.tearDownClass()

    def setUp(self):
        self.fixture = impact_fixture.ActivityImpactPostgisTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def snapshot(self, dataset=True):
        f = self.fixture
        return read_snapshot(f.engine, f.account, f.dataset if dataset else None, b"synthetic-analytics-secret-32-bytes")

    def add(self, identifier, day, nodes=()):
        f = self.fixture
        f.add_activity(identifier, day)
        return f.add_source(identifier, nodes)

    def test_duplicate_formats_preserve_activity_grain_and_null_statistics(self):
        f = self.fixture
        self.add(BASE_ID, "2026-10-01", TINY_NODES)
        source, _ = f.add_source(BASE_ID, TINY_NODES)
        with f.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET source_kind='fit' WHERE id=:id"), {"id": source})
            db.execute(text("UPDATE activities SET distance_m=5000,duration_s=1800,pace_s_per_km=360,statistics_version=1 WHERE id=:id"), {"id": BASE_ID})
        self.add(BASE_ID + 1, None)
        snapshot = self.snapshot()
        activities = snapshot.tables["activities"]
        self.assertEqual(len(activities), 2)
        self.assertEqual(sum(row["distance_m"] or 0 for row in activities), 5000)
        self.assertTrue(activities[0]["has_gpx"] and activities[0]["has_fit"])
        self.assertIsNone(activities[1]["distance_m"])
        self.assertIsNone(activities[1]["recorded_date"])
        self.assertEqual(len(snapshot.tables["activity_nodes"]), len(set(TINY_NODES)))

    def test_overlap_history_matches_impact_and_period_union_is_not_sum(self):
        f = self.fixture
        self.add(BASE_ID, "2026-10-01", TINY_NODES[:2])
        self.add(BASE_ID + 1, "2026-10-02", TINY_NODES[1:])
        snapshot = self.snapshot()
        for row in snapshot.tables["exploration_history"]:
            impact = f.impact(row["activity_id"], rule=row["rule"]).json()
            for field in ("history_status", "supported_nodes", "new_nodes", "streets_advanced", "streets_completed"):
                self.assertEqual(row[field], impact[field], (row, field, impact))
        support = snapshot.tables["activity_nodes"]
        self.assertGreater(len(support), len({row["node_token"] for row in support}))
        self.assertEqual(len({row["node_token"] for row in support}), len(set(TINY_NODES)))
        self.assertTrue(all(len(row["node_token"]) == 64 for row in support))
        self.assertFalse(any("node_id" in row for row in support))

    def test_unknown_contributing_date_withholds_new_but_preserves_supported(self):
        self.add(BASE_ID, None, TINY_NODES[:1])
        self.add(BASE_ID + 1, "2026-10-02", TINY_NODES[1:])
        snapshot = self.snapshot()
        for row in snapshot.tables["exploration_history"]:
            self.assertEqual(row["history_status"], "unknown-dates")
            self.assertIsNotNone(row["supported_nodes"])
            self.assertIsNone(row["new_nodes"])
            self.assertIsNone(row["streets_completed"])

    def test_pending_geography_and_manual_completion_do_not_fabricate_gps(self):
        f = self.fixture
        self.add(BASE_ID, "2026-10-01", TINY_NODES[:1])
        with f.engine.begin() as db:
            db.execute(text("""INSERT INTO manual_street_completions(account_id,dataset_id,street_id,reason)
                VALUES (:account,:dataset,:street,'synthetic')"""), {
                "account": f.account, "dataset": f.dataset, "street": f.streets["Tiny Road"],
            })
            db.execute(text("UPDATE map_datasets SET status='importing' WHERE id=:id"), {"id": f.dataset})
        snapshot = self.snapshot()
        self.assertNotEqual(snapshot.tables["dataset_status"][0]["coverage_status"], "ready")
        self.assertEqual(snapshot.tables["activity_nodes"], [])
        for row in snapshot.tables["street_progress"]:
            self.assertIsNone(row["visited_nodes"])
            self.assertIsNone(row["eligible_nodes"])
            self.assertIsNone(row["threshold_nodes"])
            self.assertIsNone(row["gps_complete"])
            if row["manual_complete"]:
                self.assertTrue(row["effective_complete"])
        for row in snapshot.tables["exploration_history"]:
            self.assertIsNone(row["supported_nodes"])
            self.assertIsNone(row["new_nodes"])

    def test_source_revision_invalidates_support_and_snapshot_version(self):
        f = self.fixture
        source, _ = self.add(BASE_ID, "2026-10-01", TINY_NODES)
        before = self.snapshot()
        with f.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET revision=revision+1 WHERE id=:id"), {"id": source})
        after = self.snapshot()
        self.assertNotEqual(before.revision, after.revision)
        self.assertEqual(after.tables["activity_nodes"], [])
        self.assertTrue(all(row["supported_nodes"] is None for row in after.tables["exploration_history"]))

    def test_missing_geography_keeps_activity_statistics_and_input_limit_is_explicit(self):
        self.add(BASE_ID, "2026-10-01", TINY_NODES)
        self.add(BASE_ID + 1, "2026-10-02", TINY_NODES)
        snapshot = self.snapshot(dataset=False)
        self.assertEqual(len(snapshot.tables["activities"]), 2)
        self.assertEqual(snapshot.tables["dataset_status"][0]["coverage_status"], "missing-geography")
        self.assertEqual(snapshot.tables["exploration_history"], [])
        with patch("backend.app.analytics_datasets.MAX_HISTORY_ACTIVITIES", 1):
            with self.assertRaises(HTTPException) as raised:
                self.snapshot()
        self.assertEqual(raised.exception.status_code, 503)

    def test_owner_scope_and_node_tokens_are_independent(self):
        f = self.fixture
        self.add(BASE_ID, "2026-10-01", TINY_NODES)
        own = self.snapshot()
        original = f.account
        try:
            f.account = f.other
            self.add(BASE_ID + 1, "2026-10-01", TINY_NODES)
            other = self.snapshot()
        finally:
            f.account = original
        self.assertEqual([row["activity_id"] for row in own.tables["activities"]], [BASE_ID])
        self.assertEqual([row["activity_id"] for row in other.tables["activities"]], [BASE_ID + 1])
        self.assertFalse({row["node_token"] for row in own.tables["activity_nodes"]}
                         & {row["node_token"] for row in other.tables["activity_nodes"]})

    def test_noncontributing_unknown_date_does_not_poison_dated_history(self):
        self.add(BASE_ID, None)
        self.add(BASE_ID + 1, "2026-10-02", TINY_NODES)
        snapshot = self.snapshot()
        own = [row for row in snapshot.tables["exploration_history"] if row["activity_id"] == BASE_ID + 1]
        self.assertTrue(all(row["history_status"] == "ready" and row["new_nodes"] == len(TINY_NODES) for row in own))
        from services.analytics.templates import TEMPLATES

        sql = TEMPLATES["exploration"][1][2][1].replace("analytics.exploration_history", "template_history")
        with self.fixture.engine.begin() as db:
            db.exec_driver_sql("""CREATE TEMP TABLE template_history(recorded_date date,rule text,
                new_nodes integer,supported_nodes integer,history_status text) ON COMMIT DROP""")
            db.execute(text("""INSERT INTO template_history VALUES
                (:recorded_date,:rule,:new_nodes,:supported_nodes,:history_status)"""), snapshot.tables["exploration_history"])
            rows = db.execute(text(sql)).mappings().all()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["history_status"] == "ready" and row["cumulative_retained_nodes"] == len(TINY_NODES) for row in rows))
            db.exec_driver_sql("INSERT INTO template_history VALUES(NULL,'normal',NULL,1,'unknown-dates')")
            rows = db.execute(text(sql)).mappings().all()
            self.assertIsNone(next(row for row in rows if row["rule"] == "normal")["cumulative_retained_nodes"])

    def test_provider_only_and_mixed_provenance_do_not_affect_metrics_or_readiness(self):
        f = self.fixture
        self.add(BASE_ID, "2026-10-01", TINY_NODES[:1])
        provider, job = self.add(BASE_ID + 1, None, TINY_NODES[1:])
        mixed, mixed_job = self.add(BASE_ID + 2, None, TINY_NODES)
        f.add_source(BASE_ID + 2, TINY_NODES)
        with f.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET source_kind='unapproved-provider' WHERE id IN (:a,:b)"), {"a": provider, "b": mixed})
            db.execute(text("UPDATE jobs SET status='failed' WHERE id IN (:a,:b)"), {"a": job, "b": mixed_job})
            db.execute(text("""INSERT INTO activity_sources(account_id,source_kind,status)
                VALUES (:account,'unapproved-provider','queued')"""), {"account": f.account})
        snapshot = self.snapshot()
        self.assertEqual([row["activity_id"] for row in snapshot.tables["activities"]], [BASE_ID])
        status = snapshot.tables["dataset_status"][0]
        self.assertEqual(status["coverage_status"], "ready")
        self.assertEqual([status[field] for field in ("pending_sources", "failed_sources", "pending_imports")], [0, 0, 0])
        self.assertEqual(len(snapshot.tables["activity_nodes"]), 1)
        self.assertTrue(all(row["history_status"] == "ready" for row in snapshot.tables["exploration_history"]))

    def test_normal_nine_of_ten_crosses_before_strict_ten_of_ten(self):
        f = self.fixture
        with f.engine.begin() as db:
            street = db.execute(text("""INSERT INTO streets
                (dataset_id,city_id,normalized_name,display_name,eligible_node_count)
                VALUES (:dataset,:city,'ten-node-road','Ten Node Road',10) RETURNING id"""), {
                "dataset": f.dataset, "city": f.city_id,
            }).scalar_one()
            for node in LONG_NODES[:10]:
                db.execute(text("INSERT INTO street_nodes(dataset_id,street_id,osm_node_id) VALUES (:dataset,:street,:node)"), {
                    "dataset": f.dataset, "street": street, "node": node,
                })
        self.add(BASE_ID, "2026-10-01", LONG_NODES[:9])
        snapshot = self.snapshot()
        progress = {row["rule"]: row for row in snapshot.tables["street_progress"] if row["street_id"] == street}
        self.assertTrue(progress["normal"]["gps_complete"])
        self.assertFalse(progress["strict"]["gps_complete"])
        self.add(BASE_ID + 1, "2026-10-02", LONG_NODES[9:10])
        snapshot = self.snapshot()
        for row in snapshot.tables["exploration_history"]:
            impact = f.impact(row["activity_id"], rule=row["rule"]).json()
            self.assertEqual(row["streets_completed"], impact["streets_completed"])


if __name__ == "__main__":
    unittest.main()
