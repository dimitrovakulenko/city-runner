import hashlib
import os
import re
import stat
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.jobs import claim
from backend.app.main import create_app
from backend.app.storage import LocalObjectStore
from backend.app.uploads import process_upload
from backend.app.worker import run_once
from backend.tests.test_fit import fit_fixture, semicircles


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")
SAMPLE_GPX = b'''<?xml version="1.0"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="integration">
  <metadata><name>Segmented run</name></metadata>
  <trk><type>Run</type><trkseg>
    <trkpt lat="50.1" lon="4.2"><time>2026-10-04T08:00:00Z</time></trkpt>
    <trkpt lat="50.2" lon="4.3"><time>2026-10-04T08:01:00</time></trkpt>
  </trkseg><trkseg>
    <trkpt lat="51" lon="5"><time>2026-10-04T08:05:00+00:00</time></trkpt>
  </trkseg></trk>
</gpx>'''


@unittest.skipUnless(DATABASE_URL, "run with the disposable PostGIS integration harness")
class UploadIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("upload integration tests require the generated disposable test database")
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(cls.config, "head")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.account_id = str(uuid.uuid4())
        self.other_account_id = str(uuid.uuid4())
        self.alice_token = self._add_account(self.account_id)
        self.bob_token = self._add_account(self.other_account_id)
        self.temp = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp.name) / "private-uploads"
        self.storage_env = patch.dict(os.environ, {"UPLOAD_STORAGE_DIR": str(self.storage_root)})
        self.storage_env.start()
        self.store = LocalObjectStore(self.storage_root)
        self.client = TestClient(create_app(self.engine))
        self.alice_headers = {"Authorization": f"Bearer {self.alice_token}"}
        self.bob_headers = {"Authorization": f"Bearer {self.bob_token}"}

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id IN (:alice,:bob)"), {
                "alice": self.account_id, "bob": self.other_account_id,
            })
            db.execute(text("DELETE FROM accounts WHERE id IN (:alice,:bob)"), {
                "alice": self.account_id, "bob": self.other_account_id,
            })
        self.storage_env.stop()
        self.temp.cleanup()

    def _add_account(self, account_id):
        token = f"integration-token-{uuid.uuid4().hex}"
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts (id) VALUES (:id)"), {"id": account_id})
            db.execute(text("""INSERT INTO sessions (token_digest, account_id, expires_at)
                VALUES (:digest, :account_id, :expires_at)"""), {
                "digest": digest, "account_id": account_id,
                "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
            })
        return token

    def upload(self, content=SAMPLE_GPX, *, client=None, headers=None, filename="run.gpx"):
        return (client or self.client).post("/api/uploads", headers=headers or self.alice_headers,
            files={"file": (filename, content, "application/gpx+xml")})

    def process_next(self):
        return run_once(self.engine, {"process_upload": lambda job: process_upload(self.engine, job, self.store)})

    def test_upload_worker_duplicate_and_account_isolation(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()
        self.assertIsInstance(result["id"], str)
        self.assertIsInstance(result["job_id"], str)
        self.assertFalse(result["duplicate"])
        duplicate = self.upload()
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(duplicate.json()["id"], result["id"])
        self.assertEqual(duplicate.json()["job_id"], result["job_id"])
        other = self.upload(client=self.client, headers=self.bob_headers)
        self.assertEqual(other.status_code, 202)
        self.assertFalse(other.json()["duplicate"])
        self.assertNotEqual(other.json()["id"], result["id"])

        stored_path = self.storage_root / self._source_object_key(result["id"])
        self.assertEqual(stat.S_IMODE(self.storage_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(stored_path.stat().st_mode), 0o600)
        self.assertNotIn(str(self.storage_root), response.text)

        self.assertTrue(self.process_next())
        self.assertTrue(self.process_next())
        self.assertFalse(self.process_next())
        completed = self.client.get(f"/api/uploads/{result['id']}", headers=self.alice_headers)
        self.assertEqual(completed.status_code, 200)
        state = completed.json()
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["job_id"], result["job_id"])
        self.assertEqual(state["error"], None)
        self.assertEqual(self.client.get(f"/api/uploads/{other.json()['id']}", headers=self.alice_headers).status_code,
                         404)
        other_state = self.client.get(f"/api/uploads/{other.json()['id']}", headers=self.bob_headers).json()
        self.assertEqual(other_state["status"], "succeeded")

        detail = self.client.get(f"/api/activities/{state['activity_id']}", headers=self.alice_headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["name"], "Segmented run")
        self.assertEqual(detail.json()["type"], "run")
        self.assertEqual(detail.json()["date"], "2026-10-04")
        self.assertEqual(detail.json()["tracks"], [[[4.2, 50.1], [4.3, 50.2]], [[5.0, 51.0]]])
        self.assertEqual(detail.json()["timestamps"], [[
            "2026-10-04T08:00:00Z", None,
        ], ["2026-10-04T08:05:00+00:00"]])
        self.assertFalse(detail.json()["processed"])
        listed = self.client.get("/api/activities", headers=self.alice_headers).json()
        self.assertEqual([item["name"] for item in listed["items"]], ["Segmented run"])
        other_detail = self.client.get(f"/api/activities/{other_state['activity_id']}", headers=self.bob_headers)
        self.assertEqual(other_detail.status_code, 200)
        self.assertEqual(self.client.get(f"/api/activities/{state['activity_id']}", headers=self.bob_headers).status_code,
                         404)
        self.assertEqual(self.client.get(f"/api/activities/{other_state['activity_id']}", headers=self.alice_headers).status_code,
                         404)

    def _source_object_key(self, source_id):
        with self.engine.connect() as db:
            return db.execute(text("SELECT private_object_key FROM activity_sources WHERE id=:id"), {
                "id": int(source_id),
            }).scalar_one()

    def test_invalid_gpx_and_point_limit_are_visible_failed_states(self):
        invalid = self.upload(b"<gpx><trk>")
        self.assertEqual(invalid.status_code, 202)
        self.process_next()
        failed = self.client.get(f"/api/uploads/{invalid.json()['id']}", headers=self.alice_headers).json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "invalid_gpx_xml")

        points = b"".join(b"<trkpt lat='0' lon='0'/>" for _ in range(100_001))
        too_many = b"<gpx><trk><trkseg>" + points + b"</trkseg></trk></gpx>"
        response = self.upload(too_many, filename="large-points.gpx")
        self.assertEqual(response.status_code, 202)
        self.process_next()
        failed = self.client.get(f"/api/uploads/{response.json()['id']}", headers=self.alice_headers).json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "gpx_point_limit")

    def test_fit_upload_keeps_format_identity_and_worker_imports_samples(self):
        timestamp = 1_167_609_600
        content = fit_fixture(records=((timestamp, semicircles(50.1), semicircles(4.2)),))
        response = self.upload(content, filename="morning.FIT")
        self.assertEqual(response.status_code, 202, response.text)
        source_id = response.json()["id"]
        object_key = self._source_object_key(source_id)
        self.assertTrue(object_key.endswith(".fit"))
        self.assertEqual(self.store.read(object_key), content)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT source_kind FROM activity_sources WHERE id=:id"), {
                "id": int(source_id),
            }).scalar_one(), "fit")
        self.assertTrue(self.process_next())
        state = self.client.get(f"/api/uploads/{source_id}", headers=self.alice_headers).json()
        self.assertEqual(state["status"], "succeeded")
        detail = self.client.get(f"/api/activities/{state['activity_id']}",
                                 headers=self.alice_headers).json()
        self.assertEqual(detail["type"], "running")
        self.assertAlmostEqual(detail["tracks"][0][0][0], 4.2, places=5)
        self.assertAlmostEqual(detail["tracks"][0][0][1], 50.1, places=5)
        self.assertEqual(detail["timestamps"][0][0], "2026-12-31T00:00:00Z")

        duplicate = self.upload(content, filename="again.fit")
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(duplicate.json()["id"], source_id)

        no_gps = self.upload(fit_fixture(records=()), filename="indoor.fit")
        self.assertEqual(no_gps.status_code, 202)
        self.assertTrue(self.process_next())
        failed = self.client.get(f"/api/uploads/{no_gps.json()['id']}",
                                 headers=self.alice_headers).json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "fit_no_track_points")
        self.assertEqual(self.client.get("/api/activities", headers=self.alice_headers)
                         .json()["total"], 1)

    def test_byte_limit_precedes_complete_multipart_parse(self):
        response = self.upload(b"x" * (10 * 1024 * 1024 + 1), filename="oversize.gpx")
        self.assertEqual(response.status_code, 413)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"), {
                "id": self.account_id,
            }).scalar_one(), 0)
        self.assertEqual(list(self.storage_root.iterdir()), [])

    def test_malformed_multipart_is_safe_400_without_creating_rows(self):
        response = self.client.post("/api/uploads", headers={
            **self.alice_headers,
            "Content-Type": "multipart/form-data; boundary=malformed-boundary",
        }, content=b"not a multipart body")
        self.assertEqual(response.status_code, 400, response.text)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"), {
                "id": self.account_id,
            }).scalar_one(), 0)
        self.assertEqual(list(self.storage_root.iterdir()), [])

    def test_chunked_body_limit_rejects_without_creating_database_or_storage_rows(self):
        boundary = "chunked-gpx-boundary"
        header = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"large.gpx\"\r\n"
                  "Content-Type: application/gpx+xml\r\n\r\n").encode()

        def body_chunks():
            yield header
            for _ in range(21):
                yield b"x" * (512 * 1024)
            yield f"\r\n--{boundary}--\r\n".encode()

        response = self.client.post("/api/uploads", headers={
            **self.alice_headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }, content=body_chunks())
        self.assertEqual(response.status_code, 413, response.text)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"), {
                "id": self.account_id,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT count(*) FROM jobs WHERE account_id=:id"), {
                "id": self.account_id,
            }).scalar_one(), 0)
        self.assertEqual(list(self.storage_root.iterdir()), [])

    def test_transaction_rollback_removes_new_unreferenced_blob(self):
        client = TestClient(create_app(self.engine), raise_server_exceptions=False)
        try:
            with patch("backend.app.uploads.enqueue", side_effect=RuntimeError("private diagnostic")):
                response = self.upload(client=client)
        finally:
            client.close()
        self.assertEqual(response.status_code, 500)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"), {
                "id": self.account_id,
            }).scalar_one(), 0)
        self.assertEqual(list(self.storage_root.iterdir()), [])
        self.assertNotIn("private diagnostic", response.text)

    def test_expired_lease_restart_and_stale_revision_are_fenced(self):
        upload = self.upload().json()
        first = claim(self.engine, kind="process_upload")
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET leased_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {
                "id": int(upload["job_id"]),
            })
        second = claim(self.engine, kind="process_upload")
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        process_upload(self.engine, first, self.store)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities WHERE user_id=:id"), {
                "id": self.account_id,
            }).scalar_one(), 0)
        process_upload(self.engine, second, self.store)
        self.assertEqual(self.client.get(f"/api/uploads/{upload['id']}", headers=self.alice_headers).json()["status"],
                         "succeeded")

        stale = self.upload(SAMPLE_GPX.replace(b"Segmented run", b"Stale revision")).json()
        claimed = claim(self.engine, kind="process_upload")
        with self.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET revision=revision+1 WHERE id=:id"), {
                "id": int(stale["id"]),
            })
        process_upload(self.engine, claimed, self.store)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE id=:id"), {
                "id": int(stale["job_id"]),
            }).scalar_one(), "failed")
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities WHERE name='Stale revision'")).scalar_one(), 0)

    def test_job_account_mismatch_cannot_read_or_write_another_accounts_source(self):
        upload = self.upload().json()
        job = claim(self.engine, kind="process_upload")
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET account_id=:other WHERE id=:id"), {
                "other": self.other_account_id, "id": int(upload["job_id"]),
            })
        with patch.object(self.store, "read", side_effect=AssertionError("cross-account source read")):
            process_upload(self.engine, job, self.store)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE id=:id"), {
                "id": int(upload["job_id"]),
            }).scalar_one(), "failed")
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities WHERE user_id=:id"), {
                "id": self.account_id,
            }).scalar_one(), 0)

    def test_transient_failure_retries_then_marks_source_failed_on_exhaustion(self):
        upload = self.upload().json()
        self.store.delete(self._source_object_key(upload["id"]))
        for attempt in range(1, 6):
            job = claim(self.engine, kind="process_upload")
            self.assertEqual(job["attempts"], attempt)
            process_upload(self.engine, job, self.store)
            with self.engine.connect() as db:
                row = db.execute(text("""SELECT jobs.status AS job_status, activity_sources.status AS source_status
                    FROM jobs JOIN activity_sources ON activity_sources.id=:source_id WHERE jobs.id=:job_id"""), {
                    "source_id": int(upload["id"]), "job_id": int(upload["job_id"]),
                }).mappings().one()
            expected = "failed" if attempt == 5 else "queued"
            self.assertEqual(row["job_status"], expected)
            self.assertEqual(row["source_status"], expected)
            if attempt < 5:
                with self.engine.begin() as db:
                    db.execute(text("UPDATE jobs SET available_at=clock_timestamp() WHERE id=:id"), {
                        "id": int(upload["job_id"]),
                    })
        state = self.client.get(f"/api/uploads/{upload['id']}", headers=self.alice_headers).json()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "source_unavailable")

    def test_openapi_declares_multipart_binary_file(self):
        schema = self.client.get("/openapi.json").json()
        request_body = schema["paths"]["/api/uploads"]["post"]["requestBody"]
        file_schema = request_body["content"]["multipart/form-data"]["schema"]["properties"]["file"]
        self.assertEqual(file_schema["format"], "binary")


if __name__ == "__main__":
    unittest.main()
