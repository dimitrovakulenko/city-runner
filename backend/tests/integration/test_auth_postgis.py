import os
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app import auth
from backend.app.main import create_app


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "set POSTGIS_TEST_DATABASE_URL via the PostGIS test runner")
class AuthPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(DATABASE_URL)
        if not DATABASE_PATTERN.fullmatch(url.database or ""):
            raise RuntimeError("Integration tests require a UUID-named disposable city_runner_test database")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        command.upgrade(cls.config, "head")
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"GOOGLE_CLIENT_IDS": "com.example.integration"})
        self.env_patch.start()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities"))
            db.execute(text("DELETE FROM sessions"))
            db.execute(text("DELETE FROM login_challenges"))
            db.execute(text("DELETE FROM login_identities"))
            db.execute(text("DELETE FROM accounts"))
            db.execute(text("INSERT INTO accounts (id) VALUES ('alice'), ('bob')"))
        self.app = create_app(self.engine)
        self.client = TestClient(self.app)
        self.key_patch = patch("backend.app.auth.jwks_client", lambda provider: SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.public_key)
        ))
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        self.client.close()
        self.env_patch.stop()

    def challenge(self):
        response = self.client.post("/api/auth/challenges", json={"provider": "google"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def signed_token(self, nonce):
        now = datetime.now(timezone.utc)
        return jwt.encode({
            "iss": "https://accounts.google.com", "aud": "com.example.integration",
            "sub": "same-provider-subject", "nonce": nonce, "iat": now,
            "exp": now + timedelta(minutes=5),
        }, self.private_key, algorithm="RS256", headers={"kid": "synthetic-key"})

    def run_together(self, requests):
        barrier = threading.Barrier(len(requests))
        verify = auth.verify_id_token

        def synchronized_verify(provider, token, nonce):
            subject = verify(provider, token, nonce)
            barrier.wait(timeout=15)
            return subject

        def send(challenge, token):
            with TestClient(self.app) as client:
                return client.post("/api/auth/exchange", json={
                    "challenge_id": challenge["id"], "id_token": token,
                })

        with patch("backend.app.auth.verify_id_token", synchronized_verify):
            with ThreadPoolExecutor(max_workers=len(requests)) as pool:
                futures = [pool.submit(send, challenge, token) for challenge, token in requests]
                return [future.result(timeout=30) for future in futures]

    def test_same_challenge_has_one_success_and_one_session(self):
        challenge = self.challenge()
        token = self.signed_token(challenge["nonce"])
        responses = self.run_together([(challenge, token), (challenge, token)])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 401])
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM sessions")).scalar_one(), 1)

    def test_concurrent_first_login_for_subject_creates_one_account(self):
        first = self.challenge()
        second = self.challenge()
        responses = self.run_together([
            (first, self.signed_token(first["nonce"])),
            (second, self.signed_token(second["nonce"])),
        ])
        self.assertEqual([response.status_code for response in responses], [200, 200])
        accounts = {response.json()["account"]["id"] for response in responses}
        self.assertEqual(len(accounts), 1)
        with self.engine.connect() as db:
            account_rows = db.execute(text("SELECT id FROM accounts WHERE id NOT IN ('alice','bob')")).all()
            self.assertEqual(len(account_rows), 1)
            self.assertEqual(account_rows[0][0], next(iter(accounts)))
            self.assertEqual(db.execute(text("""SELECT count(*) FROM login_identities
                WHERE provider='google' AND subject='same-provider-subject'""")).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT count(*) FROM sessions")).scalar_one(), 2)


if __name__ == "__main__":
    unittest.main()
