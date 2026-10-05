import os
import unittest
from pathlib import Path
import re

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.main import create_app


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "set POSTGIS_TEST_DATABASE_URL via the PostGIS test runner")
class PostgisActivityTests(unittest.TestCase):
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
        try:
            command.upgrade(cls.config, "head")
            with cls.engine.connect() as db:
                if db.execute(text("SELECT PostGIS_Version() IS NOT NULL")).scalar_one() is not True:
                    raise RuntimeError("PostGIS is not available in the integration database")
        except BaseException:
            cls.engine.dispose()
            if cls.previous_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = cls.previous_database_url
            raise

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities"))
            db.execute(text("""INSERT INTO accounts (id) VALUES ('alice'), ('bob')
                ON CONFLICT (id) DO NOTHING"""))

    def tearDown(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities"))

    def test_00_migration_up_down_up_preserves_legacy_owner(self):
        command.downgrade(self.config, "base")
        command.upgrade(self.config, "0001_activities")
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO activities (user_id,name) VALUES ('legacy-owner','Before auth')"))
        command.upgrade(self.config, "head")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT id FROM accounts WHERE id='legacy-owner'")).scalar_one(),
                             "legacy-owner")
            self.assertEqual(db.execute(text("SELECT user_id FROM activities")).scalar_one(), "legacy-owner")
        command.downgrade(self.config, "base")
        command.upgrade(self.config, "head")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities")).scalar_one(), 0)

    def test_owner_search_segment_gaps_and_bigint_ids(self):
        big_id = 9_223_372_036_854_770_000
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps,location)
                VALUES (:id,'alice','Long Run','2026-10-04','running',true,1,
                  '[[[4.1,50.1],[4.2,50.2]],[[5.1,51.1]]]','[["2026-10-04T08:00:00Z","2026-10-04T08:01:00Z"],["2026-10-04T08:05:00Z"]]',
                  ST_SetSRID(ST_MakePoint(4.1,50.1),4326)),
                  (2,'alice','Recovery Walk','2026-10-03','walking',false,0,'[]','[]',NULL),
                  (3,'bob','Private Long Run','2026-10-05','running',true,0,'[]','[]',NULL)"""), {"id": big_id})
            generated_id = db.execute(text("""INSERT INTO activities
                (user_id,name,activity_type,tracks,timestamps)
                VALUES ('alice','Generated ID','running','[]','[]') RETURNING id""")).scalar_one()
        identity = ["alice"]
        client = TestClient(create_app(self.engine, lambda: identity[0]))
        try:
            result = client.get("/api/activities?q=long").json()
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["id"], str(big_id))
            self.assertEqual(client.get("/api/activities/3").status_code, 404)
            detail = client.get(f"/api/activities/{big_id}").json()
            self.assertEqual(detail["id"], str(big_id))
            self.assertEqual(len(detail["tracks"]), 2)
            self.assertEqual(detail["timestamps"][1], ["2026-10-04T08:05:00Z"])
            self.assertEqual(detail["bounds"], [[4.1, 50.1], [5.1, 51.1]])
            self.assertEqual(client.get(f"/api/activities/{generated_id}").json()["id"], str(generated_id))
            identity[0] = "bob"
            self.assertEqual(client.get("/api/activities?q=long").json()["total"], 1)
            self.assertEqual(client.get(f"/api/activities/{big_id}").status_code, 404)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
