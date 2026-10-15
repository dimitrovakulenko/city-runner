"""Restore/audit helpers, restricted to generated synthetic acceptance DBs."""

import json
import os
from pathlib import Path
import re
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.account_data import verify_account_deletions
from backend.app.analytics_api import private_call
from backend.app.analytics_config import AnalyticsConfig
from backend.app.analytics_store import initialize_store, owner_key, provision_role, role_name
from backend.app.analytics_worker import recover_owner
from backend.app.storage import LocalObjectStore


def main():
    url = make_url(os.environ["DATABASE_URL"])
    if re.fullmatch(r"city_runner_analytics_acceptance_[0-9a-f]{12}", url.database or "") is None:
        raise RuntimeError("A generated disposable acceptance database is required.")
    fixture = json.loads(Path("/var/lib/city-runner/acceptance.json").read_text())
    engine, config = create_engine(url), AnalyticsConfig.from_env()
    account = fixture.get("lifecycle", fixture["accounts"]["B"])["id"]
    owner = owner_key(account)
    mode = sys.argv[1]
    if mode == "slow-fixture":
        with config.store.begin() as db:
            db.exec_driver_sql("CREATE OR REPLACE FUNCTION analytics.acceptance_slow(seconds double precision) RETURNS integer LANGUAGE sql AS $$ SELECT 1 FROM pg_sleep(least(seconds,30)) $$")
        print(json.dumps({"prepared": True}))
    elif mode == "restore-roles":
        # Traffic is stopped. Recreate deterministic credentials before restoring
        # old table ACLs/policies, including the role dropped by account deletion.
        initialize_store(config.store, config.secret)
        with engine.connect() as db:
            owners = db.execute(text("SELECT owner_key FROM analytics_accounts")).scalars().all()
        with config.store.begin() as db:
            for key in owners:
                provision_role(db, key, config.secret)
        print(json.dumps({"restored_role_count": len(owners)}))
    elif mode == "audit-restored":
        with engine.connect() as db:
            primary = db.execute(text("SELECT 1 FROM accounts WHERE id=:account"), {"account": account}).first() is not None
            keys = db.execute(text("SELECT private_object_key FROM activity_sources WHERE account_id=:account"), {"account": account}).scalars().all()
        with config.store.connect() as db:
            projected = db.execute(text("SELECT count(*) FROM analytics.activities WHERE owner_key=:owner"), {"owner": owner}).scalar_one()
            role = db.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role_name(owner)}).first() is not None
        store = LocalObjectStore()
        checks = {"primary_resurrected": primary, "projection_resurrected": projected == 1, "role_resurrected": role,
            "private_metadata_resurrected": len(private_call(config, owner, "state")["boards"]) >= 3,
            "original_resurrected": bool(keys) and all(store.read(key, owner_id=account) for key in keys)}
        print(json.dumps(checks))
        if not all(checks.values()):
            raise SystemExit(1)
    elif mode == "audit-deleted":
        verified = verify_account_deletions(engine)
        print(json.dumps({"all_tombstones_verified": verified}))
        if not verified:
            raise SystemExit(1)
    elif mode in {"activity", "recover"}:
        key = owner_key(fixture.get("capacity", fixture["accounts"]["B"])["id"])
        if mode == "recover":
            recover_owner(engine, config, key)
            print(json.dumps({"recovered": True}))
            return
        with config.store.connect() as db:
            active = db.execute(text("SELECT count(*) FROM pg_stat_activity WHERE usename=:role AND state='active'"), {"role": role_name(key)}).scalar_one()
            fence = db.execute(text("SELECT fenced FROM control.owners WHERE owner_key=:owner"), {"owner": key}).scalar_one()
            pending = db.execute(text("SELECT count(*) FROM control.requests WHERE owner_key=:owner"), {"owner": key}).scalar_one()
        with engine.connect() as db:
            state = db.execute(text("SELECT state FROM analytics_accounts WHERE owner_key=:owner"), {"owner": key}).scalar_one()
        print(json.dumps({"active": active, "fenced": fence, "pending": pending, "state": state}))
    else:
        raise RuntimeError("Unknown acceptance recovery operation.")
    engine.dispose(); config.store.dispose()


if __name__ == "__main__":
    main()
