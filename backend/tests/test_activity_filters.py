import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend.app.main import create_app


class ActivityFilterUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.tmp.name}/activities.db")
        with self.engine.begin() as db:
            db.execute(text("""CREATE TABLE activities (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                date TEXT, activity_type TEXT, processed BOOLEAN NOT NULL DEFAULT 0,
                unmapped_points INTEGER NOT NULL DEFAULT 0, tracks JSON, timestamps JSON)"""))
            db.execute(text("""INSERT INTO activities VALUES
                (1,'alice','First','2026-01-01',' Running ',1,2,'[]','[]'),
                (2,'alice','Second','2026-01-02','WALKING',0,3,'[]','[]'),
                (3,'alice','Unknown date','unknown',NULL,0,4,'[]','[]'),
                (4,'alice','Invalid date','2026-02-30','unknown',0,5,'[]','[]'),
                (6,'alice','Year zero','0000-01-01','unknown',0,0,'[]','[]'),
                (7,'alice','Malformed date','not-a-date','unknown',0,0,'[]','[]'),
                (5,'bob','Private','2026-01-01','cycling',0,6,'[]','[]')"""))
            db.execute(text("""CREATE TABLE activity_sources (
                id INTEGER PRIMARY KEY, account_id TEXT, activity_id INTEGER, source_kind TEXT)"""))
            db.execute(text("""INSERT INTO activity_sources VALUES
                (1,'alice',1,'gpx'),(2,'alice',1,'fit'),(3,'alice',2,'fit'),(4,'bob',5,'gpx')"""))
        self.identity = "alice"
        self.client = TestClient(create_app(self.engine, lambda: self.identity))

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def test_source_type_dates_and_pagination_are_owner_scoped(self):
        self.assertEqual(self.client.get("/api/activities", params={"source": "gpx"}).json()["total"], 1)
        fit = self.client.get("/api/activities", params={"source": "fit", "page_size": 1}).json()
        self.assertEqual((fit["total"], len(fit["items"])), (2, 1))
        self.assertEqual(self.client.get("/api/activities", params={"source": "unknown"}).json()["total"], 4)
        typed = self.client.get("/api/activities", params={"activity_type": " RUNNING "}).json()
        self.assertEqual([item["id"] for item in typed["items"]], ["1"])
        unknown_type = self.client.get("/api/activities", params={"activity_type": "unknown"}).json()
        self.assertEqual({item["id"] for item in unknown_type["items"]}, {"3", "4", "6", "7"})
        bounded = self.client.get("/api/activities", params={
            "date_from": "2026-01-01", "date_to": "2026-01-01", "source": "all",
        }).json()
        self.assertEqual([item["id"] for item in bounded["items"]], ["1"])
        self.assertEqual(self.client.get("/api/activities/filters").json(), {
            "activity_types": ["running", "walking"], "types_truncated": False,
        })
        self.identity = "bob"
        self.assertEqual(self.client.get("/api/activities/filters").json()["activity_types"], ["cycling"])

    def test_bad_dates_type_and_source_are_safe_422(self):
        for params in ({"date_from": "2026-02-30"}, {"date_to": "2026/01/01"},
                       {"date_from": "2026-02-02", "date_to": "2026-02-01"},
                       {"activity_type": "x" * 81}, {"source": "future"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/api/activities", params=params).status_code, 422)

    def test_filter_options_are_bounded_and_sorted(self):
        with self.engine.begin() as db:
            for index in range(105):
                db.execute(text("""INSERT INTO activities
                    (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                    VALUES (:id,'alice','Option','2026-01-01',:type,0,0,'[]','[]')"""), {
                    "id": 100 + index, "type": f"kind-{index:03d}",
                })
        options = self.client.get("/api/activities/filters").json()
        self.assertEqual((len(options["activity_types"]), options["types_truncated"]), (100, True))
        self.assertEqual(options["activity_types"], sorted(options["activity_types"]))


if __name__ == "__main__":
    unittest.main()
