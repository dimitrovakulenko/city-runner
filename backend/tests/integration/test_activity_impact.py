"""Synthetic PostGIS coverage-history impact API tests."""

import hashlib
import os
import re
import tempfile
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
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.geography import import_osm_xml
from backend.app.main import create_app
from backend.tests.integration.test_explorer import LONG_NODES, TINY_NODES, _osm_fixture


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")
BASE_ID = 9_007_199_254_746_000
RULE_NODES = list(range(9_007_199_254_700_001, 9_007_199_254_700_011))


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class ActivityImpactPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("activity impact tests require generated disposable PostGIS")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        command.upgrade(cls.config, "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.region = f"impact-{uuid.uuid4().hex}"
        self.account = f"impact-a-{uuid.uuid4().hex}"
        self.other = f"impact-b-{uuid.uuid4().hex}"
        self.tokens = {self.account: uuid.uuid4().hex * 2, self.other: uuid.uuid4().hex * 2}
        self.tmp = tempfile.TemporaryDirectory()
        self.osm_path = Path(self.tmp.name) / "impact.osm"
        self.osm_path.write_bytes(_osm_fixture())
        with self.engine.begin() as db:
            for account, token in self.tokens.items():
                db.execute(text("INSERT INTO accounts(id) VALUES (:account)"), {"account": account})
                db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                    VALUES (:digest,:account,:expires)"""), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
                    "expires": datetime.now(timezone.utc) + timedelta(hours=1),
                })
        self.dataset = import_osm_xml(
            self.engine, self.osm_path, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-06T00:00:00Z", coverage_mode="complete",
            coverage_evidence="Synthetic activity-impact fixture",
        )["dataset_id"]
        self.app = create_app(self.engine)
        self.client = TestClient(self.app)
        with self.engine.connect() as db:
            self.city_id = int(db.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset"), {
                "dataset": self.dataset,
            }).scalar_one())
            self.streets = {row["display_name"]: int(row["id"]) for row in db.execute(text(
                "SELECT id,display_name FROM streets WHERE dataset_id=:dataset"), {
                "dataset": self.dataset,
            }).mappings()}

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id IN (:a,:b)"), {
                "a": self.account, "b": self.other,
            })
            db.execute(text("DELETE FROM accounts WHERE id IN (:a,:b)"), {
                "a": self.account, "b": self.other,
            })
            db.execute(text("DELETE FROM map_datasets WHERE region=:region"), {"region": self.region})
        self.tmp.cleanup()

    def headers(self, account=None):
        account = account or self.account
        return {"Authorization": "Bearer " + self.tokens[account]}

    def impact(self, activity_id, **params):
        query = {"dataset_id": self.dataset, **params}
        return self.client.get(f"/api/activities/{activity_id}/impact", params=query, headers=self.headers())

    def add_activity(self, activity_id, activity_date, name=None):
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,:name,:date,'run',true,0,'[]'::json,'[]'::json)"""), {
                "id": activity_id, "account": self.account,
                "name": name or f"Synthetic {activity_id}", "date": activity_date,
            })

    def add_source(self, activity_id, nodes=(), *, source_revision=1, job_status="succeeded",
                   source_status="succeeded", run_status="succeeded", include_run=True):
        with self.engine.begin() as db:
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,status,revision)
                VALUES (:account,:activity,'gpx',:status,:revision) RETURNING id"""), {
                "account": self.account, "activity": activity_id,
                "status": source_status, "revision": source_revision,
            }).scalar_one()
            job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,status,attempts,max_attempts)
                VALUES (:account,'match_coverage',:key,:status,1,5) RETURNING id"""), {
                "account": self.account, "key": f"impact-{source_id}-{uuid.uuid4().hex}",
                "status": job_status,
            }).scalar_one()
            if include_run:
                db.execute(text("""INSERT INTO coverage_source_runs
                    (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                     supported_sample_count,unsupported_sample_count,matched_node_count)
                    VALUES (:account,:source,:revision,:dataset,:job,:status,:count,:count,0,:count)"""), {
                    "account": self.account, "source": source_id, "revision": source_revision,
                    "dataset": self.dataset, "job": job_id, "status": run_status, "count": len(nodes),
                })
                for node_id in nodes:
                    db.execute(text("""INSERT INTO source_node_contributions
                        (account_id,source_id,source_revision,dataset_id,node_id)
                        VALUES (:account,:source,:revision,:dataset,:node)"""), {
                        "account": self.account, "source": source_id, "revision": source_revision,
                        "dataset": self.dataset, "node": node_id,
                    })
        return int(source_id), int(job_id)

    def test_union_same_day_order_global_new_nodes_and_later_activity(self):
        older, same_day, target, later = BASE_ID + 1, BASE_ID + 3, BASE_ID + 7, BASE_ID + 2
        with self.engine.begin() as db:
            for index, node_id in enumerate(RULE_NODES):
                db.execute(text("""INSERT INTO osm_nodes(dataset_id,osm_node_id,point)
                    VALUES (:dataset,:node,ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography)"""), {
                    "dataset": self.dataset, "node": node_id,
                    "lon": 0.9 + index * 0.01, "lat": 0.9,
                })
            rule_street = db.execute(text("""INSERT INTO streets
                (dataset_id,city_id,normalized_name,display_name,eligible_node_count)
                VALUES (:dataset,:city,'rule-road','Rule Road',10) RETURNING id"""), {
                "dataset": self.dataset, "city": self.city_id,
            }).scalar_one()
            for node_id in RULE_NODES:
                db.execute(text("INSERT INTO street_nodes(dataset_id,street_id,osm_node_id) "
                                "VALUES (:dataset,:street,:node)"), {
                    "dataset": self.dataset, "street": rule_street, "node": node_id,
                })
        self.add_activity(older, "2026-01-01")
        self.add_source(older, [TINY_NODES[0], TINY_NODES[1], LONG_NODES[0], *RULE_NODES[:8]])
        self.add_source(older, [TINY_NODES[1]])
        self.add_activity(same_day, "2026-01-02")
        self.add_source(same_day, [LONG_NODES[0]])
        self.add_source(same_day, [LONG_NODES[0], TINY_NODES[1]])
        self.add_activity(target, "2026-01-02")
        target_sources = [
            self.add_source(target, [TINY_NODES[1], TINY_NODES[2], LONG_NODES[0], RULE_NODES[8]])[0],
            self.add_source(target, [TINY_NODES[2], LONG_NODES[1]])[0],
        ]
        self.add_activity(later, "2026-01-03")
        self.add_source(later, [TINY_NODES[0], TINY_NODES[1], TINY_NODES[2], LONG_NODES[2]])
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO manual_street_completions
                (account_id,dataset_id,street_id,reason)
                VALUES (:account,:dataset,:street,'manual label does not affect GPS impact')"""), {
                "account": self.account, "dataset": self.dataset, "street": self.streets["Long Road"],
            })
            revisit_street = db.execute(text("""INSERT INTO streets
                (dataset_id,city_id,normalized_name,display_name,eligible_node_count)
                VALUES (:dataset,:city,'revisited-road','Revisited Road',1) RETURNING id"""), {
                "dataset": self.dataset, "city": self.city_id,
            }).scalar_one()
            db.execute(text("INSERT INTO street_nodes(dataset_id,street_id,osm_node_id) "
                            "VALUES (:dataset,:street,:node)"), {
                "dataset": self.dataset, "street": revisit_street, "node": TINY_NODES[1],
            })

        first = self.impact(target, page=1, page_size=1)
        self.assertEqual(first.status_code, 200, first.text)
        response = first.json()
        self.assertEqual(response["history_status"], "ready")
        self.assertEqual((response["supported_nodes"], response["new_nodes"],
                          response["streets_advanced"], response["streets_completed"], response["total"]),
                         (5, 3, 3, 2, 4))
        self.assertEqual((response["activity_id"], response["dataset_id"]), (str(target), str(self.dataset)))
        self.assertEqual([item["name"] for item in response["streets"]], ["Rule Road"])
        rule_road = response["streets"][0]
        self.assertEqual((rule_road["before_nodes"], rule_road["after_nodes"],
                          rule_road["completed_by_activity"], rule_road["current_nodes"]), (8, 9, True, 9))
        self.assertEqual(rule_road["bounds"], [0.9, 0.9, 0.99, 0.9])
        second = self.impact(target, page=2, page_size=1).json()
        tiny = second["streets"][0]
        self.assertEqual(tiny["name"], "Tiny Road")
        self.assertEqual((tiny["supported_nodes"], tiny["new_nodes"], tiny["before_nodes"],
                          tiny["after_nodes"], tiny["completed_by_activity"], tiny["current_nodes"]),
                         (2, 1, 2, 3, True, 3))
        self.assertEqual(tiny["bounds"], [0.2, 0.4, 0.4, 0.4])
        third = self.impact(target, page=3, page_size=1).json()
        self.assertEqual([item["name"] for item in third["streets"]], ["Long Road"])
        long = third["streets"][0]
        self.assertEqual((long["supported_nodes"], long["new_nodes"], long["before_nodes"],
                          long["after_nodes"], long["completed_by_activity"], long["current_nodes"]),
                         (2, 1, 1, 2, False, 3))
        self.assertEqual(long["bounds"], [0.2, 0.8, 0.2 + 10 * 0.04, 0.8])
        fourth = self.impact(target, page=4, page_size=1).json()
        self.assertEqual([item["name"] for item in fourth["streets"]], ["Revisited Road"])
        revisit = fourth["streets"][0]
        self.assertEqual((revisit["new_nodes"], revisit["before_nodes"], revisit["after_nodes"],
                          revisit["completed_by_activity"], revisit["current_nodes"]), (0, 1, 1, False, 1))
        beyond_last = self.impact(target, page=99, page_size=1).json()
        self.assertEqual(beyond_last["streets"], [])
        self.assertEqual((beyond_last["supported_nodes"], beyond_last["new_nodes"],
                          beyond_last["streets_advanced"], beyond_last["streets_completed"],
                          beyond_last["total"]), (5, 3, 3, 2, 4))
        strict = self.impact(target, rule="strict").json()
        self.assertEqual(strict["streets_completed"], 1)
        self.assertFalse(next(item for item in strict["streets"] if item["name"] == "Rule Road")
                         ["completed_by_activity"])
        self.assertFalse(next(item for item in strict["streets"] if item["name"] == "Long Road")
                         ["completed_by_activity"])
        self.assertEqual(len(target_sources), 2)

    def test_zero_support_target_and_unrelated_unknown_date(self):
        target, unrelated, invalid_target = BASE_ID + 120, BASE_ID + 121, BASE_ID + 122
        self.add_activity(target, "2026-06-01")
        self.add_source(target)
        self.add_activity(unrelated, "2026-02-30")
        self.add_source(unrelated)

        empty = self.impact(target).json()
        self.assertEqual(empty["history_status"], "ready")
        self.assertEqual((empty["supported_nodes"], empty["new_nodes"], empty["streets_advanced"],
                          empty["streets_completed"], empty["streets"], empty["total"]),
                         (0, 0, 0, 0, [], 0))

        self.add_activity(invalid_target, "2026-06-02")
        self.add_source(invalid_target, [TINY_NODES[0]])
        with self.engine.begin() as db:
            db.execute(text("UPDATE activities SET date='2026-02-30' WHERE id=:activity"), {
                "activity": invalid_target,
            })
        invalid = self.impact(invalid_target).json()
        self.assertEqual(invalid["history_status"], "unknown-dates")
        self.assertEqual((invalid["supported_nodes"], invalid["new_nodes"], invalid["total"]), (1, None, 1))
        self.assertEqual(invalid["streets"][0]["name"], "Tiny Road")

    def test_backfill_and_source_deletion_recompute_history(self):
        target = BASE_ID + 20
        backfill = BASE_ID + 40
        self.add_activity(target, "2026-02-02")
        target_source, _ = self.add_source(target, [TINY_NODES[0], TINY_NODES[1]])
        self.assertEqual(self.impact(target).json()["new_nodes"], 2)
        self.add_activity(backfill, "2026-02-01")
        backfill_source, _ = self.add_source(backfill, [TINY_NODES[1]])
        after_backfill = self.impact(target).json()
        self.assertEqual((after_backfill["new_nodes"], after_backfill["streets_advanced"],
                          after_backfill["streets_completed"]), (1, 1, 0))
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=:source"), {
                "account": self.account, "source": backfill_source,
            })
        after_delete = self.impact(target).json()
        self.assertEqual((after_delete["new_nodes"], after_delete["streets_completed"]), (2, 0))
        self.assertGreater(target_source, 0)

    def test_unknown_dates_and_history_limit_keep_current_street_links(self):
        invalid_history, target = BASE_ID + 50, BASE_ID + 60
        self.add_activity(invalid_history, "2026-02-30")
        self.add_source(invalid_history, [TINY_NODES[0]])
        self.add_activity(target, "2026-03-01")
        self.add_source(target, [TINY_NODES[1]])
        unknown = self.impact(target).json()
        self.assertEqual(unknown["history_status"], "unknown-dates")
        self.assertEqual((unknown["supported_nodes"], unknown["new_nodes"], unknown["total"]), (1, None, 1))
        self.assertIsNone(unknown["streets"][0]["before_nodes"])
        self.assertIsNone(unknown["streets"][0]["completed_by_activity"])
        self.assertEqual(unknown["streets"][0]["current_nodes"], 2)

        for index in range(2):
            activity_id = BASE_ID + 70 + index
            self.add_activity(activity_id, f"2026-03-0{index + 2}")
            self.add_source(activity_id, [LONG_NODES[index]])
        with patch("backend.app.activity_impact.MAX_HISTORY_ACTIVITIES", 2):
            limited = self.impact(target).json()
        self.assertEqual(limited["history_status"], "history-limit")
        self.assertIsNone(limited["new_nodes"])
        self.assertEqual(limited["total"], 1)
        self.assertEqual(limited["streets"][0]["name"], "Tiny Road")

    def test_missing_provenance_owner_order_pending_and_failed_coverage(self):
        target, no_source = BASE_ID + 80, BASE_ID + 81
        self.add_activity(target, "2026-04-01")
        self.add_source(target, [TINY_NODES[0]])
        self.add_activity(no_source, "2026-04-02")
        missing = self.impact(no_source).json()
        self.assertEqual(missing["history_status"], "missing-provenance")
        self.assertIsNone(missing["supported_nodes"])
        self.assertEqual((missing["streets"], missing["total"]), ([], None))

        negative_id = -987654321
        self.add_activity(negative_id, "2026-04-02")
        signed = self.impact(negative_id).json()
        self.assertEqual((signed["activity_id"], signed["history_status"]),
                         (str(negative_id), "missing-provenance"))

        pending_id = BASE_ID + 82
        self.add_activity(pending_id, "2026-04-03")
        self.add_source(pending_id, [TINY_NODES[1]])
        queued_id = BASE_ID + 83
        self.add_activity(queued_id, "2026-04-04")
        queued, _ = self.add_source(queued_id, source_status="succeeded", include_run=False)
        pending = self.impact(pending_id).json()
        self.assertEqual(pending["coverage"]["status"], "pending")
        self.assertIsNone(pending["supported_nodes"])
        self.assertEqual((pending["streets"], pending["total"]), ([], None))
        failed_id = BASE_ID + 84
        self.add_activity(failed_id, "2026-04-05")
        self.add_source(failed_id, [LONG_NODES[0]], job_status="failed", run_status="failed")
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=:source"), {
                "account": self.account, "source": queued,
            })
        # A failed unrelated match fences all ready-only metrics for this account/dataset.
        failed = self.impact(target).json()
        self.assertEqual(failed["coverage"]["status"], "failed")
        self.assertIsNone(failed["new_nodes"])
        self.assertEqual((failed["streets"], failed["total"]), ([], None))

        absent_id = BASE_ID + 999
        with patch("backend.app.activity_impact._dataset", side_effect=AssertionError("dataset lookup ran")):
            absent = self.client.get(f"/api/activities/{absent_id}/impact",
                params={"dataset_id": self.dataset}, headers=self.headers())
            foreign = self.client.get(f"/api/activities/{target}/impact",
                params={"dataset_id": self.dataset}, headers=self.headers(self.other))
        self.assertEqual((absent.status_code, foreign.status_code), (404, 404))
        bad_page = self.impact(target, page=0)
        bad_size = self.impact(target, page_size=101)
        self.assertEqual((bad_page.status_code, bad_size.status_code), (422, 422))

    def test_source_revision_and_concurrent_source_deletion_use_snapshot_fences(self):
        target, old = BASE_ID + 100, BASE_ID + 101
        self.add_activity(old, "2026-05-01")
        old_source, old_job = self.add_source(old, [TINY_NODES[0]])
        self.add_activity(target, "2026-05-02")
        target_source, _ = self.add_source(target, [TINY_NODES[1]])
        with self.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET revision=2 WHERE account_id=:account AND id=:source"), {
                "account": self.account, "source": old_source,
            })
            new_job = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,status,attempts,max_attempts)
                VALUES (:account,'match_coverage','rev-two','succeeded',1,5) RETURNING id"""), {
                "account": self.account,
            }).scalar_one()
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                 supported_sample_count,unsupported_sample_count,matched_node_count)
                VALUES (:account,:source,2,:dataset,:job,'succeeded',0,0,0,0)"""), {
                "account": self.account, "source": old_source, "dataset": self.dataset, "job": new_job,
            })
        fenced = self.impact(target).json()
        self.assertEqual(fenced["new_nodes"], 1)
        self.assertGreater(old_job, 0)

        entered, release = threading.Event(), threading.Event()
        from backend.app import activity_impact as impact_module
        real_coverage = impact_module._coverage

        def gated_coverage(*args):
            value = real_coverage(*args)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("snapshot test gate timed out")
            return value

        with patch("backend.app.activity_impact._coverage", side_effect=gated_coverage):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: TestClient(self.app).get(
                    f"/api/activities/{target}/impact", params={"dataset_id": self.dataset},
                    headers=self.headers()))
                self.assertTrue(entered.wait(5))
                with self.engine.begin() as db:
                    db.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=:source"), {
                        "account": self.account, "source": target_source,
                    })
                release.set()
                during_delete = future.result(timeout=10)
        self.assertEqual(during_delete.status_code, 200, during_delete.text)
        self.assertEqual(during_delete.json()["supported_nodes"], 1)
        after_delete = self.impact(target).json()
        self.assertEqual(after_delete["history_status"], "missing-provenance")


if __name__ == "__main__":
    unittest.main()
