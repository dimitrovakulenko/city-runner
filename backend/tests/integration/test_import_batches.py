"""Synthetic D18 API and transaction interleavings on disposable PostGIS."""
import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from backend.app.jobs import claim
from backend.app.main import create_app
from backend.app.storage import LocalObjectStore
from backend.app.uploads import process_upload
from backend.tests.integration.test_uploads import SAMPLE_GPX

DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "run with disposable PostGIS harness")
class ImportBatchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not re.fullmatch(r"city_runner_test_[0-9a-f]{12}", make_url(DATABASE_URL).database or ""):
            raise RuntimeError("D18 requires a disposable test database")
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(config, "head")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.accounts = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.tokens = [uuid.uuid4().hex, uuid.uuid4().hex]
        with self.engine.begin() as db:
            for account, token in zip(self.accounts, self.tokens):
                db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": account})
                db.execute(text("INSERT INTO sessions(token_digest,account_id,expires_at) VALUES (:digest,:id,:expires)"), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "id": account,
                    "expires": datetime.now(timezone.utc) + timedelta(hours=1),
                })
        self.headers = {"Authorization": "Bearer " + self.tokens[0]}
        self.other_headers = {"Authorization": "Bearer " + self.tokens[1]}
        self.temp = tempfile.TemporaryDirectory()
        self.storage_patch = patch.dict(os.environ, {"UPLOAD_STORAGE_DIR": self.temp.name})
        self.storage_patch.start()
        self.store = LocalObjectStore(self.temp.name)
        self.app = create_app(self.engine)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id=ANY(:ids)"), {"ids": self.accounts})
            db.execute(text("DELETE FROM accounts WHERE id=ANY(:ids)"), {"ids": self.accounts})
        self.storage_patch.stop()
        self.temp.cleanup()

    def batch(self, files=None, request_id=None, expected=201, headers=None):
        response = self.client.post("/api/import-batches", headers={**(headers or self.headers), "Content-Type": "application/json"}, content=json.dumps({
            "request_id": request_id or str(uuid.uuid4()),
            "files": files if files is not None else [{"name": "run.gpx", "format": "gpx"}],
        }).encode())
        self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.headers.get("content-type", "").startswith("application/json") else None

    def item_path(self, batch, index=0):
        return f"/api/import-batches/{batch['id']}/items/{batch['items'][index]['id']}/upload"

    def upload(self, batch, content=SAMPLE_GPX, index=0, client=None, expected=202):
        response = (client or self.client).post(self.item_path(batch, index), headers=self.headers,
            files={"file": (batch["items"][index]["name"], content, "application/octet-stream")})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.headers.get("content-type", "").startswith("application/json") else None

    def detail(self, batch, client=None):
        response = (client or self.client).get("/api/import-batches/" + batch["id"], headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def process(self):
        job = claim(self.engine, kind="process_upload")
        self.assertIsNotNone(job)
        process_upload(self.engine, job, self.store)
        return job

    def mutation(self, path, expected=204, headers=None):
        response = self.client.post(path, headers=headers or self.headers)
        self.assertEqual(response.status_code, expected, response.text)

    def test_manifest_bounds_replay_isolation_and_safe_names(self):
        request_id = str(uuid.uuid4())
        files = [{"name": "../folder/run.gpx", "format": "gpx"}]
        batch = self.batch(files, request_id)
        self.assertEqual(batch["items"][0]["name"], "run.gpx")
        self.assertEqual(self.batch(files, request_id)["id"], batch["id"])
        self.batch([{"name": "../other/run.gpx", "format": "gpx"}], request_id, expected=409)
        self.batch([], expected=422)
        self.batch([{"name": "x.gpx", "format": "gpx"}] * 21, expected=422)
        self.batch(request_id="x" * 36, expected=422)
        self.batch([{"name": "\ud800", "format": "gpx"}], expected=422)
        self.assertNotEqual(self.batch(files, request_id, headers=self.other_headers)["id"], batch["id"])
        self.assertEqual(self.client.get("/api/import-batches/" + batch["id"], headers=self.other_headers).status_code, 404)
        self.assertEqual(self.client.post(self.item_path(batch), headers=self.other_headers,
            files={"file": ("run.gpx", SAMPLE_GPX)}).status_code, 404)
        for suffix in ["stop", "resume"]:
            self.mutation("/api/import-batches/" + batch["id"] + "/" + suffix, 404, self.other_headers)
        self.assertEqual(self.client.get("/api/import-batches?page_size=21", headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/api/import-batches?page=9223372036854775808", headers=self.headers).status_code, 422)
        for ident in ["0", "-1", "9223372036854775808"]:
            self.assertEqual(self.client.get("/api/import-batches/" + ident, headers=self.headers).status_code, 422)
            self.mutation("/api/import-batches/" + ident + "/stop", 422)
            self.mutation("/api/uploads/" + ident + "/retry", 422)
            response = self.client.post(f"/api/import-batches/{batch['id']}/items/{ident}/upload",
                headers=self.headers, files={"file": ("run.gpx", SAMPLE_GPX)})
            self.assertEqual(response.status_code, 422, response.text)
        schema = self.client.get("/openapi.json").json()
        upload_schema = schema["paths"]["/api/import-batches/{batch_id}/items/{item_id}/upload"]["post"]
        self.assertEqual(upload_schema["requestBody"]["content"]["multipart/form-data"]["schema"]["properties"]["file"]["format"], "binary")

    def test_stop_preserves_accepted_jobs_and_restart_status(self):
        batch = self.batch([{"name": "a.gpx", "format": "gpx"}, {"name": "b.gpx", "format": "gpx"}])
        accepted = self.upload(batch)
        self.mutation("/api/import-batches/" + batch["id"] + "/stop")
        self.assertEqual(self.upload(batch)["source_id"], accepted["source_id"])
        self.upload(batch, index=1, expected=409)
        self.process()
        detail = self.detail(batch)
        self.assertEqual(detail["counts"], {"awaiting_upload": 1, "queued": 0, "processing": 0,
                                          "succeeded": 1, "failed": 0, "deleted": 0})
        self.assertEqual(detail["state"], "stopped")
        self.mutation("/api/uploads/" + accepted["source_id"] + "/retry", 409)
        with TestClient(create_app(self.engine)) as restarted:
            self.assertEqual(self.detail(batch, restarted), detail)
        self.mutation("/api/import-batches/" + batch["id"] + "/resume")
        duplicate = self.upload(batch, index=1)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["source_id"], accepted["source_id"])
        self.assertEqual(self.detail(batch)["counts"]["succeeded"], 2)

    def test_concurrent_same_item_uploads_and_changed_bytes(self):
        batch = self.batch()
        barrier = threading.Barrier(2)
        def submit():
            with TestClient(self.app) as client:
                barrier.wait(timeout=5)
                return self.upload(batch, client=client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0]["source_id"], results[1]["source_id"])
        self.upload(batch, SAMPLE_GPX + b" ", expected=409)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"),
                                       {"id": self.accounts[0]}).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT count(*) FROM jobs WHERE account_id=:id"),
                                       {"id": self.accounts[0]}).scalar_one(), 1)
        self.assertEqual(len(list(Path(self.temp.name).iterdir())), 1)

    def test_deleted_duplicates_stay_terminal_even_after_new_import(self):
        batch = self.batch([{"name": "a.gpx", "format": "gpx"}, {"name": "b.gpx", "format": "gpx"}])
        accepted = self.upload(batch)
        self.upload(batch, index=1)
        self.process()
        activity_id = self.detail(batch)["items"][0]["activity_id"]
        response = self.client.delete("/api/activities/" + activity_id, headers=self.headers)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.detail(batch)["counts"]["deleted"], 2)
        self.upload(batch, expected=409)
        self.mutation("/api/uploads/" + accepted["source_id"] + "/retry", 404)
        self.mutation("/api/import-batches/" + batch["id"] + "/stop")
        self.mutation("/api/import-batches/" + batch["id"] + "/resume")
        fresh = self.upload(self.batch())
        self.assertNotEqual(fresh["source_id"], accepted["source_id"])
        self.assertEqual(self.detail(batch)["counts"]["deleted"], 2)

    def test_concurrent_retry_is_revision_fenced_and_shared(self):
        batch = self.batch([{"name": "a.gpx", "format": "gpx"}, {"name": "b.gpx", "format": "gpx"}])
        source = self.upload(batch, b"<gpx><trk>")
        self.upload(batch, b"<gpx><trk>", index=1)
        old_job = self.process()
        self.assertEqual(self.detail(batch)["counts"]["failed"], 2)
        path = "/api/uploads/" + source["source_id"] + "/retry"
        self.mutation(path, 404, self.other_headers)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.client.post, path, headers=self.headers) for _ in range(2)]
            responses = [future.result(timeout=10) for future in futures]
        self.assertEqual([response.status_code for response in responses], [204, 204])
        self.assertEqual(self.detail(batch)["counts"]["queued"], 2)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT revision FROM activity_sources WHERE id=:id"),
                                       {"id": int(source["source_id"])}).scalar_one(), 2)
            self.assertEqual(db.execute(text("SELECT count(*) FROM jobs WHERE account_id=:id"),
                                       {"id": self.accounts[0]}).scalar_one(), 2)
        process_upload(self.engine, old_job, self.store)
        self.assertEqual(self.detail(batch)["counts"]["queued"], 2)
        self.process()
        self.assertEqual(self.detail(batch)["counts"]["failed"], 2)

    def test_rollback_preserves_prior_item_and_removes_unreferenced_original(self):
        batch = self.batch([{"name": "a.gpx", "format": "gpx"}, {"name": "b.gpx", "format": "gpx"}])
        source = self.upload(batch)
        with TestClient(self.app, raise_server_exceptions=False) as client:
            with patch("backend.app.import_batches.enqueue", side_effect=RuntimeError("synthetic failure")):
                self.upload(batch, SAMPLE_GPX + b" ", index=1, client=client, expected=500)
        detail = self.detail(batch)
        self.assertEqual(detail["items"][0]["source_id"], source["source_id"])
        self.assertEqual(detail["items"][1]["status"], "awaiting_upload")
        self.assertEqual(len(list(Path(self.temp.name).iterdir())), 1)

    def test_committed_stop_wins_during_streaming(self):
        batch = self.batch()
        streaming, release = threading.Event(), threading.Event()
        boundary = "d18-stream-boundary"
        def chunks():
            yield (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="run.gpx"\r\n'
                   "Content-Type: application/gpx+xml\r\n\r\n").encode()
            streaming.set()
            if not release.wait(timeout=10):
                raise RuntimeError("stream was not released")
            yield SAMPLE_GPX
            yield f"\r\n--{boundary}--\r\n".encode()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client.post, self.item_path(batch),
                headers={**self.headers, "Content-Type": "multipart/form-data; boundary=" + boundary}, content=chunks())
            try:
                self.assertTrue(streaming.wait(timeout=5))
                self.mutation("/api/import-batches/" + batch["id"] + "/stop")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10).status_code, 409)
        self.assertEqual(self.detail(batch)["counts"]["awaiting_upload"], 1)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_commit_failure_removes_unreferenced_original(self):
        batch = self.batch()
        def reject_commit(connection):
            raise RuntimeError("synthetic commit failure")
        event.listen(self.engine, "commit", reject_commit)
        try:
            with TestClient(self.app, raise_server_exceptions=False) as client:
                response = client.post(self.item_path(batch), headers=self.headers,
                    files={"file": ("run.gpx", SAMPLE_GPX)})
                self.assertEqual(response.status_code, 500, response.text)
        finally:
            event.remove(self.engine, "commit", reject_commit)
        self.assertEqual(self.detail(batch)["counts"]["awaiting_upload"], 1)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_deletion_between_duplicate_conflict_and_source_lookup_recovers(self):
        original = self.batch()
        source = self.upload(original)
        self.process()
        activity_id = self.detail(original)["items"][0]["activity_id"]
        new_batch = self.batch()
        reached, release = threading.Event(), threading.Event()
        paused = False
        def after_execute(connection, cursor, statement, parameters, context, many):
            nonlocal paused
            if not paused and "INSERT INTO activity_sources" in statement:
                paused = True
                reached.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("upload race was not released")
        def submit():
            with TestClient(self.app) as client:
                return self.upload(new_batch, client=client)
        event.listen(self.engine, "after_cursor_execute", after_execute)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(submit)
                try:
                    self.assertTrue(reached.wait(timeout=5))
                    response = self.client.delete("/api/activities/" + activity_id, headers=self.headers)
                    self.assertEqual(response.status_code, 204, response.text)
                finally:
                    release.set()
                result = future.result(timeout=10)
            self.assertNotEqual(result["source_id"], source["source_id"])
            self.assertEqual(self.detail(original)["counts"]["deleted"], 1)
            self.assertEqual(self.detail(new_batch)["counts"]["queued"], 1)
        finally:
            release.set()
            event.remove(self.engine, "after_cursor_execute", after_execute)

    def test_retry_between_duplicate_discovery_and_source_lock_revalidates(self):
        invalid = b"<gpx><trk>"
        original = self.batch()
        source = self.upload(original, invalid)
        self.process()
        new_batch = self.batch()
        reached, release = threading.Event(), threading.Event()
        paused = False
        def after_execute(connection, cursor, statement, parameters, context, many):
            nonlocal paused
            if not paused and "SELECT id,revision,status FROM activity_sources" in statement:
                paused = True
                reached.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("retry race was not released")
        event.listen(self.engine, "after_cursor_execute", after_execute)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.upload, new_batch, invalid)
                try:
                    self.assertTrue(reached.wait(timeout=5))
                    self.mutation("/api/uploads/" + source["source_id"] + "/retry")
                finally:
                    release.set()
                result = future.result(timeout=10)
            self.assertEqual(result["source_id"], source["source_id"])
            self.assertTrue(result["duplicate"])
            self.assertEqual(self.detail(original)["counts"]["queued"], 1)
            self.assertEqual(self.detail(new_batch)["counts"]["queued"], 1)
            with self.engine.connect() as db:
                self.assertEqual(db.execute(text("SELECT count(*) FROM jobs WHERE account_id=:id"),
                                           {"id": self.accounts[0]}).scalar_one(), 2)
        finally:
            release.set()
            event.remove(self.engine, "after_cursor_execute", after_execute)
