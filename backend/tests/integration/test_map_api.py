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
from backend.app.coverage import rebuild_account_coverage
from backend.app.main import create_app
from backend.app.map_api import BBox


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


def _city_fixture():
    root = Element("osm", {"version": "0.6", "generator": "map-api-test"})
    coords = {
        1: (0, 0), 2: (2, 0), 3: (2, 2), 4: (0, 2),
        10001: (0.25, 0.5), 10002: (0.75, 0.5), 10003: (1.25, 0.5),
    }
    for node_id, (lon, lat) in coords.items():
        SubElement(root, "node", {"id": str(node_id), "lon": str(lon), "lat": str(lat)})
    ways = [(10, [1, 2]), (11, [2, 3]), (12, [3, 4]), (13, [4, 1]),
            (20, [10001, 10002, 10003])]
    for way_id, refs in ways:
        way = SubElement(root, "way", {"id": str(way_id)})
        for ref in refs:
            SubElement(way, "nd", {"ref": str(ref)})
        if way_id == 20:
            SubElement(way, "tag", {"k": "highway", "v": "residential"})
            SubElement(way, "tag", {"k": "name", "v": "Map Road"})
    relation = SubElement(root, "relation", {"id": "900"})
    for way_id in (10, 11, 12, 13):
        SubElement(relation, "member", {"type": "way", "ref": str(way_id), "role": "outer"})
    for key, value in {"type": "boundary", "boundary": "administrative", "admin_level": "8",
                       "name": "Map City"}.items():
        SubElement(relation, "tag", {"k": key, "v": value})
    return tostring(root, encoding="utf-8", xml_declaration=True)


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class MapApiPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("map API tests require the generated disposable PostGIS database")
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
        self.account = f"map-{uuid.uuid4().hex}"
        self.other = f"map-other-{uuid.uuid4().hex}"
        self.region = f"map-region-{uuid.uuid4().hex}"
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
            coverage_evidence="Synthetic complete city fixture",
        )["dataset_id"]
        self.app = create_app(self.engine)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id IN (:a,:b)"), {"a": self.account, "b": self.other})
            db.execute(text("DELETE FROM accounts WHERE id IN (:a,:b)"), {"a": self.account, "b": self.other})
            db.execute(text("DELETE FROM map_datasets WHERE id=:id"), {"id": self.dataset})
        self.tmp.cleanup()

    def headers(self, account):
        return {"Authorization": f"Bearer {self.tokens[account]}"}

    def add_activity(self, account, *, tracks, activity_id=None, source_status="succeeded"):
        activity_id = activity_id or (9007199254740993 if account == self.account else 9007199254740995)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,'Synthetic run','2026-10-05','run',false,2,CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                "id": activity_id, "account": account,
                "tracks": json.dumps(tracks),
                "timestamps": json.dumps([[None for _ in segment] for segment in tracks]),
            })
            if source_status:
                db.execute(text("""INSERT INTO activity_sources
                    (account_id,activity_id,source_kind,status) VALUES (:account,:id,'gpx',:status)"""), {
                    "account": account, "id": activity_id, "status": source_status,
                })
        return activity_id

    def add_successful_source(self, activity_id, visited_nodes):
        with self.engine.begin() as db:
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,status) VALUES (:account,:activity,'gpx','succeeded')
                RETURNING id"""), {"account": self.account, "activity": activity_id}).scalar_one()
            job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,status,attempts,max_attempts)
                VALUES (:account,'coverage.match',:key,'succeeded',1,5) RETURNING id"""), {
                    "account": self.account, "key": str(source_id),
                }).scalar_one()
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,sample_count,
                 supported_sample_count,unsupported_sample_count,matched_node_count)
                VALUES (:account,:source,1,:dataset,:job,'succeeded',:samples,:samples,0,:samples)"""), {
                    "account": self.account, "source": source_id, "dataset": self.dataset,
                    "job": job_id, "samples": len(visited_nodes),
                })
            for node_id in visited_nodes:
                db.execute(text("""INSERT INTO source_node_contributions
                    (account_id,source_id,source_revision,dataset_id,node_id)
                    VALUES (:account,:source,1,:dataset,:node)"""), {
                        "account": self.account, "source": source_id,
                        "dataset": self.dataset, "node": node_id,
                    })
        rebuild_account_coverage(self.engine, self.account, self.dataset)
        return source_id

    def test_bbox_validation_and_owner_scoped_clipped_legacy_tracks(self):
        for value in ("179,0,-179,1", "nan,0,1,1", "-90,-90,90,90", "0,1,2,1"):
            with self.subTest(bbox=value):
                response = self.client.get("/api/map", params={"bbox": value, "zoom": 16},
                    headers=self.headers(self.account))
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 16}).status_code, 401)

        alice_id = self.add_activity(self.account, tracks=[[[ -1, .5], [.5, .5]], [[1.5, .5], [3, .5]]], source_status=None)
        bob_id = self.add_activity(self.other, tracks=[[[.5, .75], [1.5, .75]]], source_status=None)
        response = self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 16},
                                   headers=self.headers(self.account))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["geography_state"], "supported")
        self.assertEqual([track["activity_id"] for track in body["tracks"]], [str(alice_id)])
        self.assertEqual(body["tracks"][0]["geometry"]["type"], "MultiLineString")
        self.assertEqual(len(body["tracks"][0]["geometry"]["coordinates"]), 2)
        self.assertEqual(body["streets"][0]["visited_nodes"], 0)
        self.assertEqual(body["node_state"], "ready")

        other = self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 16},
                                 headers=self.headers(self.other))
        self.assertEqual([track["activity_id"] for track in other.json()["tracks"]], [str(bob_id)])

    def test_bbox_candidate_budget_marks_false_positive_exhaustion_truncated(self):
        self.add_activity(self.account, tracks=[[[.5,.5],[1,.5]]], activity_id=900)
        false_positive_ids = range(1000, 1051)
        for activity_id in false_positive_ids:
            self.add_activity(self.account, tracks=[[[-1,.9],[.9,-1]]], activity_id=activity_id)
        response = self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 16},
                                   headers=self.headers(self.account))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tracks"], [])
        self.assertTrue(response.json()["limits"]["tracks"]["truncated"])

        healthy = self.client.get("/api/map", params={"bbox": "5,5,6,6", "zoom": 16},
                                  headers=self.headers(self.account))
        self.assertEqual(healthy.status_code, 200, healthy.text)
        self.assertEqual(healthy.json()["tracks"], [])
        self.assertFalse(healthy.json()["limits"]["tracks"]["truncated"])

    def test_pending_import_and_progress_are_separate_and_ready_zero_shows_missing_nodes(self):
        self.add_activity(self.account, tracks=[[[.25,.5],[.75,.5]]], source_status="succeeded")
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activity_sources(account_id,source_kind,status)
                VALUES (:account,'gpx','queued')"""), {"account": self.account})
        response = self.client.get("/api/progress", headers=self.headers(self.account))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["state"], "pending")
        self.assertEqual(body["pending_imports"], 1)
        self.assertEqual(body["datasets"][0]["pending_sources"], 1)
        self.assertIsNone(body["datasets"][0]["eligible_streets"])

        ready_zero = self.client.get("/api/progress", headers=self.headers(self.other))
        self.assertEqual(ready_zero.json()["state"], "ready")
        self.assertEqual(ready_zero.json()["datasets"][0]["visited_node_count"], 0)
        missing = self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 16},
                                  headers=self.headers(self.other))
        self.assertEqual(missing.json()["node_state"], "ready")
        self.assertEqual(len(missing.json()["missing_nodes"]), 3)

    def test_live_progress_survives_overlapping_source_delete_and_ignores_zero_node_street(self):
        first_activity = self.add_activity(self.account, tracks=[[[.25,.5],[.75,.5]]], source_status=None)
        second_activity = self.add_activity(self.account, tracks=[[[.75,.5],[1.25,.5]]],
                                            activity_id=9007199254740997, source_status=None)
        self.add_successful_source(first_activity, [10001, 10002])
        overlap_source = self.add_successful_source(second_activity, [10001])
        with self.engine.begin() as db:
            city_id = db.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset"),
                                 {"dataset": self.dataset}).scalar_one()
            zero_way = 800
            db.execute(text("""INSERT INTO osm_ways(dataset_id,osm_way_id,name,tags,geometry)
                VALUES (:dataset,:way,'Zero Node','{}'::jsonb,
                  ST_GeomFromText('MULTILINESTRING((0.2 0.8,0.8 0.8))',4326))"""), {
                    "dataset": self.dataset, "way": zero_way,
                })
            zero_street = db.execute(text("""INSERT INTO streets
                (dataset_id,city_id,normalized_name,display_name,eligible_node_count)
                VALUES (:dataset,:city,'Zero Node','Zero Node',0) RETURNING id"""), {
                    "dataset": self.dataset, "city": city_id,
                }).scalar_one()
            db.execute(text("""INSERT INTO street_ways(dataset_id,street_id,osm_way_id)
                VALUES (:dataset,:street,:way)"""), {
                    "dataset": self.dataset, "street": zero_street, "way": zero_way,
                })
            db.execute(text("UPDATE activities SET unmapped_points=0 WHERE user_id=:account"),
                       {"account": self.account})
        before = self.client.get("/api/progress", headers=self.headers(self.account)).json()
        self.assertEqual(before["datasets"][0]["visited_node_count"], 2)
        self.assertEqual(before["datasets"][0]["eligible_streets"], 1)
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=:source"), {
                "account": self.account, "source": overlap_source,
            })
        after = self.client.get("/api/progress", headers=self.headers(self.account)).json()
        self.assertEqual(after["state"], "ready")
        self.assertEqual(after["datasets"][0]["visited_node_count"], 2)
        self.assertEqual(after["datasets"][0]["eligible_streets"], 1)
        self.assertEqual(after["datasets"][0]["completed_streets"], 0)
        self.assertEqual(after["unmapped_points"], 0)
        view = self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 16},
                               headers=self.headers(self.account)).json()
        self.assertEqual(len(view["streets"]), 1)
        self.assertEqual(view["streets"][0]["visited_nodes"], 2)
        self.assertEqual(len(view["missing_nodes"]), 1)

    def test_00_track_cache_backfills_maintains_segments_and_bounds_complex_tracks(self):
        huge_id = 9223372036854770000
        many_points = [[0.1 + i * 1e-7, 0.5 + (i % 2) * 0.0001] for i in range(100000)]
        command.downgrade(self.config, "0006_coverage")
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,'Large legacy run','2026-10-05','run',false,0,
                  CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                "id": huge_id, "account": self.account,
                "tracks": json.dumps([many_points]),
                "timestamps": json.dumps([[None for _ in many_points]]),
            })
        command.upgrade(self.config, "head")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT ST_NPoints(track_geometry) FROM activities WHERE id=:id"),
                                        {"id": huge_id}).scalar_one(), 100000)
            indexes = set(db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='activities'")).scalars())
            self.assertIn("ix_activities_track_geometry_gist", indexes)
            db.execute(text("SET LOCAL enable_seqscan=off"))
            # Ownership can legitimately use its B-tree; check the spatial predicate independently.
            plan = " ".join(db.execute(text("""EXPLAIN SELECT id FROM activities
                WHERE track_geometry && ST_MakeEnvelope(0,0,2,2,4326)""")).scalars())
            self.assertIn("ix_activities_track_geometry_gist", plan)

        capped = self.client.get("/api/map", params={"bbox": "0,0,2,2", "zoom": 24},
                                 headers=self.headers(self.account))
        self.assertEqual(capped.status_code, 200, capped.text)
        self.assertEqual(capped.json()["limits"]["tracks"]["returned"], 0)
        self.assertTrue(capped.json()["limits"]["tracks"]["truncated"])

        with self.engine.begin() as db:
            db.execute(text("UPDATE activities SET tracks=CAST(:tracks AS json) WHERE id=:id"), {
                "id": huge_id, "tracks": json.dumps([[[0.2,0.5]]]),
            })
            self.assertEqual(db.execute(text("SELECT GeometryType(track_geometry),ST_NPoints(track_geometry) FROM activities WHERE id=:id"),
                                        {"id": huge_id}).one(), ("POINT", 1))
            db.execute(text("UPDATE activities SET tracks=CAST(:tracks AS json) WHERE id=:id"), {
                "id": huge_id, "tracks": json.dumps([[[0.2,0.5],["bad",0.6],[0.8,0.5]]]),
            })
            self.assertIsNone(db.execute(text("SELECT track_geometry FROM activities WHERE id=:id"),
                                          {"id": huge_id}).scalar_one())


class MapBboxTests(unittest.TestCase):
    def test_bbox_rejects_dateline_wrap_and_area_and_accepts_local_viewport(self):
        self.assertEqual(BBox.parse("3.6,50.9,3.8,51.1").values, (3.6, 50.9, 3.8, 51.1))
        with self.assertRaisesRegex(ValueError, "antimeridian"):
            BBox.parse("179,0,-179,1")
        with self.assertRaisesRegex(ValueError, "area"):
            BBox.parse("-20,-20,20,20")


if __name__ == "__main__":
    unittest.main()
