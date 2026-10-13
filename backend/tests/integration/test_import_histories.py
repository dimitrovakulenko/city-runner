"""Bounded full-history manifests, byte verification and parent fencing."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from backend.app.account_deletion_journal import initialize_ledger
from backend.app.import_batches import _attach_file
from backend.app.main import create_app
from backend.app.storage import LocalObjectStore
from backend.app.uploads import process_upload
from backend.app.worker import run_once
from backend.tests.test_fit import fit_fixture, semicircles


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
FIT_BYTES = fit_fixture(records=((0x10000020, semicircles(50), semicircles(4)),
                                 (0x10000021, semicircles(50.1), semicircles(4.1))))


@unittest.skipUnless(DATABASE_URL, "run with disposable PostGIS integration harness")
class ImportHistoryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (make_url(DATABASE_URL).database or "").startswith("city_runner_test_"):
            raise RuntimeError("history import tests require generated disposable PostGIS")
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(config, "head")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.account, self.other_account = str(uuid.uuid4()), str(uuid.uuid4())
        self.temp = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp.name) / "originals"
        self.ledger_path = Path(self.temp.name) / "private" / "deletions.jsonl"
        self.environment = patch.dict(os.environ, {
            "UPLOAD_STORAGE_DIR": str(self.storage_root),
            "ACCOUNT_DELETION_LEDGER": str(self.ledger_path),
        })
        self.environment.start()
        initialize_ledger()
        self.store = LocalObjectStore(self.storage_root)
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:a),(:b)"), {
                "a": self.account, "b": self.other_account,
            })
        self.client = TestClient(create_app(self.engine, lambda: self.account))
        self.other_client = TestClient(create_app(self.engine, lambda: self.other_account))

    def tearDown(self):
        self.client.close()
        self.other_client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id IN (:a,:b)"), {
                "a": self.account, "b": self.other_account,
            })
            db.execute(text("DELETE FROM accounts WHERE id IN (:a,:b)"), {
                "a": self.account, "b": self.other_account,
            })
        self.environment.stop()
        self.temp.cleanup()

    def file_spec(self, name="run.fit", data=FIT_BYTES, *, sha256=None, size=None):
        return {"name": name, "format": "fit", "size": len(data) if size is None else size,
                "sha256": hashlib.sha256(data).hexdigest() if sha256 is None else sha256}

    def history_body(self, files, *, request_id=None, label="selected-history"):
        return {"request_id": request_id or str(uuid.uuid4()), "label": label, "files": files}

    def create_history(self, body, *, client=None, expected=201):
        response = (client or self.client).post("/api/import-histories", json=body)
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def test_atomic_chunked_manifest_replay_summary_owner_export_and_cascade(self):
        body = self.history_body([self.file_spec(f"run-{index}.fit") for index in range(21)])
        before = self.engine.connect()
        try:
            rev_before = before.execute(text("SELECT revision FROM sync_account_revisions WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one_or_none() or 0
        finally:
            before.close()
        response = self.create_history(body)
        result = response.json()
        self.assertEqual(result["file_count"], 21)
        self.assertEqual(result["counts"]["awaiting_upload"], 21)
        self.assertEqual(len(result["batch_ids"]), 2)
        self.assertEqual(result["duplicate_count"], 0)

        replay = self.create_history(body)
        self.assertEqual(replay.json()["id"], result["id"])
        changed = {**body, "label": "different"}
        self.assertEqual(self.create_history(changed, expected=409).status_code, 409)
        batches = self.client.get(f"/api/import-histories/{result['id']}/batches?page=1&page_size=1")
        self.assertEqual(batches.status_code, 200)
        self.assertEqual(batches.json()["total"], 2)
        first_batch = batches.json()["items"][0]
        self.assertEqual(first_batch["history_id"], result["id"])
        self.assertEqual(first_batch["items"][0]["expected_sha256"], body["files"][0]["sha256"])
        self.assertEqual(first_batch["items"][0]["expected_size"], len(FIT_BYTES))
        self.assertEqual(self.client.get("/api/import-batches").json()["total"], 0)
        self.assertEqual(self.other_client.get(f"/api/import-histories/{result['id']}").status_code, 404)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT count(*) FROM jobs WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 0)
            rev_after = db.execute(text("SELECT revision FROM sync_account_revisions WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one()
        self.assertGreater(rev_after, rev_before)

        with self.client.stream("GET", "/api/account/export") as exported:
            self.assertEqual(exported.status_code, 200)
            archive_bytes = b"".join(exported.iter_bytes())
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            histories = json.loads(archive.read("data/import_histories.json"))
            batch_rows = json.loads(archive.read("data/import_batches.json"))
            item_rows = json.loads(archive.read("data/import_batch_items.json"))
        self.assertEqual(len(histories), 1)
        self.assertEqual(sum(row["history_id"] == result["id"] for row in batch_rows), 2)
        self.assertEqual(len(item_rows), 21)
        self.assertEqual(item_rows[0]["expected_sha256"], body["files"][0]["sha256"])

        with self.engine.begin() as db:
            db.execute(text("DELETE FROM accounts WHERE id=:id"), {"id": self.account})
        with self.engine.connect() as db:
            for table in ("import_histories", "import_batches", "import_batch_items"):
                self.assertEqual(db.execute(text(f"SELECT count(*) FROM {table} WHERE account_id=:id"), {
                    "id": self.account,
                }).scalar_one(), 0)

    def test_limits_and_oversized_json_are_rejected_before_manifest_rows(self):
        too_many_bytes = self.history_body([self.file_spec(f"large-{i}.fit", size=10 * 1024 * 1024)
                                            for i in range(26)])
        self.assertEqual(self.create_history(too_many_bytes, expected=422).status_code, 422)
        bad_hash = self.history_body([self.file_spec(sha256="A" * 64)])
        self.assertEqual(self.create_history(bad_hash, expected=422).status_code, 422)
        unsafe_name = self.history_body([self.file_spec(name="unsafe.fit")])
        unsafe_name["files"][0]["name"] = "bad\ud800.fit"
        invalid_unicode = self.client.post("/api/import-histories",
            content=json.dumps(unsafe_name, ensure_ascii=True).encode("ascii"),
            headers={"content-type": "application/json"})
        self.assertEqual(invalid_unicode.status_code, 422)
        self.assertEqual(invalid_unicode.json(), {"detail": "Invalid history manifest."})
        oversized = self.client.post("/api/import-histories", content=b" " * (4 * 1024 * 1024 + 1),
                                     headers={"content-type": "application/json"})
        self.assertEqual(oversized.status_code, 413, oversized.text)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM import_histories WHERE account_id=:id"), {
                "id": self.account,
            }).scalar_one(), 0)

    def test_declared_bytes_are_checked_before_original_write_or_item_pin(self):
        for file_spec in (self.file_spec(size=len(FIT_BYTES) - 1),
                          self.file_spec(sha256="0" * 64)):
            history = self.create_history(self.history_body([file_spec])).json()
            batch = self.client.get(f"/api/import-histories/{history['id']}/batches").json()["items"][0]
            item = batch["items"][0]
            response = self.client.post(f"/api/import-batches/{batch['id']}/items/{item['id']}/upload",
                                        files={"file": (item["name"], FIT_BYTES, "application/octet-stream")})
            self.assertEqual(response.status_code, 409, response.text)
            detail = self.client.get(f"/api/import-batches/{batch['id']}").json()["items"][0]
            self.assertEqual(detail["status"], "awaiting_upload")
            self.assertIsNone(detail["source_id"])
        self.assertEqual(self.store.list_owned_keys(self.account), [])
        with self.engine.connect() as db:
            row_count = db.execute(text("SELECT count(*) FROM import_batch_items WHERE account_id=:id AND source_id IS NOT NULL"), {
                "id": self.account,
            }).scalar_one()
        self.assertEqual(row_count, 0)

    def test_child_creation_rolls_back_with_parent_when_item_insert_fails(self):
        trigger_client = TestClient(create_app(self.engine, lambda: self.account), raise_server_exceptions=False)
        try:
            with self.engine.begin() as db:
                db.execute(text("""CREATE FUNCTION reject_history_manifest_test() RETURNS trigger AS $$
                    BEGIN RAISE EXCEPTION 'synthetic item failure'; END
                    $$ LANGUAGE plpgsql"""))
                db.execute(text("""CREATE TRIGGER reject_history_manifest_test BEFORE INSERT ON import_batch_items
                    FOR EACH ROW EXECUTE FUNCTION reject_history_manifest_test()"""))
            body = self.history_body([self.file_spec("rollback.fit")])
            response = trigger_client.post("/api/import-histories", json=body)
            self.assertEqual(response.status_code, 500)
            with self.engine.connect() as db:
                for table in ("import_histories", "import_batches", "import_batch_items"):
                    self.assertEqual(db.execute(text(f"SELECT count(*) FROM {table} WHERE account_id=:id"), {
                        "id": self.account,
                    }).scalar_one(), 0)
        finally:
            trigger_client.close()
            with self.engine.begin() as db:
                db.execute(text("DROP TRIGGER IF EXISTS reject_history_manifest_test ON import_batch_items"))
                db.execute(text("DROP FUNCTION IF EXISTS reject_history_manifest_test()"))

    def test_stop_racing_acceptance_serializes_parent_and_resume_spans_child_batches(self):
        body = self.history_body([self.file_spec(f"same-{i}.fit") for i in range(21)])
        history = self.create_history(body).json()
        pages = self.client.get(f"/api/import-histories/{history['id']}/batches?page_size=20").json()
        first_batch, second_batch = pages["items"]
        first_item = first_batch["items"][0]
        later_item = second_batch["items"][0]
        write_started, release_write, stop_lock = threading.Event(), threading.Event(), threading.Event()
        original_write = self.store.write
        errors = []

        def pause_write(*args, **kwargs):
            key = original_write(*args, **kwargs)
            write_started.set()
            if not release_write.wait(5):
                raise RuntimeError("history stop race timed out")
            return key

        def observe_parent_lock(_connection, _cursor, statement, _parameters, _context, _executemany):
            if "SELECT id FROM import_histories" in statement and "FOR UPDATE" in statement:
                stop_lock.set()

        event.listen(self.engine, "before_cursor_execute", observe_parent_lock)
        try:
            with patch.object(self.store, "write", side_effect=pause_write):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    accepting = pool.submit(_attach_file, self.engine, self.store, self.account,
                        int(first_batch["id"]), int(first_item["id"]), FIT_BYTES, "fit")
                    self.assertTrue(write_started.wait(5))
                    stopping = pool.submit(self.client.post, f"/api/import-histories/{history['id']}/stop")
                    self.assertTrue(stop_lock.wait(5))
                    self.assertFalse(stopping.done())
                    release_write.set()
                    accepting.result(timeout=10)
                    self.assertEqual(stopping.result(timeout=10).status_code, 204)
        finally:
            release_write.set()
            event.remove(self.engine, "before_cursor_execute", observe_parent_lock)

        accepted = self.client.get(f"/api/import-batches/{first_batch['id']}").json()["items"][0]
        self.assertEqual(accepted["status"], "queued")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE account_id=:account AND kind='process_upload'"), {
                "account": self.account,
            }).scalar_one(), "queued")
        later = self.client.post(f"/api/import-batches/{second_batch['id']}/items/{later_item['id']}/upload",
                                 files={"file": (later_item["name"], FIT_BYTES, "application/octet-stream")})
        self.assertEqual(later.status_code, 409)
        child_resume = self.client.post(f"/api/import-batches/{second_batch['id']}/resume")
        self.assertEqual(child_resume.status_code, 409)
        self.assertEqual(self.client.post(f"/api/import-histories/{history['id']}/resume").status_code, 204)
        self.assertEqual(self.client.post(f"/api/import-batches/{second_batch['id']}/items/{later_item['id']}/upload",
            files={"file": (later_item["name"], FIT_BYTES, "application/octet-stream")}).status_code, 202)
        self.assertTrue(run_once(self.engine, {
            "process_upload": lambda job: process_upload(self.engine, job, self.store),
        }))
        summary = self.client.get(f"/api/import-histories/{history['id']}").json()
        self.assertEqual(summary["state"], "open")
        self.assertEqual(summary["counts"]["succeeded"], 2)
        self.assertEqual(summary["duplicate_count"], 1)


if __name__ == "__main__":
    unittest.main()
