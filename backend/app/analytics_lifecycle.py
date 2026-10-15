"""Durable analytics export/deletion, including deletion-journal restore replay."""

import time
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from backend.app.analytics_api import private_call
from backend.app.analytics_config import AnalyticsConfig
from backend.app.analytics_store import TABLES, owner_key, remove_owner, role_name


def export_definitions(db, account, config=None):
    row = db.execute(text("""SELECT owner_key,selected_dataset_id,default_dashboard_id
        FROM analytics_accounts WHERE account_id=:account"""), {"account": account}).mappings().first()
    if row is None:
        return None
    config = config or AnalyticsConfig.from_env()
    if config is None:
        raise HTTPException(503, "Analytics export requires its configured metadata service.")
    return {"owner_mapping": dict(row), "definitions": private_call(config, row["owner_key"], "export")}


def fence_deletion(db, account, *, force=False, configured=False):
    if configured:
        db.execute(text("""INSERT INTO analytics_accounts(account_id,owner_key,generation,state)
            VALUES (:account,:owner,:generation,'deleting') ON CONFLICT DO NOTHING"""), {
            "account": account, "owner": owner_key(account), "generation": uuid.uuid4(),
        })
    db.execute(text("""UPDATE analytics_accounts SET state='deleting',generation=:generation,
        lease_token=NULL,leased_until=NULL,available_at=clock_timestamp(),updated_at=clock_timestamp()
        WHERE account_id=:account AND (state NOT IN ('deleting','deleted') OR :force)"""), {
        "account": account, "generation": uuid.uuid4(), "force": force,
    })


def complete_receipts(db):
    db.execute(text("""UPDATE account_deletions receipt SET status='complete',completed_at=clock_timestamp()
        WHERE status='cleanup-pending' AND NOT EXISTS (
          SELECT 1 FROM account_deletion_outbox item WHERE item.deletion_id=receipt.id AND item.status<>'complete')
        AND NOT EXISTS (SELECT 1 FROM analytics_accounts owner WHERE owner.account_id=receipt.account_id AND owner.state<>'deleted')"""))


def verify_analytics_deleted(account, config):
    owner = owner_key(account)
    with config.store.connect() as target:
        target.exec_driver_sql("SET statement_timeout='1500ms'")
        if target.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role_name(owner)}).first():
            return False
        if target.execute(text("SELECT 1 FROM control.requests WHERE owner_key=:owner"), {"owner": owner}).first():
            return False
        for table in TABLES.values():
            if target.execute(text(f"SELECT 1 FROM analytics.{table.name} WHERE owner_key=:owner LIMIT 1"), {"owner": owner}).first():
                return False
    return private_call(config, owner, "verify-deleted")["deleted"]


def process_analytics_cleanup(engine, config=None):
    config = config or AnalyticsConfig.from_env()
    if config is None:
        return False
    lease = uuid.uuid4()
    with engine.begin() as db:
        row = db.execute(text("""WITH candidate AS (
            SELECT account_id FROM analytics_accounts WHERE state='deleting'
              AND available_at<=clock_timestamp() AND (leased_until IS NULL OR leased_until<clock_timestamp())
            ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE analytics_accounts owner SET lease_token=:lease,leased_until=clock_timestamp()+interval '60 seconds'
            FROM candidate WHERE owner.account_id=candidate.account_id RETURNING owner.*"""), {"lease": lease}).mappings().first()
        if row is None:
            return False
        record = dict(row)
    try:
        # The app account/journal fence already prevents fresh admissions. First
        # terminate verified owner work and remove projected data/credentials.
        with config.store.begin() as target:
            target.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            target.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
            target.execute(text("UPDATE control.owners SET fenced=true,enabled=false WHERE owner_key=:owner"), {"owner": record["owner_key"]})
            remove_owner(target, record["owner_key"])
            target.execute(text("DELETE FROM control.requests WHERE owner_key=:owner"), {"owner": record["owner_key"]})
        deadline = time.monotonic() + 25
        complete = False
        while time.monotonic() < deadline:
            if private_call(config, record["owner_key"], "delete")["complete"]:
                complete = verify_analytics_deleted(record["account_id"], config)
                break
        with engine.begin() as db:
            db.execute(text("""UPDATE analytics_accounts SET state=:state,lease_token=NULL,leased_until=NULL,
                selected_dataset_id=NULL,default_dashboard_id=NULL,source_revision=NULL,last_error=NULL,
                available_at=clock_timestamp()+interval '1 second',updated_at=clock_timestamp()
                WHERE account_id=:account AND state='deleting' AND lease_token=:lease
                  AND leased_until>clock_timestamp()"""), {
                "state": "deleted" if complete else "deleting", "account": record["account_id"], "lease": lease,
            })
            complete_receipts(db)
    except Exception:
        with engine.begin() as db:
            db.execute(text("""UPDATE analytics_accounts SET lease_token=NULL,leased_until=NULL,last_error='cleanup_failed',
                available_at=clock_timestamp()+interval '10 seconds',updated_at=clock_timestamp()
                WHERE account_id=:account AND state='deleting' AND lease_token=:lease"""), {
                "account": record["account_id"], "lease": lease,
            })
    return True
