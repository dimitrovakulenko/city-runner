"""Single-use handoff, linked app revocation and private preferences."""

import os
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import text

from backend.app.analytics_config import AnalyticsConfig
from backend.app.analytics_projection import refresh_projection, register_account
from backend.app.analytics_store import owner_key
from backend.app.main import create_app
from backend.tests.integration import test_analytics_projection as projection_fixture


@unittest.skipUnless(os.getenv("POSTGIS_TEST_DATABASE_URL"), "run with scripts/dev/postgis-test.sh")
class AnalyticsAuthPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        projection_fixture.AnalyticsProjectionPostgisTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        projection_fixture.AnalyticsProjectionPostgisTests.tearDownClass()

    def setUp(self):
        self.projection = projection_fixture.AnalyticsProjectionPostgisTests(methodName="runTest")
        self.projection.setUp()
        self.projection.publish()
        f = self.fixture = self.projection.fixture
        self.secret = projection_fixture.SECRET
        config = AnalyticsConfig(self.projection.target, self.secret, "http://127.0.0.1:8091",
                                 "http://127.0.0.1:5173", "http://synthetic-superset.invalid")
        self.client = TestClient(create_app(f.engine, analytics_config=config))
        self.metadata = patch("backend.app.analytics_api.private_call", side_effect=lambda _, owner, action, data=None: {
            "boards": [{"id": 1 if owner == owner_key(f.account) else 2, "title": "Synthetic private board"}],
        })
        self.metadata.start()
        self.internal = {"X-Analytics-Secret": self.secret.decode()}
        with self.projection.target.begin() as db:
            db.exec_driver_sql("UPDATE control.gateway SET instance=NULL,leased_until=NULL")

    def tearDown(self):
        self.metadata.stop()
        self.client.close()
        self.projection.tearDown()

    def ticket(self, **data):
        response = self.client.post("/api/analytics/handoff", json=data, headers=self.fixture.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(self.fixture.tokens[self.fixture.account], response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["ticket"]

    def consume(self, ticket, previous=None):
        return self.client.post("/internal/analytics/consume", headers=self.internal,
                                json={"token": ticket, "previous_token": previous})

    def validate(self, token):
        return self.client.post("/internal/analytics/validate", headers=self.internal, json={"token": token})

    def test_ticket_single_use_and_ordinary_application_logout_revokes_analytics(self):
        ticket = self.ticket()
        consumed = self.consume(ticket)
        self.assertEqual(consumed.status_code, 200, consumed.text)
        token = consumed.json()["token"]
        self.assertEqual(self.consume(ticket).status_code, 401)
        self.assertEqual(self.validate(token).status_code, 200)
        self.assertEqual(self.client.delete("/api/auth/session", headers=self.fixture.headers()).status_code, 204)
        self.assertEqual(self.validate(token).status_code, 401)

    def test_expired_ticket_and_underlying_session_fail_closed(self):
        ticket = self.ticket()
        with self.fixture.engine.begin() as db:
            db.exec_driver_sql("UPDATE analytics_handoff_tickets SET expires_at=clock_timestamp()-interval '1 second'")
        self.assertEqual(self.consume(ticket).status_code, 401)
        token = self.consume(self.ticket()).json()["token"]
        with self.fixture.engine.begin() as db:
            db.execute(text("UPDATE sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE account_id=:account"), {"account": self.fixture.account})
        self.assertEqual(self.validate(token).status_code, 401)

    def test_account_replacement_revokes_old_analytics_and_application_session(self):
        f = self.fixture
        old = self.consume(self.ticket()).json()["token"]
        with f.engine.begin() as db:
            register_account(db, f.other)
        refresh_projection(f.engine, self.projection.target, f.other, self.secret)
        ticket = self.client.post("/api/analytics/handoff", json={}, headers=f.headers(f.other)).json()["ticket"]
        replaced = self.consume(ticket, old)
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(self.validate(old).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=f.headers()).status_code, 401)
        self.assertEqual(self.validate(replaced.json()["token"]).json()["owner"], owner_key(f.other))

    def test_private_default_and_dataset_preferences_invalidate_old_projection(self):
        f = self.fixture
        self.assertEqual(self.client.put("/api/analytics/preferences", json={"default_dashboard_id": "2"}, headers=f.headers()).status_code, 404)
        self.assertEqual(self.client.put("/api/analytics/preferences", json={"default_dashboard_id": "1"}, headers=f.headers()).status_code, 204)
        self.assertEqual(self.consume(self.ticket()).json()["destination"], "/superset/dashboard/1/")
        token = self.consume(self.ticket(destination="sql")).json()["token"]
        self.assertEqual(self.client.put("/api/analytics/preferences", json={"dataset_id": None}, headers=f.headers()).status_code, 204)
        self.assertEqual(self.validate(token).status_code, 409)
        self.assertEqual(self.client.post("/api/analytics/handoff", json={}, headers=f.headers()).status_code, 409)

    def test_internal_secret_and_handoff_ownership_are_required(self):
        self.assertEqual(self.client.post("/internal/analytics/consume", json={"token": "forged"}).status_code, 401)
        self.assertEqual(self.consume("forged").status_code, 401)
        self.assertEqual(self.client.post("/api/analytics/handoff", json={"destination": "dashboard", "dashboard_id": "2"}, headers=self.fixture.headers()).status_code, 404)
        self.assertEqual(self.client.post("/api/analytics/handoff", json={"owner": "foreign"}, headers=self.fixture.headers()).status_code, 422)
        for _ in range(4):
            self.ticket()
        self.assertEqual(self.client.post("/api/analytics/handoff", json={}, headers=self.fixture.headers()).status_code, 429)

    def test_metadata_failure_does_not_consume_ticket_or_revoke_previous_login(self):
        old = self.consume(self.ticket()).json()["token"]
        ticket = self.ticket()
        with patch("backend.app.analytics_api.private_call", side_effect=HTTPException(503, "unavailable")):
            self.assertEqual(self.consume(ticket, old).status_code, 503)
        self.assertEqual(self.validate(old).status_code, 200)
        self.assertEqual(self.client.get("/api/me", headers=self.fixture.headers()).status_code, 200)
        self.assertEqual(self.consume(ticket, old).status_code, 200)

    def test_singleton_admission_and_interrupted_request_survive_gateway_restart(self):
        instance, other = str(uuid.uuid4()), str(uuid.uuid4())
        claim = lambda key: self.client.post("/internal/analytics/claim", json={"instance": key}, headers=self.internal)
        self.assertEqual(claim(instance).status_code, 204)
        self.assertEqual(claim(other).status_code, 409)
        token = self.consume(self.ticket()).json()["token"]
        profile = self.validate(token).json()
        admission = {"token": token, "version": profile["version"], "request_id": str(uuid.uuid4()), "gateway_id": other}
        self.assertEqual(self.client.post("/internal/analytics/admit", json=admission, headers=self.internal).status_code, 409)
        admission["gateway_id"] = instance
        self.assertEqual(self.client.post("/internal/analytics/admit", json=admission, headers=self.internal).status_code, 204)
        # A new process cannot clear admission even if SQL already ended.
        with self.projection.target.begin() as db:
            db.exec_driver_sql("UPDATE control.gateway SET leased_until=clock_timestamp()-interval '1 second'")
        self.assertEqual(claim(other).status_code, 204)
        self.assertEqual(self.client.post("/internal/analytics/reconcile", json={"instance": other}, headers=self.internal).status_code, 204)
        self.assertEqual(self.validate(token).status_code, 409)
        with self.projection.target.connect() as db:
            self.assertTrue(db.execute(text("SELECT fenced FROM control.owners WHERE owner_key=:owner"), {"owner": owner_key(self.fixture.account)}).scalar_one())

    def test_analytics_logout_revokes_underlying_application_session(self):
        token = self.consume(self.ticket()).json()["token"]
        response = self.client.post("/internal/analytics/logout", json={"token": token}, headers=self.internal)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.validate(token).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=self.fixture.headers()).status_code, 401)

    def test_private_board_titles_are_withheld_after_logout_during_metadata_rpc(self):
        def revoke(*args, **kwargs):
            self.assertEqual(self.client.delete("/api/auth/session", headers=self.fixture.headers()).status_code, 204)
            return {"boards": [{"id": 1, "title": "Must not escape"}]}
        with patch("backend.app.analytics_api.private_call", side_effect=revoke):
            response = self.client.get("/api/analytics", headers=self.fixture.headers())
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("Must not escape", response.text)


if __name__ == "__main__":
    unittest.main()
