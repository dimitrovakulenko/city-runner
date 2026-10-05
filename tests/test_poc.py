import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from poc.core import Store, distance, geometry_roads, parse_gpx, relation_polygon


def relation(city_id, west, east, name="Test city"):
    ring = [(west, 50.849), (east, 50.849), (east, 50.851), (west, 50.851), (west, 50.849)]
    return {"id": city_id, "tags": {"name": name, "admin_level": "8"}, "members": [
        {"type": "way", "role": "outer", "geometry": [{"lon": lon, "lat": lat} for lon, lat in ring]}]}


def roads(points, name="Test street", way_id=1):
    return {"elements": [{"type": "way", "id": way_id, "nodes": [point["id"] for point in points],
                          "tags": {"name": name, "highway": "residential"}}] + [point | {"type": "node"} for point in points]}


def gpx(points):
    return ('<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>' +
            ''.join(f'<trkpt lon="{lon}" lat="{lat}"/>' for lon, lat in points) + '</trkseg></trk></gpx>').encode()


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_gpx_segments_invalid_positions_and_no_gap_interpolation(self):
        xml = b'<gpx><trk><trkseg><trkpt lon="4.35" lat="50.85"/><trkpt lon="nan" lat="50.85"/></trkseg><trkseg><trkpt lon="4.36" lat="50.85"/></trkseg></trk></gpx>'
        self.assertEqual(parse_gpx(xml), [[[4.35, 50.85]], [[4.36, 50.85]]])
        self.store.add_city(relation(1, 4.34, 4.37), roads([
            {"id": 1, "lon": 4.35, "lat": 50.85}, {"id": 2, "lon": 4.355, "lat": 50.85},
            {"id": 3, "lon": 4.36, "lat": 50.85}]))
        self.store.add_activity(1, "GPS gap", "2026-10-05", xml)
        self.store.process(1, None)
        street = self.store.streets()[0]
        self.assertEqual(street["visited"], 2)
        self.assertFalse(street["complete"])

    def test_gpx_metadata_keeps_namespaced_point_timestamps_and_activity_metadata(self):
        xml = (b'<gpx xmlns="http://www.topografix.com/GPX/1/1"><metadata><time>2026-10-04T23:59:00Z</time></metadata>'
               b'<trk><type>Running</type><trkseg><trkpt lon="4.35" lat="50.85"><time>2026-10-05T00:01:02Z</time></trkpt>'
               b'<trkpt lon="4.36" lat="50.85"><time>2026-10-05T00:02:03+02:00</time></trkpt></trkseg></trk></gpx>')
        self.assertEqual(parse_gpx(xml), [[[4.35, 50.85], [4.36, 50.85]]])
        self.store.add_activity(1, "Namespaced", "", xml)
        with self.store.db() as db:
            row = db.execute("SELECT date,activity_type,timestamps FROM activities WHERE id=1").fetchone()
        self.assertEqual(row["date"], "2026-10-05")
        self.assertEqual(row["activity_type"], "running")
        self.assertEqual(json.loads(row["timestamps"]), [["2026-10-05T00:01:02Z", "2026-10-05T00:02:03+02:00"]])

    def test_missing_and_invalid_gpx_metadata_are_unknown(self):
        for activity_id, xml in ((1, b'<gpx><trk><trkseg><trkpt lon="4.35" lat="50.85"/></trkseg></trk></gpx>'),
                                 (2, b'<gpx><metadata><time>yesterday</time></metadata><trk><type> </type><trkseg>'
                                     b'<trkpt lon="4.35" lat="50.85"><time>bad timestamp</time></trkpt></trkseg></trk></gpx>')):
            self.store.add_activity(activity_id, "Unknown", "invalid date", xml)
        with self.store.db() as db:
            rows = db.execute("SELECT date,activity_type,timestamps FROM activities ORDER BY id").fetchall()
        self.assertEqual([(row["date"], row["activity_type"], json.loads(row["timestamps"])) for row in rows],
                         [("unknown", "unknown", [[None]]), ("unknown", "unknown", [[None]])])

    def test_existing_database_rows_survive_metadata_migration(self):
        path = self.store.path
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE activities")
            db.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY, name TEXT, date TEXT, tracks TEXT, processed INTEGER DEFAULT 0, unmapped INTEGER DEFAULT 0)")
            db.execute("INSERT INTO activities VALUES (7,'Old row',NULL,'[[[4.35,50.85]]]',1,0)")
            db.execute("INSERT INTO hits VALUES (7,42)")
        self.store = Store(self.folder.name)
        with self.store.db() as db:
            row = db.execute("SELECT * FROM activities WHERE id=7").fetchone()
            hit = db.execute("SELECT node_id FROM hits WHERE activity_id=7").fetchone()
        self.assertEqual((row["name"], row["date"], row["processed"], row["activity_type"], json.loads(row["timestamps"])),
                         ("Old row", "unknown", 1, "unknown", []))
        self.assertEqual(hit["node_id"], 42)

    def test_twenty_five_metre_radius_and_repeated_import(self):
        self.store.add_city(relation(1, 4.34, 4.37), roads([
            {"id": 1, "lon": 4.35, "lat": 50.85}, {"id": 2, "lon": 4.351, "lat": 50.85}]))
        points = [(4.35, 50.8501), (4.351, 50.8503)]
        self.assertLess(distance(*points[0], 4.35, 50.85), 25)
        self.assertGreater(distance(*points[1], 4.351, 50.85), 25)
        self.store.add_activity(1, "Run", "2026-10-05", gpx(points))
        self.store.add_activity(1, "Duplicate", "2026-10-05", gpx(points))
        self.store.process(1, None)
        self.store.process(1, None)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hits").fetchone()[0], 1)

    def test_osm_geometry_preserves_node_ids_and_excludes_inaccessible_roads(self):
        tags = [{"name": "Allowed", "highway": "residential"},
                {"name": "Path", "highway": "footway"},
                {"name": "Private", "highway": "residential", "access": "private"},
                {"name": "No pedestrians", "highway": "residential", "foot": "no"},
                {"name": "Motorway", "highway": "motorway"}]
        ways = [{"type": "way", "id": index, "nodes": [1, 2], "tags": tag,
                 "geometry": [{"lon": 4.35, "lat": 50.85}, {"lon": 4.351, "lat": 50.85}]}
                for index, tag in enumerate(tags)]
        data = geometry_roads({"elements": ways})
        self.assertEqual([e["id"] for e in data["elements"] if e["type"] == "node"], [1, 2])
        self.store.add_city(relation(1, 4.34, 4.37), data)
        self.assertEqual({s["name"] for s in self.store.streets()}, {"Allowed", "Path"})
        ways[0]["geometry"] = ways[0]["geometry"][:1]
        with self.assertRaises(ValueError):
            geometry_roads({"elements": ways})

    def test_cross_city_same_name_and_all_small_street_nodes(self):
        self.store.add_city(relation(1, 4.34, 4.355, "First city"), roads([
            {"id": 1, "lon": 4.35, "lat": 50.85}, {"id": 2, "lon": 4.351, "lat": 50.85}]))
        self.store.add_city(relation(2, 4.355, 4.37, "Second city"), roads([
            {"id": 3, "lon": 4.36, "lat": 50.85}, {"id": 4, "lon": 4.361, "lat": 50.85}]))
        self.store.add_activity(1, "Two cities", "2026-10-05", gpx([(4.35, 50.85), (4.351, 50.85), (4.36, 50.85)]))
        self.store.process(1, None)
        cities = self.store.cities()
        self.assertEqual([(city["total"], city["completed"]) for city in cities], [(1, 1), (1, 0)])
        self.assertEqual(len({street["id"] for street in self.store.streets()}), 2)

    def test_ninety_percent_and_multiple_activities(self):
        self.store.add_city(relation(1, 4.34, 4.37), roads([
            {"id": index + 1, "lon": 4.35 + index * .001, "lat": 50.85} for index in range(10)]))
        self.store.add_activity(1, "First", "2026-10-04", gpx([(4.35 + i * .001, 50.85) for i in range(8)]))
        self.store.process(1, None)
        self.assertFalse(self.store.streets()[0]["complete"])
        self.store.add_activity(2, "Second", "2026-10-05", gpx([(4.358, 50.85)]))
        self.store.process(2, None)
        self.assertTrue(self.store.streets()[0]["complete"])
        self.assertEqual(self.store.streets()[0]["visited"], 9)

    def test_new_city_queues_earlier_tracks_for_border_nodes(self):
        self.store.add_city(relation(1, 4.34, 4.355), roads([
            {"id": 1, "lon": 4.3548, "lat": 50.85}, {"id": 2, "lon": 4.3549, "lat": 50.85}]))
        self.store.add_activity(1, "Border run", "2026-10-04", gpx([(4.3549, 50.85)]))
        self.store.process(1, None)
        self.store.add_city(relation(2, 4.355, 4.37), roads([
            {"id": 3, "lon": 4.3551, "lat": 50.85}, {"id": 4, "lon": 4.36, "lat": 50.85}]))
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT processed FROM activities WHERE id=1").fetchone()[0], 0)
        self.store.process(1, None)
        with self.store.db() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM hits WHERE activity_id=1 AND node_id=3").fetchone())

    def test_city_hole_and_missing_boundaries_are_visible(self):
        city = relation(1, 4.34, 4.37)
        city["members"].append({"type": "way", "role": "inner", "geometry": [
            {"lon": lon, "lat": lat} for lon, lat in [(4.349, 50.8495),(4.351,50.8495),(4.351,50.8505),(4.349,50.8505),(4.349,50.8495)]]})
        from shapely.geometry import Point
        self.assertFalse(relation_polygon(city).covers(Point(4.35, 50.85)))
        self.store.add_activity(1, "Unknown area", "2026-10-05", gpx([(4.35, 50.85)]))
        class NoCity:
            def city(self, lon, lat):
                return None
        self.store.process(1, NoCity())
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT unmapped FROM activities").fetchone()[0], 1)
        self.assertEqual(self.store.cities(), [])

    def test_auth_map_errors_and_sync_csrf(self):
        from poc.app import create_app
        with TestClient(create_app(self.store, password="test-only", poll=False)) as client:
            self.assertEqual(client.get("/api/status").status_code, 401)
            client.auth = ("poc", "wrong")
            self.assertEqual(client.get("/api/status").status_code, 401)
            client.auth = ("poc", "test-only")
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/api/status").status_code, 200)
            self.assertEqual(client.get("/api/map?bbox=nan,0,1,2").status_code, 400)
            self.assertEqual(client.get("/api/map?bbox=0,0,1,1&zoom=14").json()["nodes"]["features"], [])
            self.assertEqual(client.post("/api/sync").status_code, 403)

    def test_gpx_upload_completes_street_rejects_invalid_and_deduplicates(self):
        from poc.app import create_app
        self.store.add_city(relation(1, 4.34, 4.37), roads([
            {"id": 1, "lon": 4.35, "lat": 50.85}, {"id": 2, "lon": 4.351, "lat": 50.85}]))
        self.store.add_activity(1, "Garmin fixture", "2026-10-05", gpx([(4.35, 50.85)]))
        self.store.process(1, None)
        self.assertFalse(self.store.streets()[0]["complete"])
        headers = {"X-PoC-Request": "1", "Content-Type": "application/gpx+xml"}
        with TestClient(create_app(self.store, password="test-only", poll=False)) as client:
            client.auth = ("poc", "test-only")
            self.assertEqual(client.post("/api/import", content=b"<gpx/>").status_code, 403)
            for content in (b"broken", b"<gpx/>", b'<other><trkpt lon="4.35" lat="50.85"/></other>'):
                self.assertEqual(client.post("/api/import", content=content, headers=headers).status_code, 400)
            self.assertEqual(client.post("/api/import", content=b"x" * (10 * 1024 * 1024 + 1), headers=headers).status_code, 413)
            content = gpx([(4.35, 50.85), (4.351, 50.85)])
            for duplicate in (False, True):
                response = client.post("/api/import?name=export.gpx", content=content, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["duplicate"], duplicate)
                self.assertEqual(response.json()["points"], 2)
                self.assertEqual(response.json()["bounds"], [[4.35, 50.85], [4.351, 50.85]])
                for _ in range(100):
                    if not client.get("/api/status").json()["sync"]["running"]:
                        break
                    time.sleep(.01)
                self.assertFalse(client.get("/api/status").json()["sync"]["running"])
                self.assertTrue(self.store.streets()[0]["complete"])
                self.assertEqual(client.get("/api/status").json()["counts"]["activities"], 2)

    def test_pending_location_consent_keeps_gpx_matching_offline(self):
        from poc.app import create_app
        self.store.set("location_lookup_pending", True)
        with patch("poc.core.requests.request", side_effect=AssertionError("Location must stay local")) as network:
            with TestClient(create_app(self.store, password="test-only", poll=False)) as client:
                client.auth = ("poc", "test-only")
                response = client.post("/api/import", content=gpx([(4.35, 50.85)]), headers={"X-PoC-Request": "1"})
                self.assertEqual(response.status_code, 200)
                for _ in range(100):
                    status = client.get("/api/status").json()
                    if not status["sync"]["running"]:
                        break
                    time.sleep(.01)
                self.assertFalse(status["sync"]["running"])
                self.assertTrue(status["location_lookup_pending"])
                self.assertEqual(status["counts"]["unmapped_points"], 1)
                self.assertIsNone(status["processing_error"])
                network.assert_not_called()

    def test_sync_reimports_nothing_and_retries_failed_history(self):
        from poc.garmin_sync import sync
        path = self.store.directory / "garmin"
        path.mkdir()
        (path / "garmin_tokens.json").write_text("{}")
        class FakeGarmin:
            class ActivityDownloadFormat:
                GPX = "gpx"
            fail = False
            downloads = 0
            def login(self, path):
                pass
            def get_full_name(self):
                return "Fixture account"
            def get_activities(self, start, limit):
                return [{"activityId": 1, "activityName": "Indoor", "activityType": {"typeKey": "running"}, "hasPolyline": not self.fail}]
            def download_activity(self, activity_id, format):
                type(self).downloads += 1
                raise RuntimeError("Fixture failure")
        with patch("poc.garmin_sync.Garmin", FakeGarmin), patch("poc.garmin_sync.time.sleep"):
            FakeGarmin.fail = True
            sync(self.store)
            sync(self.store)
            with self.store.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 1)
            self.assertTrue(self.store.get("history_complete"))
            # A separate account with a failed download must not claim a completed historical import.
            other = Store(self.store.directory / "other")
            token_path = other.directory / "garmin"
            token_path.mkdir()
            (token_path / "garmin_tokens.json").write_text("{}")
            FakeGarmin.fail = False
            sync(other)
            self.assertFalse(other.get("history_complete", False))
            self.assertEqual(FakeGarmin.downloads, 1)

    def test_paginated_garmin_history_then_new_activity_updates_coverage(self):
        from poc.garmin_sync import sync
        path = self.store.directory / "garmin"
        path.mkdir()
        (path / "garmin_tokens.json").write_text("{}")
        class FakeGarmin:
            class ActivityDownloadFormat:
                GPX = "gpx"
            ids = list(range(101, 0, -1))
            downloads = []
            pages = []
            def login(self, path):
                pass
            def get_full_name(self):
                return "Fixture runner"
            def get_activities(self, start, limit):
                type(self).pages.append(start)
                return [{"activityId": id, "activityName": "Run", "activityType": {"typeKey": "running"}}
                        for id in self.ids[start:start + limit]]
            def download_activity(self, activity_id, format):
                type(self).downloads.append(activity_id)
                return gpx([(4.351 if activity_id == 102 else 4.35, 50.85)])
        class FakeOSM:
            def city(self, lon, lat):
                return relation(1, 4.34, 4.37)
            def roads(self, city_id):
                return roads([{"id": 1, "lon": 4.35, "lat": 50.85}, {"id": 2, "lon": 4.351, "lat": 50.85}])
        with patch("poc.garmin_sync.Garmin", FakeGarmin), patch("poc.garmin_sync.time.sleep"):
            sync(self.store, FakeOSM())
            self.assertEqual(FakeGarmin.pages, [0, 100])
            self.assertEqual(len(FakeGarmin.downloads), 101)
            self.assertTrue(self.store.get("history_complete"))
            self.assertFalse(self.store.streets()[0]["complete"])
            sync(self.store, FakeOSM())
            self.assertEqual(len(FakeGarmin.downloads), 101)
            FakeGarmin.ids.insert(0, 102)
            sync(self.store, FakeOSM())
            self.assertEqual(len(FakeGarmin.downloads), 102)
            self.assertTrue(self.store.streets()[0]["complete"])
            with self.store.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM activities WHERE processed=1").fetchone()[0], 102)


if __name__ == "__main__":
    unittest.main()
