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
                date TEXT, activity_type TEXT, tracks JSON, timestamps JSON)"""))
            db.execute(text("""INSERT INTO activities VALUES
                (1,'alice','Run','2026-10-04','running','[[[4,50],[5,51]]]','[["a","b"]]'),
                (2,'alice','Walk','2026-10-03','walking','[]','[]'),
                (3,'bob','Private','2026-10-05','running','[]','[]')"""))
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

    def test_account_isolation_and_pagination_keep_contract(self):
        response = self.client.get("/api/activities?limit=1&offset=1")
        self.assertEqual(response.json(), {"items": [{"id": "2", "name": "Walk", "date": "2026-10-03", "type": "walking"}],
                                           "limit": 1, "offset": 1, "total": 2})
        self.assertEqual(self.client.get("/api/activities/3").status_code, 404)
        self.identity[0] = "bob"
        self.assertEqual([a["name"] for a in self.client.get("/api/activities").json()["items"]], ["Private"])

    def test_persisted_data_survives_app_restart(self):
        self.client.close()
        self.engine.dispose()
        new_engine = create_engine(self.url)
        restarted = TestClient(create_app(new_engine, lambda: "alice"))
        self.assertEqual(restarted.get("/api/activities/1").json()["tracks"], [[[4, 50], [5, 51]]])
        restarted.close()
        new_engine.dispose()


if __name__ == "__main__":
    unittest.main()
