"""Real gateway entry, identity forwarding and result-generation regressions."""

import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.analytics.gateway import COOKIE, create_app


class AuthoringGatewayTests(unittest.TestCase):
    def gateway(self, *, changed=False, startup_failure=False):
        self.version = "generation-one"
        self.events = []
        self.upstream_headers = None
        self.used = False

        async def respond(request):
            if request.url.host == "superset.internal":
                self.upstream_headers = dict(request.headers)
                if changed:
                    self.version = "generation-two"
                return httpx.Response(200, json={"private": "must-not-escape-on-revision-change"})
            action = request.url.path.split("/")[-1]
            self.events.append(action)
            if action == "reconcile" and startup_failure:
                return httpx.Response(503)
            if action == "consume":
                if self.used:
                    return httpx.Response(401)
                self.used = True
                return httpx.Response(200, json={"token": "synthetic-linked-session", "destination": "/dashboard/list/"})
            if action == "validate":
                return httpx.Response(200, json={"owner": "a" * 64, "version": self.version})
            return httpx.Response(204)

        transport = httpx.MockTransport(respond)
        app = create_app(backend_url="http://backend.internal", secret="synthetic-server-key-at-least-32-bytes",
            public_origin="http://127.0.0.1:8091", web_origin="http://127.0.0.1:4183",
            superset_url="http://superset.internal", transport=transport)
        original = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs.setdefault("transport", transport)
            return original(*args, **kwargs)

        return app, patch("services.analytics.proxy.httpx.AsyncClient", side_effect=client)

    def test_handoff_requires_web_origin_and_strips_upstream_identity_secrets(self):
        app, mocked = self.gateway()
        with mocked, TestClient(app) as client:
            self.assertEqual(client.post("/session", data={"ticket": "one-use"}, headers={"Origin": "https://foreign.invalid"}).status_code, 403)
            issued = client.post("/session", data={"ticket": "one-use"}, headers={"Origin": "http://127.0.0.1:4183"}, follow_redirects=False)
            self.assertEqual(issued.status_code, 303)
            self.assertIn("HttpOnly", issued.headers["set-cookie"])
            self.assertIn("SameSite=strict", issued.headers["set-cookie"])
            self.assertIn(COOKIE, client.cookies)
            response = client.post("/api/v1/sqllab/execute/", json={}, headers={
                "Origin": "http://127.0.0.1:8091", "Authorization": "Bearer forged",
                "X-City-Runner-Account": "foreign", "X-Analytics-Secret": "forged",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.upstream_headers["x-city-runner-account"], "a" * 64)
            self.assertNotIn("authorization", self.upstream_headers)
            self.assertNotIn("x-analytics-secret", self.upstream_headers)
            self.assertNotIn(COOKIE, self.upstream_headers.get("cookie", ""))
            self.assertIn("admit", self.events)
            self.assertIn("finish", self.events)
            self.assertEqual(client.post("/integration/anything/provision").status_code, 404)
            self.assertEqual(client.post("/session", data={"ticket": "one-use"}, headers={"Origin": "http://127.0.0.1:4183"}).status_code, 401)
        self.assertEqual(self.events[-1], "release")

    def test_completed_response_is_withheld_after_generation_change(self):
        app, mocked = self.gateway(changed=True)
        with mocked, TestClient(app) as client:
            client.cookies.set(COOKIE, "synthetic-linked-session")
            response = client.post("/api/v1/sqllab/execute/", json={})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("must-not-escape", response.text)
            self.assertIn("finish", self.events)

    def test_startup_reconciliation_failure_cannot_serve(self):
        app, mocked = self.gateway(startup_failure=True)
        with mocked, self.assertRaises(HTTPException), TestClient(app):
            self.fail("Unreconciled gateway started.")


if __name__ == "__main__":
    unittest.main()
