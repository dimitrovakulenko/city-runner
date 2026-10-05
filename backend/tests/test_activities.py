import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend.app.main import create_app


class ActivityApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{self.temp.name}/activities.db"
        self.engine = create_engine(self.url)
        with self.engine.begin() as db:
            db.execute(text("""CREATE TABLE activities (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                date TEXT, activity_type TEXT, processed BOOLEAN NOT NULL DEFAULT 0,
                unmapped_points INTEGER NOT NULL DEFAULT 0, tracks JSON, timestamps JSON)"""))
            db.execute(text("""INSERT INTO activities VALUES
                (1,'alice','Morning Run','2026-10-04','running',1,2,'[[[4,50]],[[5,51]]]','[["a"],["b"]]'),
                (2,'alice','Evening run','2026-10-03','running',0,0,'[]','[]'),
                (3,'alice','Walk','2026-10-04','walking',0,1,'[]','[]'),
                (4,'bob','Private Run','2026-10-05','running',1,0,'[]','[]')"""))
        self.identity = ["alice"]
        self.app = create_app(self.engine, lambda: self.identity[0])
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_authentication_fails_closed_by_default(self):
        client = TestClient(create_app(self.engine))
        self.assertEqual(client.get("/api/activities").status_code, 401)
        client.close()

    def test_explorer_contract_search_pagination_and_isolation(self):
        response = self.client.get("/api/activities?page=2&page_size=1&q=Run")
        self.assertEqual(response.json(), {
            "items": [{"id": "2", "name": "Evening run", "date": "2026-10-03",
                       "type": "running", "processed": False, "unmapped_points": 0}],
            "page": 2, "page_size": 1, "total": 2,
        })
        self.assertEqual(self.client.get("/api/activities/4").status_code, 404)
        detail = self.client.get("/api/activities/1").json()
        self.assertEqual(detail, {
            "id": "1", "name": "Morning Run", "date": "2026-10-04", "type": "running",
            "processed": True, "unmapped_points": 2,
            "tracks": [[[4, 50]], [[5, 51]]], "timestamps": [["a"], ["b"]],
            "bounds": [[4, 50], [5, 51]],
        })
        self.identity[0] = "bob"
        self.assertEqual([a["name"] for a in self.client.get("/api/activities").json()["items"]], ["Private Run"])
        self.assertEqual(self.client.get("/api/activities?q=run").json()["total"], 1)

    def test_persisted_data_survives_app_restart(self):
        self.client.close()
        self.engine.dispose()
        new_engine = create_engine(self.url)
        restarted = TestClient(create_app(new_engine, lambda: "alice"))
        self.assertEqual(restarted.get("/api/activities/1").json()["tracks"], [[[4, 50]], [[5, 51]]])
        restarted.close()
        new_engine.dispose()


if __name__ == "__main__":
    unittest.main()
