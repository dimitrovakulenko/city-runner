"""Lease-fenced, atomic publication into the isolated analytics database."""

from __future__ import annotations

import time
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from backend.app.analytics_datasets import SNAPSHOT_SECONDS, read_snapshot
from backend.app.analytics_store import owner_key, provision_role, stage_snapshot
from backend.app.owner_guard import owner_lock
from backend.app.sync_status import _read_version


def register_account(db, account_id: str) -> dict:
    owner_lock(db, account_id)
    db.execute(text("""INSERT INTO analytics_accounts(account_id,owner_key,generation)
        VALUES (:account,:owner,:generation)
        ON CONFLICT DO NOTHING"""), {
        "account": account_id, "owner": owner_key(account_id), "generation": uuid.uuid4(),
    })
    return dict(db.execute(text("SELECT * FROM analytics_accounts WHERE account_id=:account"), {
        "account": account_id,
    }).mappings().one())


def refresh_projection(engine, store, account_id: str, secret: bytes) -> bool:
    """One bounded refresh. A cross-database commit gap remains unavailable."""
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    lease = uuid.uuid4()
    generation = uuid.uuid4()
    with engine.begin() as db:
        db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
        owner_lock(db, account_id)
        record = db.execute(text("""UPDATE analytics_accounts SET state='processing',lease_token=:lease,
            leased_until=clock_timestamp()+interval '60 seconds',last_error=NULL,updated_at=clock_timestamp()
            WHERE account_id=:account AND state NOT IN ('deleting','deleted')
              AND (leased_until IS NULL OR leased_until<clock_timestamp()) RETURNING *"""), {
            "account": account_id, "lease": lease,
        }).mappings().first()
        if record is None:
            return False
        record = dict(record)
    try:
        # Policy DDL must commit before staging: it otherwise holds table locks
        # that block every account's reads for the whole refresh.
        with store.begin() as setup:
            setup.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
            setup.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            provision_role(setup, record["owner_key"], secret)
        snapshot = read_snapshot(engine, account_id, record["selected_dataset_id"], secret, deadline=deadline)
        with store.connect() as target:
            transaction = target.begin()
            try:
                target.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
                target.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                target.execute(text("SELECT owner_key FROM control.owners WHERE owner_key=:owner FOR UPDATE"), {
                    "owner": record["owner_key"],
                }).one()
                stage_snapshot(target, record["owner_key"], snapshot, deadline)
                # Source writers take shared owner locks; this brief exclusive
                # fence covers only final comparison and the two commits.
                with engine.begin() as source:
                    source.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                    owner_lock(source, account_id, exclusive=True)
                    source.exec_driver_sql("SELECT singleton FROM sync_geography_revision WHERE singleton=true FOR SHARE")
                    current = source.execute(text("""SELECT * FROM analytics_accounts
                        WHERE account_id=:account AND lease_token=:lease AND state='processing'
                          AND leased_until>clock_timestamp() FOR UPDATE"""), {
                        "account": account_id, "lease": lease,
                    }).mappings().first()
                    if (current is None or current["generation"] != record["generation"]
                            or current["selected_dataset_id"] != record["selected_dataset_id"]
                            or _read_version(source, account_id, deadline) != snapshot.revision):
                        raise HTTPException(409, "Analytics source changed during refresh.")
                    target.execute(text("""UPDATE control.owners SET generation=:generation,
                        source_revision=:revision,enabled=true WHERE owner_key=:owner"""), {
                        "generation": generation, "revision": snapshot.revision, "owner": record["owner_key"],
                    })
                    if time.monotonic() >= deadline:
                        raise HTTPException(503, "Analytics publication exceeded its work limit.")
                    transaction.commit()
                    source.execute(text("""UPDATE analytics_accounts SET state='ready',generation=:generation,
                        source_revision=:revision,lease_token=NULL,leased_until=NULL,last_error=NULL,
                        available_at=clock_timestamp()+interval '5 seconds',updated_at=clock_timestamp()
                        WHERE account_id=:account AND lease_token=:lease"""), {
                        "generation": generation, "revision": snapshot.revision, "account": account_id, "lease": lease,
                    })
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
        return True
    except Exception as error:
        # Do not log SQL/data/provider values or revive an account deletion fence.
        code = "source_changed" if isinstance(error, HTTPException) and error.status_code == 409 else "refresh_failed"
        with engine.begin() as db:
            db.execute(text("""UPDATE analytics_accounts SET state='failed',lease_token=NULL,leased_until=NULL,
                last_error=:error,available_at=clock_timestamp()+interval '10 seconds',updated_at=clock_timestamp()
                WHERE account_id=:account AND lease_token=:lease AND state='processing'"""), {
                "account": account_id, "lease": lease, "error": code,
            })
        raise


def current_projection(db, store, account_id: str) -> dict | None:
    """Validate on every request and again before releasing a private result."""
    db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
    owner_lock(db, account_id, exclusive=True)
    db.exec_driver_sql("SELECT singleton FROM sync_geography_revision WHERE singleton=true FOR SHARE")
    row = db.execute(text("SELECT * FROM analytics_accounts WHERE account_id=:account"), {
        "account": account_id,
    }).mappings().first()
    if row is None or row["state"] != "ready":
        return None
    if row["source_revision"] != _read_version(db, account_id, time.monotonic() + 1.5):
        return None
    with store.connect() as target:
        target.exec_driver_sql("SET statement_timeout='1500ms'")
        external = target.execute(text("""SELECT 1 FROM control.owners WHERE owner_key=:owner
            AND enabled AND NOT fenced AND generation=:generation AND source_revision=:revision"""), {
            "owner": row["owner_key"], "generation": row["generation"], "revision": row["source_revision"],
        }).first()
    return dict(row) if external else None
