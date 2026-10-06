"""Synthetic route API, CAS, quota and export checks on disposable PostGIS."""

import hashlib
import json
import os
import re
import threading
import unittest
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.main import create_app
from backend.app.routes import FOSSGIS_ATTRIBUTION


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")


def osrm_response(waypoints):
    return {
        "code": "Ok",
        "waypoints": [{"location": point, "distance": 0.0} for point in waypoints],
        "routes": [{"distance": 150.25, "duration": 110.5,
                    "geometry": {"type": "LineString", "coordinates": waypoints}}],
    }


@unittest.skipUnless(DATABASE_URL, "run with disposable PostGIS harness")
class RouteIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not re.fullmatch(r"city_runner_test_[0-9a-f]{12}", make_url(DATABASE_URL).database or ""):
            raise RuntimeError("route tests require the generated disposable database")
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(config, "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.accounts = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.tokens = [uuid.uuid4().hex, uuid.uuid4().hex]
        with self.engine.begin() as db:
            for account, token in zip(self.accounts, self.tokens):
                db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": account})
                db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                    VALUES (:digest,:account,:expires)"""), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
                    "expires": datetime.now(timezone.utc) + timedelta(hours=1),
                })
            db.execute(text("UPDATE routing_provider_quota SET next_allowed_at='-infinity' WHERE provider='fossgis'"))
        self.env = patch.dict(os.environ, {"ROUTING_PROVIDER": ""})
        self.env.start()
        self.provider_calls = 0

        def provider(waypoints):
            self.provider_calls += 1
            return osrm_response(waypoints)

        self.provider = provider
        self.app = create_app(self.engine, routing_provider=self.provider)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer " + self.tokens[0]}
        self.other_headers = {"Authorization": "Bearer " + self.tokens[1]}
        self.points = [[4.0, 50.0], [4.01, 50.01]]

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM accounts WHERE id=ANY(:ids)"), {"ids": self.accounts})
            db.execute(text("UPDATE routing_provider_quota SET next_allowed_at='-infinity' WHERE provider='fossgis'"))
        self.env.stop()

    def create(self, *, name="Route & walk", points=None, headers=None):
        return self.client.post("/api/routes", headers=headers or self.headers,
                                json={"name": name, "waypoints": points or self.points})

    def test_route_crud_owner_cas_export_and_no_activity_side_effects(self):
        with self.engine.connect() as db:
            before = db.execute(text("""SELECT
                (SELECT count(*) FROM activities WHERE user_id=:account),
                (SELECT count(*) FROM activity_sources WHERE account_id=:account),
                (SELECT count(*) FROM jobs WHERE account_id=:account)"""), {"account": self.accounts[0]}).one()
        preview = self.client.post("/api/routes/preview", headers=self.headers,
            json={"waypoints": self.points, "client_revision": 5})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["client_revision"], 5)
        self.assertEqual(preview.json()["attribution"], FOSSGIS_ATTRIBUTION)

        created = self.create()
        self.assertEqual(created.status_code, 201, created.text)
        route = created.json()
        self.assertEqual((route["name"], route["revision"], route["waypoints"]),
                         ("Route & walk", 1, self.points))
        route_id = route["id"]
        self.assertEqual(self.client.get(f"/api/routes/{route_id}", headers=self.headers).json(), route)
        listing = self.client.get("/api/routes", headers=self.headers).json()
        self.assertEqual((listing["total"], listing["items"][0]["id"]), (1, route_id))
        self.assertEqual(self.client.get(f"/api/routes/{route_id}", headers=self.other_headers).status_code, 404)
        self.assertEqual(self.client.put(f"/api/routes/{route_id}", headers=self.other_headers,
            json={"name": "foreign", "waypoints": self.points, "expected_revision": 1}).status_code, 404)

        calls = self.provider_calls
        renamed = self.client.put(f"/api/routes/{route_id}", headers=self.headers,
            json={"name": "Renamed", "waypoints": self.points, "expected_revision": 1})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual((renamed.json()["revision"], self.provider_calls), (2, calls))
        stale = self.client.put(f"/api/routes/{route_id}", headers=self.headers,
            json={"name": "stale", "waypoints": self.points, "expected_revision": 1})
        self.assertEqual(stale.status_code, 409)

        changed = self.client.put(f"/api/routes/{route_id}", headers=self.headers,
            json={"name": "Moved", "waypoints": [[4.02, 50.01], self.points[1]], "expected_revision": 2})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((changed.json()["revision"], self.provider_calls), (3, calls + 1))
        stale_delete = self.client.delete(f"/api/routes/{route_id}", headers=self.headers,
                                          params={"expected_revision": 2})
        self.assertEqual(stale_delete.status_code, 409)

        export = self.client.get(f"/api/routes/{route_id}/gpx", headers=self.headers)
        self.assertEqual(export.status_code, 200, export.text)
        self.assertIn("application/gpx+xml", export.headers["content-type"])
        self.assertIn("<name>Moved</name>", export.text)
        self.assertIn("<rtept", export.text)
        self.assertNotIn("<trkpt", export.text)
        self.assertNotIn("<time>", export.text)
        self.assertEqual(export.headers["content-disposition"], f'attachment; filename="route-{route_id}.gpx"')
        gpx = ET.fromstring(export.content)
        namespace = {"g": "http://www.topografix.com/GPX/1/1"}
        exported_points = [[float(point.attrib["lon"]), float(point.attrib["lat"])]
                           for point in gpx.findall(".//g:rtept", namespace)]
        self.assertEqual(exported_points, changed.json()["geometry"]["coordinates"])
        self.assertEqual(self.client.delete(f"/api/routes/{route_id}", headers=self.other_headers,
            params={"expected_revision": 3}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/routes/{route_id}", headers=self.headers,
            params={"expected_revision": 3}).status_code, 204)
        self.assertEqual(self.client.get(f"/api/routes/{route_id}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/api/routes", headers=self.headers,
            params={"offset": 2_147_483_648}).status_code, 422)
        with self.engine.connect() as db:
            after = db.execute(text("""SELECT
                (SELECT count(*) FROM activities WHERE user_id=:account),
                (SELECT count(*) FROM activity_sources WHERE account_id=:account),
                (SELECT count(*) FROM jobs WHERE account_id=:account)"""), {"account": self.accounts[0]}).one()
        self.assertEqual(after, before)

    def test_racing_edits_only_one_revision_wins(self):
        route = self.create().json()
        arrived = 0
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def provider(waypoints):
            nonlocal arrived
            with lock:
                arrived += 1
            barrier.wait(timeout=5)
            return osrm_response(waypoints)

        app = create_app(self.engine, routing_provider=provider)
        payloads = [
            {"name": "left", "waypoints": [[4.02, 50.0], self.points[1]], "expected_revision": 1},
            {"name": "right", "waypoints": [[3.98, 50.0], self.points[1]], "expected_revision": 1},
        ]

        def update(payload):
            with TestClient(app) as client:
                return client.put(f"/api/routes/{route['id']}", headers=self.headers, json=payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(update, payloads))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(arrived, 2)

    def test_concurrent_delete_keeps_a_pending_edit_terminal(self):
        route = self.create().json()
        entered, release = threading.Event(), threading.Event()

        def blocking_provider(waypoints):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test provider gate timed out")
            return osrm_response(waypoints)

        app = create_app(self.engine, routing_provider=blocking_provider)

        def update():
            with TestClient(app) as client:
                return client.put(f"/api/routes/{route['id']}", headers=self.headers,
                    json={"name": "pending", "waypoints": [[4.02, 50.0], self.points[1]], "expected_revision": 1})

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(update)
            self.assertTrue(entered.wait(timeout=5))
            deleted = self.client.delete(f"/api/routes/{route['id']}", headers=self.headers,
                                         params={"expected_revision": 1})
            self.assertEqual(deleted.status_code, 204)
            release.set()
            self.assertEqual(pending.result(timeout=10).status_code, 404)

    def test_database_quota_is_shared_and_provider_disabled_by_default(self):
        with patch.dict(os.environ, {"ROUTING_PROVIDER": ""}):
            disabled = TestClient(create_app(self.engine))
            response = disabled.post("/api/routes/preview", headers=self.headers,
                json={"waypoints": self.points, "client_revision": 0})
            self.assertEqual(response.status_code, 503)
            disabled.close()
        with patch.dict(os.environ, {"ROUTING_PROVIDER": "fossgis"}), \
                patch("backend.app.routes._provider_request", side_effect=lambda pts: osrm_response(pts)):
            first = TestClient(create_app(self.engine))
            second = TestClient(create_app(self.engine))
            self.assertEqual(first.post("/api/routes/preview", headers=self.headers,
                json={"waypoints": self.points, "client_revision": 1}).status_code, 200)
            limited = second.post("/api/routes/preview", headers=self.headers,
                json={"waypoints": self.points, "client_revision": 2})
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers.get("retry-after"), "1")
            first.close()
            second.close()

    def test_invalid_unicode_and_nonfinite_body_errors_are_safe_422(self):
        invalid_name = self.client.post("/api/routes", headers={**self.headers, "Content-Type": "application/json"},
            content=b'{"name":"bad\\ud800","waypoints":[[4,50],[4.1,50.1]]}')
        self.assertEqual(invalid_name.status_code, 422)
        self.assertEqual(invalid_name.json(), {"detail": "Invalid route request."})
        nan_point = self.client.post("/api/routes/preview", headers={**self.headers, "Content-Type": "application/json"},
            content=b'{"waypoints":[[NaN,50],[4,51]],"client_revision":0}')
        self.assertEqual(nan_point.status_code, 422)
        self.assertEqual(nan_point.json(), {"detail": "Invalid route request."})


if __name__ == "__main__":
    unittest.main()
