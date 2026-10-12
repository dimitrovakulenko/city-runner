#!/usr/bin/env python3
"""Run the persistent, loopback-only local City Runner preview."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PREVIEW_ROOT = ROOT / ".local-preview"
PGDATA = PREVIEW_ROOT / "postgres"
PGSOCKETS = PREVIEW_ROOT / "run"
SOURCES = PREVIEW_ROOT / "sources"
JOURNAL_DIR = PREVIEW_ROOT / "journal"
JOURNAL = JOURNAL_DIR / "deletions.jsonl"
LOGS = PREVIEW_ROOT / "logs"
OWNER_FILE = PREVIEW_ROOT / "owner.json"
DATABASE = "city_runner_preview"
PG_HOST = "127.0.0.1"
PG_PORT = 55445
API_HOST = "127.0.0.1"
API_PORT = 8006
DEFAULT_PG_BIN = Path(
    "/private/tmp/city-runner-postgres16-mount/Postgres.app/Contents/Versions/16/bin"
)
REQUIRED_TABLES = frozenset({
    "activities", "accounts", "login_identities", "login_challenges", "sessions",
    "recording_identity_deletions",
    "jobs", "map_datasets", "cities", "osm_nodes", "osm_ways", "streets",
    "street_ways", "street_nodes", "activity_sources", "source_node_contributions",
    "coverage_source_runs", "account_dataset_coverage", "account_street_coverage",
    "manual_street_completions", "import_batches", "import_batch_items", "saved_routes",
    "routing_provider_quota", "account_deletions", "account_deletion_outbox",
    "sync_account_revisions", "sync_geography_revision", "sync_revision_events",
})
ALLOWED_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    f"http://{API_HOST}:{API_PORT}",
}
ALLOWED_HOSTS = {
    "127.0.0.1:5173",
    "localhost:5173",
    f"{API_HOST}:{API_PORT}",
}


class PreviewError(RuntimeError):
    pass


def _url(database: str = DATABASE) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=getpass.getuser(),
        host=PG_HOST,
        port=PG_PORT,
        database=database,
    )


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise PreviewError(f"Required preview directory is missing: {path.relative_to(PREVIEW_ROOT)}") from error
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise PreviewError(f"Preview directory is not private and user-owned: {path.relative_to(PREVIEW_ROOT)}")


def _validate_root(*, create: bool) -> None:
    if create:
        created = not PREVIEW_ROOT.exists()
        PREVIEW_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        if created:
            _fsync_directory(PREVIEW_ROOT.parent)
    try:
        info = PREVIEW_ROOT.lstat()
    except FileNotFoundError as error:
        raise PreviewError("Preview root is missing; initialize it with --init first.") from error
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise PreviewError("Preview root must be a private, user-owned directory.")
    for path in (PGDATA, PGSOCKETS, SOURCES, JOURNAL_DIR, LOGS):
        _private_directory(path, create=create)


def _fresh_install_preflight() -> None:
    """Allow only pre-provisioned empty directories; never reset partial state."""
    if not PREVIEW_ROOT.exists():
        PREVIEW_ROOT.mkdir(mode=0o700, parents=True)
        _fsync_directory(PREVIEW_ROOT.parent)
    root_info = PREVIEW_ROOT.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid()
            or stat.S_IMODE(root_info.st_mode) & 0o077):
        raise PreviewError("Preview root must be a private, user-owned directory.")
    allowed = {
        PGDATA.name: PGDATA,
        PGSOCKETS.name: PGSOCKETS,
        SOURCES.name: SOURCES,
        JOURNAL_DIR.name: JOURNAL_DIR,
        LOGS.name: LOGS,
    }
    for entry in PREVIEW_ROOT.iterdir():
        if entry.name not in allowed:
            raise PreviewError("--init refuses an existing or partially initialized preview.")
        _private_directory(entry, create=False)
        if next(entry.iterdir(), None) is not None:
            raise PreviewError("--init refuses an existing or partially initialized preview.")
    for path in allowed.values():
        _private_directory(path, create=True)
    _fsync_directory(PREVIEW_ROOT)


def _tools(pg_bin: Path) -> dict[str, Path]:
    tools = {name: pg_bin / name for name in ("initdb", "pg_ctl")}
    for name, path in tools.items():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise PreviewError(f"PostgreSQL 16 executable is unavailable: {path}")
    return tools


def _pg_control(pg_ctl: Path, action: str, *, started_by_runner: bool = False) -> None:
    log_path = LOGS / "postgres.log"
    if action == "start":
        options = ["-h", PG_HOST, "-p", str(PG_PORT), "-k", str(PGSOCKETS)]
        _run([str(pg_ctl), "-D", str(PGDATA), "-l", str(log_path), "-o",
              shlex.join(options), "-w", "start"])
    elif action == "stop" and started_by_runner:
        _run([str(pg_ctl), "-D", str(PGDATA), "-m", "fast", "-w", "stop"])


def _pg_running(pg_ctl: Path) -> bool:
    result = subprocess.run([str(pg_ctl), "-D", str(PGDATA), "status"],
                            cwd=ROOT, check=False, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return result.returncode == 0


def _verify_server(engine: Engine) -> None:
    with engine.connect() as db:
        row = db.execute(text("""SELECT current_setting('data_directory') AS data_directory,
            current_setting('listen_addresses') AS listen_addresses,
            current_setting('port') AS port""")).mappings().one()
    actual_data = Path(row["data_directory"]).resolve()
    if (actual_data != PGDATA.resolve() or row["listen_addresses"] != PG_HOST
            or row["port"] != str(PG_PORT)):
        raise PreviewError("Refusing a PostgreSQL server that does not match the private preview cluster.")


def _database_exists(admin_engine: Engine) -> bool:
    with admin_engine.connect() as db:
        return bool(db.execute(text("SELECT 1 FROM pg_database WHERE datname=:name"), {
            "name": DATABASE,
        }).first())


def _check_database(engine: Engine) -> None:
    """Fail closed if Alembic claims head while required schema is damaged."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    expected_heads = set(ScriptDirectory.from_config(
        Config(str(ROOT / "backend" / "alembic.ini"))
    ).get_heads())
    schema = "public" if engine.dialect.name == "postgresql" else None
    try:
        with engine.connect() as db:
            actual_heads = set(db.execute(text("SELECT version_num FROM alembic_version")).scalars())
        actual_tables = set(inspect(engine).get_table_names(schema=schema))
    except Exception as error:
        raise PreviewError("Preview database schema is missing or unreadable; refusing to start.") from error
    missing = REQUIRED_TABLES - actual_tables
    if actual_heads != expected_heads or missing:
        detail = "required tables are missing" if missing else "migration revision is not at head"
        raise PreviewError(f"Preview database is incomplete ({detail}); refusing to start.")
    if engine.dialect.name == "postgresql":
        with engine.connect() as db:
            has_postgis = db.execute(text(
                "SELECT 1 FROM pg_extension WHERE extname='postgis'"
            )).first()
        if not has_postgis:
            raise PreviewError("Preview database is missing PostGIS; refusing to start.")


