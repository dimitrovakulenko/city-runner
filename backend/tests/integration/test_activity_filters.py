"""PostGIS activity-selection and filtered GPS-coverage tests."""

import hashlib
import json
import os
import re
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.coverage import rebuild_account_coverage
from backend.app.geography import import_osm_xml
from backend.app.main import create_app
from backend.tests.integration.test_map_api import _city_fixture


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class ActivityFiltersPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("activity filter tests require generated disposable PostGIS")
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
        self.account = f"filter-a-{uuid.uuid4().hex}"
        self.other = f"filter-b-{uuid.uuid4().hex}"
        self.region = f"filter-{uuid.uuid4().hex}"
        self.tokens = {self.account: uuid.uuid4().hex * 2, self.other: uuid.uuid4().hex * 2}
        self.tmp = tempfile.TemporaryDirectory()
        self.osm_path = Path(self.tmp.name) / "city.osm"
        self.osm_path.write_bytes(_city_fixture())
        with self.engine.begin() as db:
            for account, token in self.tokens.items():
                db.execute(text("INSERT INTO accounts(id) VALUES (:account)"), {"account": account})
                db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                    VALUES (:digest,:account,:expires)"""), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
                    "expires": datetime.now(timezone.utc) + timedelta(days=1),
                })
        self.dataset = import_osm_xml(
            self.engine, self.osm_path, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-06T00:00:00Z", coverage_mode="complete",
            coverage_evidence="Synthetic activity-filter fixture",
        )["dataset_id"]
        self.app = create_app(self.engine)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id IN (:a,:b)"), {
                "a": self.account, "b": self.other,
            })
            db.execute(text("DELETE FROM accounts WHERE id IN (:a,:b)"), {
                "a": self.account, "b": self.other,
            })
            db.execute(text("DELETE FROM map_datasets WHERE id=:dataset"), {"dataset": self.dataset})
        self.tmp.cleanup()

    def headers(self, account=None):
        account = account or self.account
        return {"Authorization": "Bearer " + self.tokens[account]}

    def add_activity(self, activity_id, date_value, activity_type, *, unmapped=0, account=None):
        account = account or self.account
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,:name,:date,:type,true,:unmapped,
                    CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                "id": activity_id, "account": account, "name": f"Activity {activity_id}",
                "date": date_value, "type": activity_type, "unmapped": unmapped,
                "tracks": json.dumps([[[0.25, 0.5], [0.75, 0.5]]]),
                "timestamps": json.dumps([[None, None]]),
            })

    def add_source(self, activity_id, kind, nodes=(), *, unsupported=0, account=None,
                   source_status="succeeded", run_status="succeeded", job_status="succeeded"):
        account = account or self.account
        with self.engine.begin() as db:
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,status) VALUES (:account,:activity,:kind,:status)
                RETURNING id"""), {"account": account, "activity": activity_id,
                                     "kind": kind, "status": source_status}).scalar_one()
            if run_status is not None:
                job_id = db.execute(text("""INSERT INTO jobs
                    (account_id,kind,dedupe_key,status,attempts,max_attempts)
                    VALUES (:account,'match_coverage',:key,:status,1,5) RETURNING id"""), {
                    "account": account, "key": f"filter-{source_id}-{uuid.uuid4().hex}",
                    "status": job_status,
                }).scalar_one()
                db.execute(text("""INSERT INTO coverage_source_runs
                    (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                     supported_sample_count,unsupported_sample_count,matched_node_count)
                    VALUES (:account,:source,1,:dataset,:job,:status,:samples,:supported,:unsupported,:supported)"""), {
                    "account": account, "source": source_id, "dataset": self.dataset, "job": job_id,
                    "status": run_status, "samples": len(nodes) + unsupported,
                    "supported": len(nodes), "unsupported": unsupported,
                })
                for node_id in nodes:
                    db.execute(text("""INSERT INTO source_node_contributions
                        (account_id,source_id,source_revision,dataset_id,node_id)
                        VALUES (:account,:source,1,:dataset,:node)"""), {
                        "account": account, "source": source_id, "dataset": self.dataset, "node": node_id,
                    })
        return int(source_id)

    def test_activity_list_options_source_union_dates_unknowns_and_paging(self):
        first, second, no_source, invalid, year_zero, malformed = (
            9_007_199_254_746_101, 9_007_199_254_746_102, 9_007_199_254_746_103,
            9_007_199_254_746_104, 9_007_199_254_746_106, 9_007_199_254_746_107,
        )
        self.add_activity(first, "2026-01-01", " RUN ", unmapped=5)
        self.add_activity(second, "2026-01-02", "Walk", unmapped=7)
        self.add_activity(no_source, "unknown", None)
        self.add_activity(invalid, "2026-02-30", "unknown")
        self.add_activity(year_zero, "0000-01-01", "unknown")
        self.add_activity(malformed, "not-a-date", "unknown")
        self.add_source(first, "gpx", [10001])
        self.add_source(first, "fit", [10002])
        self.add_source(second, "fit", [10003])
        self.add_activity(9_007_199_254_746_105, "2026-01-01", "cycling", account=self.other)

        self.assertEqual(self.client.get("/api/activities", params={"source": "gpx"},
            headers=self.headers()).json()["total"], 1)
        fit = self.client.get("/api/activities", params={"source": "fit", "page_size": 1},
            headers=self.headers()).json()
        self.assertEqual((fit["total"], len(fit["items"])), (2, 1))
        self.assertEqual(fit["items"][0]["id"], str(second))
        fit_page_two = self.client.get("/api/activities", params={
            "source": "fit", "page_size": 1, "page": 2,
        }, headers=self.headers()).json()
        self.assertEqual(fit_page_two["items"][0]["id"], str(first))
        self.assertEqual(self.client.get("/api/activities", params={"source": "unknown"},
            headers=self.headers()).json()["total"], 4)
        unknown = self.client.get("/api/activities", params={"activity_type": " UNKNOWN "},
            headers=self.headers()).json()
        self.assertEqual({item["id"] for item in unknown["items"]},
                         {str(no_source), str(invalid), str(year_zero), str(malformed)})
        bounded = self.client.get("/api/activities", params={
            "date_from": "2026-01-01", "date_to": "2026-01-01", "activity_type": " rUn ",
        }, headers=self.headers()).json()
        self.assertEqual([item["id"] for item in bounded["items"]], [str(first)])
        self.assertEqual(self.client.get("/api/activities/filters", headers=self.headers()).json(), {
            "activity_types": ["run", "walk"], "types_truncated": False,
        })
        self.assertEqual(self.client.get("/api/activities/filters", headers=self.headers(self.other)).json()[
            "activity_types"], ["cycling"])
        for params in ({"date_from": "2026-02-30"}, {"date_to": "2026/01/01"},
                       {"date_from": "2026-02-02", "date_to": "2026-02-01"},
                       {"activity_type": "x" * 81}, {"source": "future"}):
            self.assertEqual(self.client.get("/api/activities", params=params,
                headers=self.headers()).status_code, 422, params)

    def test_filtered_map_and_progress_union_sources_manual_exclusion_and_pending_imports(self):
        first, second, third = 9_007_199_254_746_201, 9_007_199_254_746_202, 9_007_199_254_746_203
        self.add_activity(first, "2026-01-01", "RUN", unmapped=5)
        self.add_activity(second, "2026-01-02", "walk", unmapped=7)
        self.add_activity(third, "2026-01-03", "run", unmapped=11)
        gpx = self.add_source(first, "gpx", [10001], unsupported=2)
        self.add_source(first, "fit", [10002])
        self.add_source(second, "gpx", [10002, 10003])
        deleted_source = self.add_source(third, "gpx", [10003])
        rebuild_account_coverage(self.engine, self.account, self.dataset)
        with self.engine.begin() as db:
            city_id = db.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset"), {
                "dataset": self.dataset,
            }).scalar_one()
            street_id = db.execute(text("SELECT id FROM streets WHERE dataset_id=:dataset"), {
                "dataset": self.dataset,
            }).scalar_one()
            db.execute(text("""INSERT INTO manual_street_completions
                (account_id,dataset_id,street_id,reason) VALUES (:account,:dataset,:street,'manual')"""), {
                "account": self.account, "dataset": self.dataset, "street": street_id,
            })
            db.execute(text("""INSERT INTO activity_sources(account_id,source_kind,status)
                VALUES (:account,'fit','queued')"""), {"account": self.account})

        default = self.client.get("/api/progress", headers=self.headers()).json()
        self.assertEqual(default["coverage_scope"], "lifetime")
        self.assertEqual(default["filters"], {"date_from": None, "date_to": None,
                                               "activity_type": None, "source": "all"})
        self.assertEqual((default["datasets"][0]["visited_node_count"], default["unmapped_points"],
                          default["pending_imports"], default["state"]), (3, 23, 1, "ready"))

        selection = {"date_from": "2026-01-01", "date_to": "2026-01-01",
                     "activity_type": " RUN ", "source": "gpx", "coverage_scope": "filtered"}
        progress = self.client.get("/api/progress", params=selection, headers=self.headers()).json()
        dataset = progress["datasets"][0]
        self.assertEqual(progress["filters"], {"date_from": "2026-01-01", "date_to": "2026-01-01",
                                                "activity_type": "run", "source": "gpx"})
        self.assertEqual((dataset["visited_node_count"], dataset["unsupported_sample_count"],
                          dataset["eligible_streets"], dataset["completed_streets"],
                          dataset["manual_completed_streets"], dataset["effective_completed_streets"],
                          progress["unmapped_points"], progress["pending_imports"], progress["state"]),
                         (1, 2, 1, 0, 1, 0, 5, 1, "ready"))

        mapped = self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 16, **selection},
                                 headers=self.headers())
        self.assertEqual(mapped.status_code, 200, mapped.text)
        body = mapped.json()
        self.assertEqual(body["coverage_scope"], "filtered")
        self.assertEqual([track["activity_id"] for track in body["tracks"]], [str(first)])
        self.assertEqual(body["streets"][0]["visited_nodes"], 1)
        self.assertTrue(body["streets"][0]["manual_completed"])
        self.assertFalse(body["streets"][0]["effective_completed"])
        self.assertEqual({node["node_id"] for node in body["missing_nodes"]}, {"10002", "10003"})
        self.assertEqual(body["pending_imports"], 1)

        fit = self.client.get("/api/progress", params={**selection, "source": "fit"},
                              headers=self.headers()).json()
        self.assertEqual((fit["datasets"][0]["visited_node_count"], fit["unmapped_points"]), (1, 5))
        unknown = self.client.get("/api/progress", params={**selection, "source": "unknown"},
                                  headers=self.headers()).json()
        self.assertEqual((unknown["datasets"][0]["visited_node_count"], unknown["unmapped_points"]), (0, 0))

        later_selection = {"date_from": "2026-01-03", "date_to": "2026-01-03",
                           "activity_type": "run", "source": "gpx", "coverage_scope": "filtered"}
        self.assertEqual(self.client.get("/api/progress", params=later_selection,
            headers=self.headers()).json()["datasets"][0]["visited_node_count"], 1)
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=:source"), {
                "account": self.account, "source": deleted_source,
            })
        deleted = self.client.get("/api/progress", params=later_selection,
                                  headers=self.headers()).json()
        self.assertEqual((deleted["datasets"][0]["state"],
                          deleted["datasets"][0]["visited_node_count"]), ("ready", 0))

        with self.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET revision=2 WHERE id=:source"), {"source": gpx})
            job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,status,attempts,max_attempts)
                VALUES (:account,'match_coverage','filter-revision-two','succeeded',1,5) RETURNING id"""), {
                "account": self.account,
            }).scalar_one()
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                 supported_sample_count,unsupported_sample_count,matched_node_count)
                VALUES (:account,:source,2,:dataset,:job,'succeeded',0,0,0,0)"""), {
                "account": self.account, "source": gpx, "dataset": self.dataset, "job": job_id,
            })
        fenced = self.client.get("/api/progress", params=selection, headers=self.headers()).json()
        self.assertEqual((fenced["datasets"][0]["state"], fenced["datasets"][0]["visited_node_count"]),
                         ("ready", 0))


if __name__ == "__main__":
    unittest.main()
