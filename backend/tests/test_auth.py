import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend.app.main import create_app


class AuthApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.apple_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()
        cls.other_public_key = cls.other_key.public_key()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temp.name}/auth.db")
        with self.engine.begin() as db:
            db.execute(text("""CREATE TABLE accounts (
                id TEXT PRIMARY KEY, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""))
            db.execute(text("""CREATE TABLE login_identities (
                provider TEXT, subject TEXT, account_id TEXT,
                PRIMARY KEY(provider, subject))"""))
            db.execute(text("""CREATE TABLE login_challenges (
                id TEXT PRIMARY KEY, provider TEXT, nonce TEXT, expires_at DATETIME, consumed_at DATETIME)"""))
            db.execute(text("""CREATE TABLE sessions (
                token_digest CHAR(64) PRIMARY KEY, account_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME, revoked_at DATETIME)"""))
            db.execute(text("""CREATE TABLE activities (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                date TEXT, activity_type TEXT, processed BOOLEAN NOT NULL DEFAULT 0,
                unmapped_points INTEGER NOT NULL DEFAULT 0, tracks JSON, timestamps JSON)"""))
        self.client = TestClient(create_app(self.engine))
        self.env = patch.dict(os.environ, {
            "GOOGLE_CLIENT_IDS": "com.example.app", "APPLE_CLIENT_IDS": "com.example.apple",
        })
        self.env.start()
        self.client_patcher = patch("backend.app.auth.jwt.PyJWKClient", self.FakeJwkClient)
        self.client_patcher.start()

    def tearDown(self):
        self.client_patcher.stop()
        self.env.stop()
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    class FakeJwkClient:
        def __init__(self, url, timeout):
            if url not in ("https://www.googleapis.com/oauth2/v3/certs", "https://appleid.apple.com/auth/keys"):
                raise AssertionError("Unexpected key URL")
            if timeout != 5:
                raise AssertionError("JWKS timeout must be bounded")
            self.apple = url == "https://appleid.apple.com/auth/keys"

        def get_signing_key_from_jwt(self, token):
            header = jwt.get_unverified_header(token)
            key = AuthApiTests.apple_key.public_key() if self.apple else (
                AuthApiTests.other_public_key if header.get("kid") == "wrong-key" else AuthApiTests.public_key
            )
            return SimpleNamespace(key=key)

    def challenge(self):
        response = self.client.post("/api/auth/challenges", json={"provider": "google"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["nonce"]), 64)
        return response.json()

    def token(self, challenge_nonce, **claims):
        now = datetime.now(timezone.utc)
        payload = {
            "iss": "https://accounts.google.com",
            "aud": "com.example.app",
            "sub": "google-user-1",
            "nonce": challenge_nonce,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        payload.update(claims)
        return jwt.encode(payload, self.private_key, algorithm="RS256", headers={"kid": "test-key"})

    def exchange(self, challenge, token=None):
        return self.client.post("/api/auth/exchange", json={
            "challenge_id": challenge["id"],
            "id_token": token if token is not None else self.token(challenge["nonce"]),
        })

    def test_valid_signed_identity_creates_opaque_hashed_session(self):
        challenge = self.challenge()
        result = self.exchange(challenge)
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertNotEqual(body["token"], "google-user-1")
        self.assertEqual(len(body["token"]) >= 40, True)
        self.assertEqual(self.client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"}).json(),
                         {"id": body["account"]["id"]})
        with self.engine.connect() as db:
            row = db.execute(text("SELECT token_digest FROM sessions")).one()
        self.assertNotEqual(row[0], body["token"])
        self.assertEqual(len(row[0]), 64)

    def test_apple_rs256_identity_is_verified(self):
        challenge = self.client.post("/api/auth/challenges", json={"provider": "apple"}).json()
        now = datetime.now(timezone.utc)
        token = jwt.encode({
            "iss": "https://appleid.apple.com", "aud": "com.example.apple", "sub": "apple-user-1",
            "nonce": challenge["nonce"], "iat": now, "exp": now + timedelta(minutes=5),
        }, self.apple_key, algorithm="RS256", headers={"kid": "test-apple-key"})
        response = self.exchange(challenge, token)
        self.assertEqual(response.status_code, 200, response.text)

    def test_invalid_signature_issuer_audience_expiry_and_nonce_are_rejected(self):
        cases = [
            ("signature", lambda nonce: jwt.encode({
                "iss": "https://accounts.google.com", "aud": "com.example.app", "sub": "s",
                "nonce": nonce, "iat": datetime.now(timezone.utc),
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            }, self.private_key, algorithm="RS256", headers={"kid": "wrong-key"})),
            ("issuer", lambda nonce: self.token(nonce, iss="https://evil.example")),
            ("audience", lambda nonce: self.token(nonce, aud="other-app")),
            ("expiry", lambda nonce: self.token(nonce, exp=datetime.now(timezone.utc) - timedelta(seconds=5))),
            ("nonce", lambda nonce: self.token(nonce, nonce=None)),
        ]
        for name, make_token in cases:
            with self.subTest(name=name):
                challenge = self.challenge()
                response = self.exchange(challenge, make_token(challenge["nonce"]))
                self.assertEqual(response.status_code, 401, response.text)

    def test_challenge_is_single_use_and_bad_token_does_not_consume_it(self):
        challenge = self.challenge()
        self.assertEqual(self.exchange(challenge, self.token("wrong-nonce")).status_code, 401)
        success = self.exchange(challenge)
        self.assertEqual(success.status_code, 200, success.text)
        self.assertEqual(self.exchange(challenge).status_code, 401)

    def test_openapi_declares_bearer_security_only_for_protected_routes(self):
        schema = self.client.get("/openapi.json").json()
        bearer = schema["components"]["securitySchemes"]["HTTPBearer"]
        self.assertEqual(bearer, {"type": "http", "scheme": "bearer"})
        paths = schema["paths"]
        self.assertEqual(paths["/api/me"]["get"]["security"], [{"HTTPBearer": []}])
        self.assertEqual(paths["/api/cities"]["get"]["security"], [{"HTTPBearer": []}])
        self.assertNotIn("security", paths["/api/auth/challenges"]["post"])
        self.assertNotIn("security", paths["/api/auth/exchange"]["post"])
        self.assertEqual(self.client.get("/api/me", headers={"Authorization": "Basic abc"}).status_code, 401)

    def test_expired_challenge_and_expired_session_are_rejected(self):
        challenge = self.challenge()
        with self.engine.begin() as db:
            db.execute(text("UPDATE login_challenges SET expires_at=:expires WHERE id=:id"), {
                "id": challenge["id"], "expires": datetime.now(timezone.utc) - timedelta(seconds=1),
            })
        self.assertEqual(self.exchange(challenge).status_code, 401)
        fresh = self.exchange(self.challenge()).json()
        with self.engine.begin() as db:
            db.execute(text("UPDATE sessions SET expires_at=:expires"), {
                "expires": datetime.now(timezone.utc) - timedelta(seconds=1),
            })
        self.assertEqual(self.client.get("/api/me", headers={
            "Authorization": f"Bearer {fresh['token']}"
        }).status_code, 401)

    def test_google_alternate_documented_issuer_is_accepted(self):
        challenge = self.challenge()
        result = self.exchange(challenge, self.token(challenge["nonce"], iss="accounts.google.com"))
        self.assertEqual(result.status_code, 200, result.text)

    def test_sessions_are_isolated_and_revocable(self):
        first_challenge = self.challenge()
        first = self.exchange(first_challenge).json()
        second_challenge = self.challenge()
        second = self.exchange(second_challenge, self.token(second_challenge["nonce"], sub="google-user-2")).json()
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO activities (id,user_id,name) VALUES (1,:owner,'private')"),
                       {"owner": first["account"]["id"]})
        first_headers = {"Authorization": f"Bearer {first['token']}"}
        second_headers = {"Authorization": f"Bearer {second['token']}"}
        self.assertEqual(self.client.get("/api/activities/1", headers=first_headers).status_code, 200)
        self.assertEqual(self.client.get("/api/activities/1", headers=second_headers).status_code, 404)
        self.assertEqual(self.client.delete("/api/auth/session", headers=first_headers).status_code, 204)
        self.assertEqual(self.client.get("/api/me", headers=first_headers).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=second_headers).status_code, 200)

    def test_provider_without_audience_configuration_fails_closed(self):
        with patch.dict(os.environ, {"GOOGLE_CLIENT_IDS": ""}):
            self.assertEqual(self.client.post("/api/auth/challenges", json={"provider": "google"}).status_code, 503)


if __name__ == "__main__":
    unittest.main()
