"""Fail-closed checks for the persistent local preview launcher."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from alembic.config import Config
from alembic.script import ScriptDirectory

from backend.app.account_deletion_journal import append_deletion, initialize_ledger
from scripts.dev import preview


class PreviewTests(unittest.TestCase):
    def preview_paths(self, root: Path):
        return {
            "PREVIEW_ROOT": root,
            "PGDATA": root / "postgres",
            "PGSOCKETS": root / "run",
            "SOURCES": root / "sources",
            "JOURNAL_DIR": root / "journal",
            "JOURNAL": root / "journal" / "deletions.jsonl",
            "LOGS": root / "logs",
            "OWNER_FILE": root / "owner.json",
        }

    def patch_paths(self, root: Path):
        return [patch.object(preview, name, path) for name, path in self.preview_paths(root).items()]

    def test_init_rejects_partial_state_without_replacing_or_removing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".local-preview"
            root.mkdir(mode=0o700)
            journal_dir = root / "journal"
            journal_dir.mkdir(mode=0o700)
            ledger = journal_dir / "deletions.jsonl"
            ledger.write_text("preserve-me\n", encoding="utf-8")
            contexts = self.patch_paths(root)
            try:
                for context in contexts:
                    context.__enter__()
                with self.assertRaisesRegex(preview.PreviewError, "partially initialized"):
                    preview._fresh_install_preflight()
                self.assertEqual(ledger.read_text(encoding="utf-8"), "preserve-me\n")
            finally:
                for context in reversed(contexts):
                    context.__exit__(None, None, None)

    def test_fresh_preprovisioned_layout_accepts_empty_directories_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".local-preview"
            root.mkdir(mode=0o700)
            contexts = self.patch_paths(root)
            try:
                for context in contexts:
                    context.__enter__()
                preview._fresh_install_preflight()
                for path in (root / "postgres", root / "run", root / "sources", root / "journal", root / "logs"):
                    self.assertTrue(path.is_dir())
                    self.assertEqual(list(path.iterdir()), [])
            finally:
                for context in reversed(contexts):
                    context.__exit__(None, None, None)

    def test_initializing_a_new_root_fsyncs_its_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".local-preview"
            contexts = self.patch_paths(root)
            try:
                for context in contexts:
                    context.__enter__()
                with patch.object(preview, "_fsync_directory") as fsync:
                    preview._fresh_install_preflight()
                fsync.assert_any_call(root.parent)
            finally:
                for context in reversed(contexts):
                    context.__exit__(None, None, None)

    def test_startup_refuses_missing_journal_without_reinitializing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".local-preview"
            paths = self.preview_paths(root)
            for name in ("PREVIEW_ROOT", "PGDATA", "PGSOCKETS", "SOURCES", "JOURNAL_DIR", "LOGS"):
                paths[name].mkdir(mode=0o700, parents=True, exist_ok=True)
            (paths["PGDATA"] / "PG_VERSION").write_text("16\n", encoding="ascii")
            owner = str(uuid.uuid4())
            paths["OWNER_FILE"].write_text(json.dumps({"version": 1, "account_id": owner}), encoding="utf-8")
            paths["OWNER_FILE"].chmod(0o600)
            contexts = [patch.object(preview, name, path) for name, path in paths.items()]
            try:
                for context in contexts:
                    context.__enter__()
                with self.assertRaisesRegex(preview.PreviewError, "journal is missing"):
                    preview._validate_initialized_state(Path("unused"))
                self.assertFalse(paths["JOURNAL"].exists())
            finally:
                for context in reversed(contexts):
                    context.__exit__(None, None, None)

    def test_database_head_marker_does_not_hide_missing_required_tables(self):
        engine = create_engine("sqlite://")
        try:
            head = ScriptDirectory.from_config(
                Config(str(preview.ROOT / "backend" / "alembic.ini"))
            ).get_current_head()
            with engine.begin() as db:
                db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                db.execute(text("INSERT INTO alembic_version(version_num) VALUES (:head)"), {"head": head})
                for table in preview.REQUIRED_TABLES - {"sessions"}:
                    db.execute(text(f'CREATE TABLE "{table}" (id INTEGER)'))
            with self.assertRaisesRegex(preview.PreviewError, "required tables are missing"):
                preview._check_database(engine)
            with engine.begin() as db:
                db.execute(text('CREATE TABLE sessions (id INTEGER)'))
            preview._check_database(engine)
        finally:
            engine.dispose()

    def test_owner_metadata_is_strict_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner_file = root / "owner.json"
            contexts = [patch.object(preview, "PREVIEW_ROOT", root),
                        patch.object(preview, "OWNER_FILE", owner_file)]
            try:
                for context in contexts:
                    context.__enter__()
                owner = str(uuid.uuid4())
                owner_file.write_text(json.dumps({"version": 1, "account_id": owner}), encoding="utf-8")
                owner_file.chmod(0o600)
                self.assertEqual(preview._read_owner(), owner)
                owner_file.unlink()
                owner_file.symlink_to(root / "target")
                with self.assertRaisesRegex(preview.PreviewError, "not a private"):
                    preview._read_owner()
            finally:
                for context in reversed(contexts):
                    context.__exit__(None, None, None)

    def test_dev_session_is_loopback_origin_scoped_and_never_recreates_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "journal" / "deletions.jsonl"
            ledger.parent.mkdir(mode=0o700)
            with patch.dict(os.environ, {"ACCOUNT_DELETION_LEDGER": str(ledger)}):
                initialize_ledger()
                engine = create_engine("sqlite://", poolclass=StaticPool,
                                       connect_args={"check_same_thread": False})
                try:
                    with engine.begin() as db:
                        db.execute(text("CREATE TABLE accounts (id TEXT PRIMARY KEY)"))
                        db.execute(text("CREATE TABLE sessions (token_digest TEXT PRIMARY KEY, account_id TEXT, expires_at TEXT)"))
                    owner = str(uuid.uuid4())
                    with engine.begin() as db:
                        db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": owner})
                    app = preview._create_preview_app(engine, owner)
                    with TestClient(app, client=("127.0.0.1", 12345)) as client:
                        wrong_origin = client.post("/api/dev/session", headers={
                            "Origin": "http://127.0.0.1:9000", "Host": "127.0.0.1:5173",
                        })
                        self.assertEqual(wrong_origin.status_code, 403)
                        response = client.post("/api/dev/session", headers={
                            "Origin": "http://127.0.0.1:5173", "Host": "127.0.0.1:5173",
                        })
                        self.assertEqual(response.status_code, 200, response.text)
                        token = response.json()["session_token"]
                        self.assertTrue(token)
                        with engine.connect() as db:
                            before = db.execute(text("SELECT count(*) FROM sessions")).scalar_one()
                        append_deletion(owner, [])
                        deleted = client.post("/api/dev/session", headers={
                            "Origin": "http://127.0.0.1:5173", "Host": "127.0.0.1:5173",
                        })
                        self.assertEqual(deleted.status_code, 410)
                        with engine.connect() as db:
                            self.assertEqual(db.execute(text("SELECT count(*) FROM sessions")).scalar_one(), before)
                            self.assertEqual(db.execute(text("SELECT count(*) FROM accounts WHERE id=:id"), {
                                "id": owner,
                            }).scalar_one(), 1)
                finally:
                    engine.dispose()

    def test_shutdown_stops_postgres_only_when_runner_started_it(self):
        with patch.object(preview, "_run") as run:
            preview._pg_control(Path("pg_ctl"), "stop", started_by_runner=False)
            run.assert_not_called()
            preview._pg_control(Path("pg_ctl"), "stop", started_by_runner=True)
            run.assert_called_once()

    def test_sigterm_during_runner_startup_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            marker = Path(directory) / "clean-shutdown"
            code = f'''\
import signal
from pathlib import Path
from scripts.dev import preview
def startup(_pg_bin):
    try:
        Path({str(ready)!r}).write_text("ready")
        signal.pause()
    finally:
        Path({str(marker)!r}).write_text("clean")
preview._serve = startup
preview.main([])
'''
            child = subprocess.Popen([sys.executable, "-c", code], cwd=preview.ROOT)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    if child.poll() is not None:
                        self.fail("Runner exited before startup reached the cleanup guard")
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "Runner startup did not begin")
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertEqual(marker.read_text(encoding="utf-8"), "clean")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_uvicorn_sigterm_replay_runs_runner_finally(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", 0))
            except PermissionError:
                self.skipTest("sandbox does not permit temporary loopback listeners")
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "clean-shutdown"
            code = f'''\
import sys
import uvicorn
from fastapi import FastAPI
from scripts.dev.preview import _graceful_sigterm
app = FastAPI()
@app.get("/")
def ready(): return {{"ready": True}}
try:
    with _graceful_sigterm():
        uvicorn.run(app, host="127.0.0.1", port={port}, log_level="critical")
finally:
    open({str(marker)!r}, "w").write("clean")
'''
            child = subprocess.Popen([sys.executable, "-c", code], cwd=preview.ROOT)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        self.fail("Uvicorn exited before accepting a connection")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Uvicorn did not start in time")
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertEqual(marker.read_text(encoding="utf-8"), "clean")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
