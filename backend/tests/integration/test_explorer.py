import hashlib
import json
import os
import re
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.geography import import_osm_xml
from backend.app.main import create_app


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")
TINY_NODES = [9_007_199_254_740_001, 9_007_199_254_740_002, 9_007_199_254_740_003]
LONG_NODES = list(range(9_007_199_254_740_011, 9_007_199_254_740_022))


def _osm_fixture(*, include_long=True, revision="one"):
    root = Element("osm", {"version": "0.6", "generator": "explorer-test"})
    points = {
        1: (0, 0), 2: (2, 0), 3: (2, 2), 4: (0, 2),
        TINY_NODES[0]: (.2, .4), TINY_NODES[1]: (.3, .4), TINY_NODES[2]: (.4, .4),
    }
    points.update({node_id: (.2 + i * .04, .8) for i, node_id in enumerate(LONG_NODES)})
    for node_id, (lon, lat) in points.items():
        SubElement(root, "node", {"id": str(node_id), "lon": str(lon), "lat": str(lat)})
    ways = [(10, [1, 2]), (11, [2, 3]), (12, [3, 4]), (13, [4, 1]),
            (20, TINY_NODES)]
    if include_long:
        ways.append((21, LONG_NODES))
    for way_id, refs in ways:
        way = SubElement(root, "way", {"id": str(way_id)})
        for node_id in refs:
            SubElement(way, "nd", {"ref": str(node_id)})
        if way_id >= 20:
            SubElement(way, "tag", {"k": "highway", "v": "residential"})
            SubElement(way, "tag", {"k": "name", "v": "Tiny Road" if way_id == 20 else "Long Road"})
    relation = SubElement(root, "relation", {"id": "900"})
    for way_id in (10, 11, 12, 13):
        SubElement(relation, "member", {"type": "way", "ref": str(way_id), "role": "outer"})
    for key, value in {"type": "boundary", "boundary": "administrative", "admin_level": "8",
                       "name": "Explorer City"}.items():
        SubElement(relation, "tag", {"k": key, "v": value})
    SubElement(root, "tag", {"k": "revision", "v": revision})
    return tostring(root, encoding="utf-8", xml_declaration=True)


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class ExplorerPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("explorer tests require the generated disposable PostGIS database")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        command.upgrade(cls.config, "head")
        with cls.engine.begin() as db:
            db.execute(text("""SELECT setval(pg_get_serial_sequence('streets','id'),
                GREATEST(COALESCE((SELECT max(id) FROM streets),0),9007199254740990),true)"""))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.region = f"explorer-{uuid.uuid4().hex}"
        self.account = f"explorer-a-{uuid.uuid4().hex}"
        self.other = f"explorer-b-{uuid.uuid4().hex}"
        self.tokens = {self.account: uuid.uuid4().hex * 2, self.other: uuid.uuid4().hex * 2}
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "city.osm"
        self.path.write_bytes(_osm_fixture())
        with self.engine.begin() as db:
            for account, token in self.tokens.items():
                db.execute(text("INSERT INTO accounts(id) VALUES (:account)"), {"account": account})
                db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                    VALUES (:digest,:account,:expires)"""), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
                    "expires": datetime.now(timezone.utc) + timedelta(days=1),
                })
        self.dataset = import_osm_xml(
            self.engine, self.path, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-06T00:00:00Z", coverage_mode="complete",
            coverage_evidence="Synthetic complete city fixture",
        )["dataset_id"]
        self.app = create_app(self.engine)
        self.client = TestClient(self.app)
        with self.engine.connect() as db:
            self.city_id = int(db.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset"), {
                "dataset": self.dataset,
            }).scalar_one())
            self.streets = {row["display_name"]: int(row["id"]) for row in db.execute(text("""
                SELECT id,display_name FROM streets WHERE dataset_id=:dataset
            """), {"dataset": self.dataset}).mappings()}

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
        return {"Authorization": f"Bearer {self.tokens[account]}"}

    def add_source(self, account, name, nodes, *, activity_id):
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,:name,'2026-10-05','run',true,0,'[]'::json,'[]'::json)"""), {
                "id": activity_id, "account": account, "name": name,
            })
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,status)
                VALUES (:account,:activity,'gpx','succeeded') RETURNING id"""), {
                "account": account, "activity": activity_id,
            }).scalar_one()
            job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,status,attempts,max_attempts)
                VALUES (:account,'match_coverage',:key,'succeeded',1,5) RETURNING id"""), {
                "account": account, "key": str(source_id),
            }).scalar_one()
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                 supported_sample_count,unsupported_sample_count,matched_node_count)
                VALUES (:account,:source,1,:dataset,:job,'succeeded',:count,:count,0,:count)"""), {
                "account": account, "source": source_id, "dataset": self.dataset,
                "job": job_id, "count": len(nodes),
            })
            for node_id in nodes:
                db.execute(text("""INSERT INTO source_node_contributions
                    (account_id,source_id,source_revision,dataset_id,node_id)
                    VALUES (:account,:source,1,:dataset,:node)"""), {
                    "account": account, "source": source_id, "dataset": self.dataset, "node": node_id,
                })
        return int(source_id)

    def test_city_counts_filters_detail_remaining_and_contributions(self):
        first_activity = 9_007_199_254_741_001
        second_activity = 9_007_199_254_741_002
        first_source = self.add_source(self.account, "First run", [TINY_NODES[0], *LONG_NODES[:9]],
                                       activity_id=first_activity)
        second_source = self.add_source(self.account, "Second run", [TINY_NODES[0], TINY_NODES[1], LONG_NODES[9]],
                                        activity_id=second_activity)

        normal = self.client.get("/api/cities", params={"dataset_id": self.dataset}, headers=self.headers())
        self.assertEqual(normal.status_code, 200, normal.text)
        city = normal.json()["items"][0]
        self.assertEqual((city["visited_nodes"], city["eligible_nodes"]), (12, 14))
        self.assertEqual((city["completed_streets"], city["eligible_streets"]), (1, 2))
        self.assertIsInstance(city["id"], str)

        strict = self.client.get("/api/cities", params={"dataset_id": self.dataset, "rule": "strict"},
                                 headers=self.headers()).json()["items"][0]
        self.assertEqual(strict["completed_streets"], 0)
        path = f"/api/cities/{self.city_id}/streets"
        completed = self.client.get(path, params={"dataset_id": self.dataset, "filter": "completed"},
                                    headers=self.headers()).json()
        self.assertEqual([row["name"] for row in completed["items"]], ["Long Road"])
        partial = self.client.get(path, params={"dataset_id": self.dataset, "filter": "partial"},
                                  headers=self.headers()).json()
        self.assertEqual([row["name"] for row in partial["items"]], ["Tiny Road"])
        strict_incomplete = self.client.get(path, params={"dataset_id": self.dataset, "rule": "strict",
                                                          "filter": "incomplete"},
                                            headers=self.headers()).json()
        self.assertEqual({row["name"] for row in strict_incomplete["items"]}, {"Tiny Road", "Long Road"})
        self.assertFalse(strict_incomplete["items"][0]["state"] == "complete")

        long_id = str(self.streets["Long Road"])
        detail = self.client.get(f"/api/streets/{long_id}", params={"dataset_id": self.dataset, "rule": "normal"},
                                 headers=self.headers()).json()
        self.assertEqual(detail["state"], "complete")
        self.assertEqual(detail["remaining_nodes_page"]["total"], 1)
        self.assertEqual(detail["remaining_nodes"][0]["id"], str(LONG_NODES[10]))
        strict_detail = self.client.get(f"/api/streets/{long_id}", params={"dataset_id": self.dataset, "rule": "strict"},
                                        headers=self.headers()).json()
        self.assertEqual(strict_detail["state"], "partial")
        self.assertEqual(strict_detail["remaining_nodes_page"]["total"], 1)

        contribution_response = self.client.get(f"/api/streets/{long_id}/contributions",
            params={"dataset_id": self.dataset}, headers=self.headers())
        self.assertEqual(contribution_response.status_code, 200, contribution_response.text)
        contributed = contribution_response.json()
        self.assertEqual({item["id"] for item in contributed["activities"]},
                         {str(first_activity), str(second_activity)})
        self.assertTrue(all(isinstance(item["id"], str) for item in contributed["activities"]))
        self.assertTrue(all("tracks" not in item and "timestamps" not in item
                            for item in contributed["activities"]))

        tiny_id = str(self.streets["Tiny Road"])
        tiny_contributions = self.client.get(f"/api/streets/{tiny_id}/contributions",
            params={"dataset_id": self.dataset}, headers=self.headers()).json()
        self.assertEqual(len(tiny_contributions["activities"]), 2)
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=:source"), {
                "account": self.account, "source": second_source,
            })
        remaining = self.client.get(f"/api/streets/{tiny_id}/contributions",
            params={"dataset_id": self.dataset}, headers=self.headers()).json()
        self.assertEqual([item["id"] for item in remaining["activities"]], [str(first_activity)])
        after_delete = self.client.get(f"/api/streets/{tiny_id}", params={"dataset_id": self.dataset},
                                       headers=self.headers()).json()
        self.assertEqual(after_delete["visited_nodes"], 1)
        self.assertEqual(after_delete["remaining_nodes_page"]["total"], 2)
        self.assertGreater(first_source, 0)

    def test_pending_is_null_account_isolated_and_zero_node_streets_are_hidden(self):
        pending_activity = 9_007_199_254_742_001
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO streets(dataset_id,city_id,normalized_name,display_name,eligible_node_count)
                VALUES (:dataset,:city,'zero','Zero Eligible',0)"""), {
                "dataset": self.dataset, "city": self.city_id,
            })
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,'Pending run','2026-10-05','run',false,3,'[]'::json,'[]'::json)"""), {
                "id": pending_activity, "account": self.account,
            })
            db.execute(text("""INSERT INTO activity_sources(account_id,activity_id,source_kind,status)
                VALUES (:account,:activity,'gpx','succeeded')"""), {
                "account": self.account, "activity": pending_activity,
            })
        cities = self.client.get("/api/cities", params={"dataset_id": self.dataset}, headers=self.headers()).json()
        self.assertEqual(cities["coverage"]["status"], "pending")
        self.assertIsNone(cities["items"][0]["visited_nodes"])
        streets = self.client.get(f"/api/cities/{self.city_id}/streets", params={
            "dataset_id": self.dataset, "filter": "incomplete",
        }, headers=self.headers()).json()
        self.assertFalse(streets["filter_applied"])
        self.assertEqual({row["name"] for row in streets["items"]}, {"Tiny Road", "Long Road"})
        self.assertTrue(all(row["state"] is None and row["visited_nodes"] is None for row in streets["items"]))
        tiny_id = str(self.streets["Tiny Road"])
        detail = self.client.get(f"/api/streets/{tiny_id}", params={"dataset_id": self.dataset},
                                 headers=self.headers()).json()
        self.assertIsNone(detail["remaining_nodes"])
        self.assertIsNone(detail["remaining_nodes_page"]["total"])
        contributions = self.client.get(f"/api/streets/{tiny_id}/contributions",
                                        params={"dataset_id": self.dataset},
                                        headers=self.headers()).json()
        self.assertFalse(contributions["activities_available"])
        self.assertIsNone(contributions["total"])

        other = self.client.get("/api/cities", params={"dataset_id": self.dataset},
                                headers=self.headers(self.other)).json()
        self.assertEqual(other["coverage"]["status"], "ready")
        self.assertEqual(other["items"][0]["visited_nodes"], 0)
        self.assertEqual(other["items"][0]["eligible_streets"], 2)
        other_tiny = self.client.get(f"/api/streets/{self.streets['Tiny Road']}/contributions",
            params={"dataset_id": self.dataset}, headers=self.headers(self.other)).json()
        self.assertEqual(other_tiny["activities"], [])
        self.assertEqual(other_tiny["total"], 0)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activity_sources(account_id,source_kind,status)
                VALUES (:account,'gpx','queued')"""), {"account": self.other})
        with_queued = self.client.get("/api/cities", params={"dataset_id": self.dataset},
                                     headers=self.headers(self.other)).json()
        self.assertEqual(with_queued["coverage"]["status"], "ready")
        self.assertEqual(with_queued["coverage"]["pending_imports"], 1)

        pending_sorted = self.client.get(f"/api/cities/{self.city_id}/streets", params={
            "dataset_id": self.dataset, "filter": "nearly-complete", "sort": "completion-desc",
        }, headers=self.headers()).json()
        self.assertFalse(pending_sorted["filter_applied"])
        self.assertFalse(pending_sorted["sort_applied"])
        self.assertEqual(pending_sorted["sort"], "completion-desc")
        self.assertEqual([row["name"] for row in pending_sorted["items"]],
                         ["Long Road", "Tiny Road"])

    def test_street_discovery_filters_and_sorts_global_result_before_paging(self):
        self.add_source(self.account, "Discovery run", LONG_NODES[:9], activity_id=9_007_199_254_745_001)
        roads = {
            "Alpha Road": [*LONG_NODES[:8], *TINY_NODES[:2]],
            "Beta Road": [*LONG_NODES[:8], *TINY_NODES[:2]],
            "Delta Road": [*LONG_NODES[:9], TINY_NODES[0]],
            "Gamma Road": [*LONG_NODES[:7], *TINY_NODES],
        }
        with self.engine.begin() as db:
            road_ids = {}
            for name, nodes in roads.items():
                road_ids[name] = db.execute(text("""INSERT INTO streets
                    (dataset_id,city_id,normalized_name,display_name,eligible_node_count)
                    VALUES (:dataset,:city,:normalized,:name,:eligible) RETURNING id"""), {
                    "dataset": self.dataset, "city": self.city_id,
                    "normalized": name.lower().replace(" ", "-"), "name": name, "eligible": len(nodes),
                }).scalar_one()
                for node_id in nodes:
                    db.execute(text("INSERT INTO street_nodes(dataset_id,street_id,osm_node_id) "
                                    "VALUES (:dataset,:street,:node)"), {
                        "dataset": self.dataset, "street": road_ids[name], "node": node_id,
                    })
            db.execute(text("""INSERT INTO manual_street_completions(account_id,dataset_id,street_id,reason)
                VALUES (:account,:dataset,:street,'completed manually')"""), {
                "account": self.account, "dataset": self.dataset, "street": road_ids["Beta Road"],
            })

        path = f"/api/cities/{self.city_id}/streets"

        def page(*, sort="name", page=1, page_size=100, rule="normal", filter="all", account=None):
            response = self.client.get(path, params={"dataset_id": self.dataset, "sort": sort, "page": page,
                "page_size": page_size, "rule": rule, "filter": filter}, headers=self.headers(account))
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        name_first = page(page=1, page_size=2)
        name_second = page(page=2, page_size=2)
        self.assertEqual([row["name"] for row in name_first["items"]], ["Alpha Road", "Beta Road"])
        self.assertEqual([row["name"] for row in name_second["items"]], ["Delta Road", "Gamma Road"])
        self.assertEqual((name_first["sort"], name_first["sort_applied"]), ("name", True))

        descending = page(sort="completion-desc")
        self.assertEqual([row["name"] for row in descending["items"]], [
            "Delta Road", "Long Road", "Alpha Road", "Beta Road", "Gamma Road", "Tiny Road",
        ])
        self.assertEqual([row["name"] for row in page(sort="completion-desc", page=1, page_size=2)["items"]],
                         ["Delta Road", "Long Road"])
        self.assertEqual([row["name"] for row in page(sort="completion-desc", page=2, page_size=2)["items"]],
                         ["Alpha Road", "Beta Road"])
        ascending = page(sort="completion-asc")
        self.assertEqual([row["name"] for row in ascending["items"]], [
            "Tiny Road", "Gamma Road", "Alpha Road", "Beta Road", "Long Road", "Delta Road",
        ])
        remaining = page(sort="remaining-asc")
        self.assertEqual([row["name"] for row in remaining["items"]], [
            "Delta Road", "Alpha Road", "Beta Road", "Long Road", "Gamma Road", "Tiny Road",
        ])

        normal_nearly = page(filter="nearly-complete")
        self.assertEqual([row["name"] for row in normal_nearly["items"]], ["Alpha Road", "Long Road"])
        strict_nearly = page(rule="strict", filter="nearly-complete")
        self.assertEqual([row["name"] for row in strict_nearly["items"]], [
            "Alpha Road", "Delta Road", "Long Road",
        ])
        beta = self.client.get(f"/api/streets/{road_ids['Beta Road']}",
            params={"dataset_id": self.dataset, "rule": "strict"}, headers=self.headers()).json()
        self.assertEqual((beta["visited_nodes"], beta["state"], beta["manual_completed"],
                          beta["effective_state"]), (8, "partial", True, "complete"))

        other_order = page(sort="completion-desc", account=self.other)
        self.assertEqual([row["name"] for row in other_order["items"]], [
            "Alpha Road", "Beta Road", "Delta Road", "Gamma Road", "Long Road", "Tiny Road",
        ])
        self.assertTrue(all(row["visited_nodes"] == 0 for row in other_order["items"]))
        invalid = self.client.get(path, params={"dataset_id": self.dataset, "sort": "completion-random"},
                                  headers=self.headers())
        self.assertEqual(invalid.status_code, 422)

    def test_live_support_ignores_failed_jobs_and_obsolete_source_revisions(self):
        activity_id = 9_007_199_254_744_001
        source_id = self.add_source(self.account, "Revision test run", [TINY_NODES[0]],
                                    activity_id=activity_id)
        street_id = str(self.streets["Tiny Road"])
        with self.engine.connect() as db:
            job_id = db.execute(text("""SELECT job_id FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"""), {
                "account": self.account, "source": source_id, "dataset": self.dataset,
            }).scalar_one()
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET status='failed' WHERE id=:job"), {"job": job_id})
        failed = self.client.get(f"/api/streets/{street_id}/contributions",
            params={"dataset_id": self.dataset}, headers=self.headers()).json()
        self.assertFalse(failed["activities_available"])
        self.assertEqual(failed["coverage"]["status"], "failed")
        self.assertIsNone(failed["total"])
        fallback = self.client.get(f"/api/cities/{self.city_id}/streets", params={
            "dataset_id": self.dataset, "filter": "nearly-complete", "sort": "remaining-asc",
        }, headers=self.headers()).json()
        self.assertFalse(fallback["filter_applied"])
        self.assertFalse(fallback["sort_applied"])
        self.assertEqual([row["name"] for row in fallback["items"]], ["Long Road", "Tiny Road"])

        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET status='succeeded' WHERE id=:job"), {"job": job_id})
            db.execute(text("UPDATE activity_sources SET revision=2 WHERE account_id=:account AND id=:source"), {
                "account": self.account, "source": source_id,
            })
        obsolete = self.client.get(f"/api/streets/{street_id}", params={"dataset_id": self.dataset},
                                   headers=self.headers()).json()
        self.assertEqual(obsolete["coverage"]["status"], "pending")
        self.assertIsNone(obsolete["visited_nodes"])
        self.assertIsNone(obsolete["remaining_nodes"])

        with self.engine.begin() as db:
            job2 = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,status,attempts,max_attempts)
                VALUES (:account,'match_coverage','revision-two','succeeded',1,5) RETURNING id"""), {
                "account": self.account,
            }).scalar_one()
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                 supported_sample_count,unsupported_sample_count,matched_node_count)
                VALUES (:account,:source,2,:dataset,:job,'succeeded',1,1,0,0)"""), {
                "account": self.account, "source": source_id, "dataset": self.dataset, "job": job2,
            })
        current = self.client.get(f"/api/streets/{street_id}/contributions",
            params={"dataset_id": self.dataset}, headers=self.headers()).json()
        self.assertTrue(current["activities_available"])
        self.assertEqual(current["activities"], [])
        self.assertEqual(current["total"], 0)

    def test_dataset_version_search_paging_and_validation(self):
        self.add_source(self.account, "Old geography run", [TINY_NODES[0]],
                        activity_id=9_007_199_254_743_001)
        self.path.write_bytes(_osm_fixture(include_long=False, revision="two"))
        staged = import_osm_xml(
            self.engine, self.path, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-06T01:00:00Z", coverage_mode="complete",
            coverage_evidence="Synthetic staged version",
        )["dataset_id"]
        staged_pending = self.client.get("/api/cities", params={"dataset_id": staged},
                                         headers=self.headers()).json()
        self.assertEqual(staged_pending["dataset_state"], "importing")
        self.assertEqual(staged_pending["coverage"]["status"], "pending")
        self.assertIsNone(staged_pending["items"][0]["eligible_streets"])

        staged_street = self.engine.connect()
        try:
            with staged_street.begin():
                city_id = staged_street.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset"), {
                    "dataset": staged,
                }).scalar_one()
                street_id = staged_street.execute(text("SELECT id FROM streets WHERE dataset_id=:dataset"), {
                    "dataset": staged,
                }).scalar_one()
                source_id = staged_street.execute(text("""SELECT id FROM activity_sources
                    WHERE account_id=:account ORDER BY id LIMIT 1"""), {"account": self.account}).scalar_one()
                job_id = staged_street.execute(text("""INSERT INTO jobs
                    (account_id,kind,dedupe_key,status,attempts,max_attempts)
                    VALUES (:account,'match_coverage',:key,'succeeded',1,5) RETURNING id"""), {
                    "account": self.account, "key": f"staged-{staged}",
                }).scalar_one()
                staged_street.execute(text("""INSERT INTO coverage_source_runs
                    (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                     supported_sample_count,unsupported_sample_count,matched_node_count)
                    VALUES (:account,:source,1,:dataset,:job,'succeeded',1,1,0,1)"""), {
                    "account": self.account, "source": source_id, "dataset": staged, "job": job_id,
                })
                staged_street.execute(text("""INSERT INTO source_node_contributions
                    (account_id,source_id,source_revision,dataset_id,node_id)
                    VALUES (:account,:source,1,:dataset,:node)"""), {
                    "account": self.account, "source": source_id, "dataset": staged, "node": TINY_NODES[0],
                })
        finally:
            staged_street.close()
        staged_ready = self.client.get("/api/cities", params={"dataset_id": staged},
                                       headers=self.headers()).json()
        self.assertEqual(staged_ready["coverage"]["status"], "ready")
        self.assertEqual(staged_ready["items"][0]["eligible_streets"], 1)
        self.assertEqual(staged_ready["items"][0]["eligible_nodes"], 3)
        active = self.client.get("/api/cities", params={"dataset_id": self.dataset},
                                 headers=self.headers()).json()
        self.assertEqual(active["items"][0]["eligible_streets"], 2)
        self.assertEqual(active["items"][0]["eligible_nodes"], 14)

        first_page = self.client.get(f"/api/cities/{self.city_id}/streets", params={
            "dataset_id": self.dataset, "page": 1, "page_size": 1,
        }, headers=self.headers()).json()
        second_page = self.client.get(f"/api/cities/{self.city_id}/streets", params={
            "dataset_id": self.dataset, "page": 2, "page_size": 1,
        }, headers=self.headers()).json()
        self.assertEqual(first_page["total"], 2)
        self.assertNotEqual(first_page["items"][0]["id"], second_page["items"][0]["id"])

        with self.engine.begin() as db:
            db.execute(text("UPDATE map_datasets SET status='retired' WHERE id=:id"), {"id": self.dataset})
            db.execute(text("UPDATE map_datasets SET status='active' WHERE id=:id"), {"id": staged})
        retired = self.client.get("/api/cities", params={"dataset_id": self.dataset},
                                  headers=self.headers()).json()
        self.assertEqual(retired["dataset_state"], "retired")
        self.assertEqual(retired["coverage"]["status"], "ready")
        self.assertEqual(retired["items"][0]["eligible_nodes"], 14)

        page = self.client.get("/api/cities", params={"dataset_id": self.dataset, "q": "%"},
                               headers=self.headers()).json()
        self.assertEqual(page["total"], 0)  # % is literal, not a wildcard.
        self.assertEqual(self.client.get("/api/cities", headers=self.headers()).status_code, 422)
        for params in ({"dataset_id": 9_223_372_036_854_775_808},
                       {"dataset_id": self.dataset, "page_size": 101},
                       {"dataset_id": self.dataset, "q": "x" * 201}):
            self.assertEqual(self.client.get("/api/cities", params=params,
                                             headers=self.headers()).status_code, 422)
        self.assertEqual(self.client.get("/api/cities", params={"dataset_id": 999_999_999},
                                         headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get(f"/api/cities/{self.city_id}/streets", params={
            "dataset_id": staged, "page_size": 101,
        }, headers=self.headers()).status_code, 422)
        self.assertEqual(self.client.get(f"/api/cities/9223372036854775808/streets",
            params={"dataset_id": staged}, headers=self.headers()).status_code, 422)
        self.assertEqual(self.client.get("/api/streets/9223372036854775808",
            params={"dataset_id": staged}, headers=self.headers()).status_code, 422)
        self.assertEqual(self.client.get(f"/api/streets/{self.streets['Long Road']}", params={
            "dataset_id": staged,
        }, headers=self.headers()).status_code, 404)
        with self.engine.connect() as db:
            staged_city = int(db.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset"), {
                "dataset": staged,
            }).scalar_one())
        self.assertEqual(self.client.get(f"/api/cities/{staged_city}/streets",
            params={"dataset_id": staged, "q": "%"}, headers=self.headers()).json()["total"], 0)


if __name__ == "__main__":
    unittest.main()
