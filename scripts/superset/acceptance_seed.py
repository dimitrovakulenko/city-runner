"""Seed only a generated disposable application DB; no test login HTTP route."""

import hashlib
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.app.account_deletion_journal import initialize_ledger
from backend.app.activity_statistics import backfill_activity_statistics
from backend.app.storage import LocalObjectStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activities", type=int, default=4, choices=range(1, 10001))
    parser.add_argument("--samples", type=int, default=100, choices=range(2, 1001))
    args = parser.parse_args()
    url = make_url(os.environ["DATABASE_URL"])
    if re.fullmatch(r"city_runner_analytics_acceptance_[0-9a-f]{12}", url.database or "") is None:
        raise RuntimeError("Only a generated disposable acceptance database is permitted.")
    engine = create_engine(url)
    with engine.connect() as db:
        if db.exec_driver_sql("SELECT to_regclass('public.accounts') IS NOT NULL").scalar_one():
            raise RuntimeError("Acceptance seeding requires a new empty application database.")
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(config, "head")
    initialize_ledger()
    spec = importlib.util.spec_from_file_location("history_fixture", ROOT / "scripts/dev/history-benchmark.py")
    history = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = history
    spec.loader.exec_module(history)
    accounts = [(f"acceptance-{name}-{secrets.token_hex(6)}", secrets.token_urlsafe(32)) for name in ("a", "b")]
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "synthetic.osm"
        rows = history.geography(path)
        dataset = history.import_osm_xml(engine, path, region="Analytics acceptance synthetic cities",
            city_relation_ids=[900, 901], source_timestamp="2026-10-15T00:00:00Z",
            coverage_mode="complete", coverage_evidence="Disposable analytics acceptance fixture")["dataset_id"]
        with engine.begin() as db:
            for account, token in accounts:
                db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": account})
                db.execute(text("INSERT INTO sessions(token_digest,account_id,expires_at) VALUES (:digest,:id,:expires)"), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "id": account,
                    "expires": datetime.now(timezone.utc) + timedelta(hours=12),
                })
        history.seed(engine, LocalObjectStore(), int(dataset), rows, accounts, args.activities, 2, args.samples)
        for account, _ in accounts:
            backfill_activity_statistics(engine, account)
    output = Path("/var/lib/city-runner/acceptance.json")
    output.write_text(json.dumps({"dataset_id": str(dataset), "activity_counts": {"A": args.activities, "B": 2},
        "gps_samples": args.activities * args.samples, "accounts": {
        name: {"id": account, "token": token} for name, (account, token) in zip(("A", "B"), accounts)
    }}))
    output.chmod(0o600)
    engine.dispose()
    print("Seeded two synthetic accounts; private acceptance credentials stay in the state volume.")


if __name__ == "__main__":
    main()
