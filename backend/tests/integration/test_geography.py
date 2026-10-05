import os
import tempfile
import unittest
import uuid
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from backend.app.geography import find_cities, find_nearby_nodes, import_osm_xml


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = __import__("re").compile(r"city_runner_test_[0-9a-f]{12}\Z")


def _fixture_xml(source_note="initial", include_roads=True) -> bytes:
    root = Element("osm", {"version": "0.6", "generator": "city-runner-test"})
    coords = {
        1: (0, 0), 2: (10, 0), 3: (10, 10), 4: (0, 10),
        5: (4, 4), 6: (6, 4), 7: (6, 6), 8: (4, 6),
        9: (20, 20), 10: (22, 20), 11: (22, 22), 12: (20, 22),
        13: (30, 30), 14: (32, 30), 15: (32, 32), 16: (30, 32),
        10001: (1, 1), 10002: (2, 1), 10003: (3, 1), 10004: (4, 1),
        10005: (5, 5), 10006: (10, 5), 10007: (11, 5), 10008: (7, 5),
        10009: (3, 5), 10010: (2, 4), 10011: (2, 5), 10012: (2, 6), 10013: (3, 6),
    }
    for node_id, (lon, lat) in coords.items():
        SubElement(root, "node", {"id": str(node_id), "lon": str(lon), "lat": str(lat)})

    ways = [
        (100, [1, 2], {}), (101, [3, 2], {}), (102, [3, 4], {}), (103, [4, 1], {}),
        (200, [5, 6, 7, 8, 5], {}),
        (300, [9, 10, 11, 12, 9], {}),
        (400, [13, 14, 15, 16, 13], {}),
        (500, [10001, 10002], {"highway": "residential", "name": "Main Street"}),
        (501, [10002, 10003], {"highway": "residential", "name": "Main Street"}),
        (502, [10006, 10007], {"highway": "residential", "name": "Border Road"}),
        (503, [10005, 10008], {"highway": "residential", "name": "Hole Road"}),
        (504, [10002, 10004], {"highway": "residential", "name": "Cross Road"}),
        (505, [10012, 10013], {"highway": "residential", "name": "Private Foot", "access": "private", "foot": "yes"}),
        (506, [10012, 10013], {"highway": "residential", "name": "Foot Prohibited", "foot": "no"}),
        (507, [10012, 10013], {"highway": "construction", "name": "Construction"}),
        (508, [10012, 10013], {"highway": "residential"}),
    ]
    for way_id, refs, tags in ways:
        if not include_roads and way_id >= 500:
            continue
        way = SubElement(root, "way", {"id": str(way_id)})
        for ref in refs:
            SubElement(way, "nd", {"ref": str(ref)})
        for key, value in tags.items():
            SubElement(way, "tag", {"k": key, "v": value})

    relation = SubElement(root, "relation", {"id": "900"})
    for way_id, role in ((100, "outer"), (101, "outer"), (102, "outer"), (103, "outer"),
                         (200, "inner"), (300, "outer")):
        SubElement(relation, "member", {"type": "way", "ref": str(way_id), "role": role})
    SubElement(relation, "member", {"type": "node", "ref": "10001", "role": "admin_centre"})
    SubElement(relation, "member", {"type": "relation", "ref": "9999", "role": "subarea"})
    for key, value in {"type": "boundary", "boundary": "administrative", "admin_level": "8",
                       "name": "Fixture City"}.items():
        SubElement(relation, "tag", {"k": key, "v": value})

    second = SubElement(root, "relation", {"id": "901"})
    SubElement(second, "member", {"type": "way", "ref": "400", "role": "outer"})
    for key, value in {"type": "boundary", "boundary": "administrative", "admin_level": "8",
                       "name": "Other City"}.items():
        SubElement(second, "tag", {"k": key, "v": value})
    if source_note != "initial":
        SubElement(root, "tag", {"k": "fixture_revision", "v": source_note})
    return tostring(root, encoding="utf-8", xml_declaration=True)