def _migrate(database_url: str) -> None:
    env = os.environ.copy()
    env.update(_runtime_environment(database_url))
    _run([sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"], env=env)


def _runtime_environment(database_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "UPLOAD_STORAGE_DIR": str(SOURCES),
        "ACCOUNT_DELETION_LEDGER": str(JOURNAL),
    }


def _write_owner(account_id: str) -> None:
    payload = json.dumps({"version": 1, "account_id": account_id}, separators=(",", ":")).encode()
    descriptor = os.open(OWNER_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            OWNER_FILE.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(PREVIEW_ROOT)


def _read_owner() -> str:
    try:
        info = OWNER_FILE.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise PreviewError("Preview owner metadata is not a private, user-owned file.")
        value = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
        account_id = value.get("account_id") if isinstance(value, dict) and value.get("version") == 1 else None
        if not isinstance(account_id, str) or str(uuid.UUID(account_id)) != account_id:
            raise ValueError("invalid preview owner")
        return account_id
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise PreviewError("Preview owner metadata is missing or corrupt.") from error


def _write_fresh_layout() -> None:
    for path in (PREVIEW_ROOT, PGDATA, PGSOCKETS, SOURCES, JOURNAL_DIR, LOGS):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def _initialize(pg_bin: Path) -> None:
    tools = _tools(pg_bin)
    _fresh_install_preflight()
    _write_fresh_layout()
    if next(PGDATA.iterdir(), None) is not None:
        raise PreviewError("--init refuses a non-empty PostgreSQL data directory.")

    _run([str(tools["initdb"]), "-D", str(PGDATA), "--no-locale", "--encoding=UTF8",
          f"--username={getpass.getuser()}", "--auth-local=trust", "--auth-host=trust"])
    started = False
    admin_engine = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        started = True
        _pg_control(tools["pg_ctl"], "start")
        _verify_server(admin_engine)
        if _database_exists(admin_engine):
            raise PreviewError("--init refuses an existing preview database.")
        with admin_engine.connect() as db:
            db.exec_driver_sql(f'CREATE DATABASE "{DATABASE}"')

        database_url = _url().render_as_string(hide_password=False)
        _migrate(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            _check_database(engine)
        finally:
            engine.dispose()
        os.environ.update(_runtime_environment(database_url))
        from backend.app.account_deletion_journal import initialize_ledger

        initialize_ledger()
        account_id = str(uuid.uuid4())
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.begin() as db:
                db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": account_id})
        finally:
            engine.dispose()
        _write_owner(account_id)
    finally:
        admin_engine.dispose()
        if started and _pg_running(tools["pg_ctl"]):
            _pg_control(tools["pg_ctl"], "stop", started_by_runner=True)


def _validate_initialized_state(pg_bin: Path) -> tuple[str, bool]:
    _validate_root(create=False)
    owner_id = _read_owner()
    if not PGDATA.joinpath("PG_VERSION").is_file():
        raise PreviewError("Preview PostgreSQL cluster is missing; refusing to initialize it on normal start.")
    if not JOURNAL.is_file():
        raise PreviewError("Preview deletion journal is missing; refusing to initialize a replacement.")
    os.environ.update({"ACCOUNT_DELETION_LEDGER": str(JOURNAL)})
    from backend.app.account_deletion_journal import JournalError, read_records_durable

    try:
        read_records_durable()
    except JournalError as error:
        raise PreviewError("Preview deletion journal is corrupt; refusing to start.") from error
    tools = _tools(pg_bin)
    was_running = _pg_running(tools["pg_ctl"])
    return owner_id, not was_running


def _create_preview_app(engine: Engine, owner_id: str):
    from backend.app.main import create_app
    from backend.app.owner_guard import owner_lock

    app = create_app(engine)

    @app.post("/api/dev/session", include_in_schema=False)
    def development_session(request: Request):
        try:
            remote = ipaddress.ip_address(request.client.host if request.client else "")
        except ValueError:
            remote = None
        origin = request.headers.get("origin")
        host = request.headers.get("host", "").lower()
        if (remote is None or not remote.is_loopback or origin not in ALLOWED_ORIGINS
                or host not in ALLOWED_HOSTS):
            raise HTTPException(status_code=403, detail="Development sessions are loopback-only.")
        token = secrets.token_urlsafe(32)
        with engine.begin() as db:
            # Never recreate the synthetic account: deletion remains terminal.
            owner_lock(db, owner_id, require_account=True)
            db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                VALUES (:digest,:account,:expires_at)"""), {
                "digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "account": owner_id,
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=4),
            })
        return {"session_token": token}

    return app


def _check_api_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((API_HOST, API_PORT))
        except OSError as error:
            raise PreviewError(f"Preview API port {API_PORT} is already in use.") from error


def _exit_cleanly_on_sigterm(signum, frame) -> None:
    raise SystemExit(0)


@contextmanager
def _graceful_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _exit_cleanly_on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _serve(pg_bin: Path) -> None:
    owner_id, started_postgres = _validate_initialized_state(pg_bin)
    database_url = _url().render_as_string(hide_password=False)
    os.environ.update(_runtime_environment(database_url))
    pg_ctl = _tools(pg_bin)["pg_ctl"]
    admin_engine = create_engine(_url("postgres"), pool_pre_ping=True)
    app_engine = None
    worker = None
    try:
        if started_postgres:
            _pg_control(pg_ctl, "start")
        _verify_server(admin_engine)
        if not _database_exists(admin_engine):
            raise PreviewError("Initialized preview database is missing; refusing to create it on normal start.")
        _migrate(database_url)
        app_engine = create_engine(database_url, pool_pre_ping=True)
        _check_database(app_engine)
        try:
            with app_engine.begin() as db:
                from backend.app.owner_guard import owner_lock

                owner_lock(db, owner_id, require_account=True)
        except HTTPException as error:
            raise PreviewError("Persisted preview account was deleted or is unavailable; refusing to recreate it.") from error
        _check_api_port()

        env = os.environ.copy()
        env.update(_runtime_environment(database_url))
        worker = subprocess.Popen([sys.executable, "-m", "backend.app.worker"], cwd=ROOT, env=env)

        import uvicorn

        app = _create_preview_app(app_engine, owner_id)
        print(f"Local preview API: http://{API_HOST}:{API_PORT}", flush=True)
        print(f"Preview data: {PREVIEW_ROOT}", flush=True)
        uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")
    finally:
        if worker is not None and worker.poll() is None:
            worker.send_signal(signal.SIGTERM)
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        if app_engine is not None:
            app_engine.dispose()
        admin_engine.dispose()
        if started_postgres and _pg_running(pg_ctl):
            _pg_control(pg_ctl, "stop", started_by_runner=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="explicitly create a fresh persistent preview")
    parser.add_argument("--pg-bin", type=Path, default=DEFAULT_PG_BIN,
                        help="directory containing the existing PostgreSQL 16 initdb and pg_ctl binaries")
    args = parser.parse_args(argv)
    try:
        with _graceful_sigterm():
            if args.init:
                _initialize(args.pg_bin.expanduser().resolve())
                print(f"Initialized empty local preview at {PREVIEW_ROOT}", flush=True)
            else:
                _serve(args.pg_bin.expanduser().resolve())
    except (PreviewError, OSError, subprocess.CalledProcessError) as error:
        print(f"Local preview stopped safely: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
