import tempfile
import unittest

from fastapi.testclient import TestClient

from poc.app import create_app
from poc.core import Store


class ActivityApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(self.folder.name)
        with self.store.db() as db:
            db.executemany("INSERT INTO activities(id,name,date,tracks,activity_type,timestamps) VALUES (?,?,?,?,?,?)", [
                (1, "Morning Run", "2026-10-04", '[[[4.0,50.0],[5.0,51.0]],[[6.0,52.0]]]', "running", '[["a","b"],["c"]]'),
                (-9007199254740993, "Morning Walk", "2026-10-04", '[[[-1.0,-2.0]]]', "unknown", '[[null]]'),
                (3, "Evening Ride", "unknown", "[]", "unknown", "[]"),
            ])
        self.client = TestClient(create_app(self.store, "secret", poll=False))

    def tearDown(self):
        self.client.close()
        self.folder.cleanup()

    def test_authentication(self):
        self.assertEqual(self.client.get("/api/activities").status_code, 401)
        self.assertEqual(self.client.get("/api/activities", auth=("poc", "secret")).status_code, 200)

    def test_pagination_search_unknown_metadata_and_deterministic_order(self):
        response = self.client.get("/api/activities?limit=1&offset=1", auth=("poc", "secret"))
        self.assertEqual(response.json(), {"items": [{"id": "-9007199254740993", "name": "Morning Walk", "date": "2026-10-04", "type": "unknown"}], "limit": 1, "offset": 1, "total": 3})
        result = self.client.get("/api/activities?name=morning", auth=("poc", "secret")).json()
        self.assertEqual([item["name"] for item in result["items"]], ["Morning Walk", "Morning Run"])
        unknown = self.client.get("/api/activities?offset=2", auth=("poc", "secret")).json()["items"][0]
        self.assertEqual((unknown["date"], unknown["type"]), ("unknown", "unknown"))

    def test_detail_preserves_segments_timestamps_bounds_and_string_id(self):
        result = self.client.get("/api/activities/-9007199254740993", auth=("poc", "secret"))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"id": "-9007199254740993", "name": "Morning Walk", "date": "2026-10-04", "type": "unknown",
                                         "tracks": [[[-1.0, -2.0]]], "timestamps": [[None]], "bounds": [[-1.0, -2.0], [-1.0, -2.0]]})
        self.assertEqual(self.client.get("/api/activities/999", auth=("poc", "secret")).status_code, 404)