@unittest.skipUnless(DATABASE_URL, "set POSTGIS_TEST_DATABASE_URL via the PostGIS test runner")
class GeographyPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(DATABASE_URL)
        if not DATABASE_PATTERN.fullmatch(url.database or ""):
            raise RuntimeError("Integration tests require a UUID-named disposable city_runner_test database")
        cls.engine = create_engine(DATABASE_URL)
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
        self.region = f"test-{uuid.uuid4().hex}"
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fixture.osm"
        self.path.write_bytes(_fixture_xml())

    def tearDown(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM map_datasets WHERE region=:region"), {"region": self.region})
        self.tmp.cleanup()

    def test_holes_components_border_nodes_grouping_and_indexed_lookups(self):
        imported = import_osm_xml(self.engine, self.path, region=self.region,
                                  city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z",
                                  coverage_mode="complete", coverage_evidence="Synthetic complete fixture")
        self.assertEqual(imported["status"], "active")
        self.assertTrue(imported["validated"])
        dataset_id = imported["dataset_id"]
        self.assertEqual(find_cities(self.engine, dataset_id, lon=1, lat=1)[0]["name"], "Fixture City")
        self.assertEqual(find_cities(self.engine, dataset_id, lon=21, lat=21)[0]["name"], "Fixture City")
        self.assertEqual(find_cities(self.engine, dataset_id, lon=5, lat=5), [])  # inner hole
        self.assertEqual(find_cities(self.engine, dataset_id, lon=23, lat=21), [])

        with self.engine.connect() as db:
            streets = db.execute(text("""
                SELECT s.display_name,s.eligible_node_count,count(DISTINCT sw.osm_way_id) AS ways
                FROM streets s LEFT JOIN street_ways sw
                  ON s.dataset_id=sw.dataset_id AND s.id=sw.street_id
                WHERE s.dataset_id=:dataset GROUP BY s.id ORDER BY s.display_name
            """), {"dataset": dataset_id}).mappings().all()
            self.assertEqual({row["display_name"] for row in streets},
                             {"Main Street", "Border Road", "Hole Road", "Cross Road"})
            main = next(row for row in streets if row["display_name"] == "Main Street")
            self.assertEqual((main["eligible_node_count"], main["ways"]), (3, 2))
            nodes = db.execute(text("""
                SELECT osm_node_id FROM osm_nodes WHERE dataset_id=:dataset ORDER BY osm_node_id
            """), {"dataset": dataset_id}).scalars().all()
            self.assertEqual(nodes, [10001, 10002, 10003, 10004, 10006, 10008])
            self.assertEqual(db.execute(text("""
                SELECT count(*) FROM osm_nodes WHERE dataset_id=:dataset AND osm_node_id=10002
            """), {"dataset": dataset_id}).scalar_one(), 1)
            indexes = set(db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename IN ('cities','osm_nodes')")).scalars())
            self.assertIn("ix_cities_boundary_gist", indexes)
            self.assertIn("ix_osm_nodes_point_gist", indexes)
            db.execute(text("SET LOCAL enable_seqscan=off"))
            city_plan = " ".join(db.execute(text("""
                EXPLAIN SELECT id FROM cities WHERE dataset_id=:dataset
                  AND boundary && ST_SetSRID(ST_MakePoint(1,1),4326)
                  AND ST_Covers(boundary,ST_SetSRID(ST_MakePoint(1,1),4326))
            """), {"dataset": dataset_id}).scalars())
            node_plan = " ".join(db.execute(text("""
                EXPLAIN SELECT osm_node_id FROM osm_nodes
                WHERE dataset_id=:dataset AND ST_DWithin(point,
                  ST_SetSRID(ST_MakePoint(1,1),4326)::geography,25)
            """), {"dataset": dataset_id}).scalars())
        self.assertIn("ix_cities_boundary_gist", city_plan)
        self.assertIn("ix_osm_nodes_point_gist", node_plan)

        near = find_nearby_nodes(self.engine, dataset_id, lon=1, lat=1, radius_m=25, limit=1)
        self.assertEqual(len(near), 1)
        self.assertEqual(near[0]["osm_node_id"], 10001)
        for kwargs in ({"lon": 181, "lat": 0}, {"lon": 1, "lat": 1, "limit": 501},
                       {"lon": 1, "lat": 1, "radius_m": 1001}):
            options = dict(kwargs)
            if "lon" not in options:
                options["lon"] = 1
            if "lat" not in options:
                options["lat"] = 1
            with self.subTest(options=options), self.assertRaises(ValueError):
                find_nearby_nodes(self.engine, dataset_id, **options)

    def test_repeat_selection_is_idempotent_and_replacement_stays_staged(self):
        first = import_osm_xml(self.engine, self.path, region=self.region,
                               city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z",
                               coverage_mode="complete", coverage_evidence="Synthetic complete fixture")
        repeated = import_osm_xml(self.engine, self.path, region=self.region,
                                  city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z",
                                  coverage_mode="complete", coverage_evidence="Synthetic complete fixture")
        self.assertEqual(repeated["dataset_id"], first["dataset_id"])
        self.assertTrue(repeated["reused"])

        selection = import_osm_xml(self.engine, self.path, region=self.region,
                                   city_relation_ids=[901, 900], source_timestamp="2026-10-01T00:00:00Z",
                                   coverage_mode="sampled")
        self.assertNotEqual(selection["dataset_id"], first["dataset_id"])
        self.assertEqual(selection["status"], "importing")
        self.assertTrue(selection["validated"])
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""
                SELECT count(*) FROM cities WHERE dataset_id=:dataset
            """), {"dataset": selection["dataset_id"]}).scalar_one(), 2)
            self.assertEqual(db.execute(text("""
                SELECT id FROM map_datasets WHERE region=:region AND status='active'
            """), {"region": self.region}).scalar_one(), first["dataset_id"])

        self.path.write_bytes(_fixture_xml("replacement"))
        replacement = import_osm_xml(self.engine, self.path, region=self.region,
                                     city_relation_ids=[900], source_timestamp="2026-10-02T00:00:00Z",
                                     coverage_mode="complete", coverage_evidence="Synthetic complete fixture")
        self.assertEqual(replacement["status"], "importing")
        self.assertTrue(replacement["validated"])
        with self.engine.connect() as db:
            active = db.execute(text("SELECT id FROM map_datasets WHERE region=:region AND status='active'"),
                                {"region": self.region}).scalar_one()
            self.assertEqual(active, first["dataset_id"])

    def test_bad_boundary_rolls_back_and_membership_cannot_cross_dataset(self):
        broken = _fixture_xml().replace(b'ref="103"', b'ref="999999"')
        self.path.write_bytes(broken)
        with self.assertRaisesRegex(ValueError, "incomplete way|missing way"):
            import_osm_xml(self.engine, self.path, region=self.region,
                           city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM map_datasets WHERE region=:region"),
                                        {"region": self.region}).scalar_one(), 0)

        self.path.write_bytes(_fixture_xml())
        first = import_osm_xml(self.engine, self.path, region=self.region,
                               city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z",
                               coverage_mode="complete", coverage_evidence="Synthetic complete fixture")
        staged = import_osm_xml(self.engine, self.path, region=self.region,
                                city_relation_ids=[901], source_timestamp="2026-10-01T00:00:00Z")
        with self.engine.begin() as db:
            with self.assertRaises(IntegrityError):
                with db.begin_nested():
                    db.execute(text("""
                        INSERT INTO street_ways(dataset_id,street_id,osm_way_id)
                        SELECT :staged,street_id,osm_way_id FROM street_ways
                        WHERE dataset_id=:active LIMIT 1
                    """), {"staged": staged["dataset_id"], "active": first["dataset_id"]})

    def test_sampled_boundary_only_stays_staged_and_cannot_claim_complete(self):
        self.path.write_bytes(_fixture_xml(include_roads=False))
        sampled = import_osm_xml(self.engine, self.path, region=self.region,
                                 city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z")
        self.assertEqual(sampled["status"], "importing")
        self.assertEqual(sampled["coverage_mode"], "sampled")
        with self.assertRaisesRegex(ValueError, "no eligible named roads"):
            import_osm_xml(self.engine, self.path, region=self.region, city_relation_ids=[900],
                           source_timestamp="2026-10-01T00:00:00Z", coverage_mode="complete",
                           coverage_evidence="Synthetic fixture attestation")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""
                SELECT count(*) FROM map_datasets WHERE region=:region AND status='active'
            """), {"region": self.region}).scalar_one(), 0)
            self.assertEqual(db.execute(text("""
                SELECT count(*) FROM map_datasets WHERE region=:region
            """), {"region": self.region}).scalar_one(), 1)


if __name__ == "__main__":
    unittest.main()
