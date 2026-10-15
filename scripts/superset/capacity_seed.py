"""Add a 5,000-activity/5-million-sample synthetic owner to acceptance only."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.app.activity_statistics import backfill_activity_statistics
from backend.app.storage import LocalObjectStore


def main():
    url = make_url(os.environ["DATABASE_URL"])
    if re.fullmatch(r"city_runner_analytics_acceptance_[0-9a-f]{12}", url.database or "") is None:
        raise RuntimeError("A generated disposable acceptance database is required.")
    path = Path("/var/lib/city-runner/acceptance.json")
    fixture = json.loads(path.read_text())
    if "capacity" in fixture:
        raise RuntimeError("Capacity fixture already exists.")
    spec = importlib.util.spec_from_file_location("capacity_history", ROOT / "scripts/dev/history-benchmark.py")
    history = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = history
    spec.loader.exec_module(history)
    engine = create_engine(url)
    account, token = "acceptance-load-" + uuid.uuid4().hex, secrets.token_urlsafe(32)
    with engine.begin() as db:
        db.execute(text("INSERT INTO accounts(id) VALUES(:account)"), {"account": account})
        db.execute(text("INSERT INTO sessions(token_digest,account_id,expires_at) VALUES(:digest,:account,now()+interval '12 hours')"), {
            "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
        })
    with tempfile.TemporaryDirectory() as directory:
        rows = history.geography(Path(directory) / "synthetic.osm")
        history.seed(engine, LocalObjectStore(), int(fixture["dataset_id"]), rows, [(account, token)], 5000, 0, 1000)
    backfill_activity_statistics(engine, account)
    fixture["capacity"] = {"id": account, "token": token, "activities": 5000, "gps_samples": 5_000_000}
    path.write_text(json.dumps(fixture))
    engine.dispose()
    print("Prepared the synthetic 5,000-activity owner; private credentials remain in the state volume.")


if __name__ == "__main__":
    main()
