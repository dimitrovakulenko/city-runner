"""One globally admitted analytics refresh/cleanup worker; no import job changes."""

import argparse
import logging
import os
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import create_engine, text

from backend.app.account_data import replay_account_deletions
from backend.app.analytics_api import private_call
from backend.app.analytics_config import AnalyticsConfig
from backend.app.analytics_lifecycle import process_analytics_cleanup
from backend.app.analytics_projection import current_projection, refresh_projection
from backend.app.analytics_store import initialize_store, provision_role, role_name
from backend.app.owner_guard import owner_lock

logger = logging.getLogger(__name__)
LOCK = "city-runner:analytics-worker"


def run_once(engine, config):
    with engine.connect() as admission:
        acquired = admission.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"), {"key": LOCK}).scalar_one()
        admission.commit()
        if not acquired:
            return False
        try:
            replay_account_deletions(engine, require_ledger=False)
            if process_analytics_cleanup(engine, config):
                return True
            with engine.begin() as db:
                row = db.execute(text("""SELECT owner.* FROM analytics_accounts owner JOIN accounts account
                    ON account.id=owner.account_id WHERE owner.state NOT IN ('deleting','deleted')
                      AND owner.available_at<=clock_timestamp()
                      AND (owner.leased_until IS NULL OR owner.leased_until<clock_timestamp())
                    ORDER BY owner.available_at LIMIT 1""")).mappings().first()
                if row is None:
                    return False
                record = dict(row)
                db.execute(text("""UPDATE analytics_accounts SET available_at=clock_timestamp()+interval '5 seconds'
                    WHERE account_id=:account"""), {"account": record["account_id"]})
            account, owner = record["account_id"], record["owner_key"]
            with config.store.connect() as target:
                fenced = target.execute(text("SELECT fenced FROM control.owners WHERE owner_key=:owner"), {"owner": owner}).scalar_one_or_none()
            if fenced:
                with engine.begin() as db:
                    db.execute(text("""UPDATE analytics_accounts SET last_error='boundary_failed',state='failed',
                        available_at=clock_timestamp()+interval '30 seconds' WHERE account_id=:account
                        AND state NOT IN ('deleting','deleted')"""), {"account": account})
                return True
            with engine.begin() as db:
                existing = current_projection(db, config.store, account)
            if existing:
                try:
                    private_call(config, owner, "state")
                    return True
                except HTTPException:
                    pass
            # Shared source owner fence prevents late identity creation after
            # deletion; source writers remain free to use their shared locks.
            with engine.begin() as db:
                db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                owner_lock(db, account)
                with config.store.begin() as setup:
                    setup.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                    provision_role(setup, owner, config.secret)
                private_call(config, owner, "provision")
                pending = db.execute(text("""SELECT 1 FROM jobs WHERE account_id=:account
                    AND kind IN ('process_upload','match_coverage') AND status IN ('queued','running') LIMIT 1"""), {"account": account}).first()
            if not pending:
                refresh_projection(engine, config.store, account, config.secret)
            return True
        except Exception:
            # Exception messages/SQL parameters may contain private source data.
            logger.warning("Analytics work deferred after a bounded failure.")
            return False
        finally:
            try:
                admission.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"), {"key": LOCK})
                admission.commit()
            except Exception:
                admission.invalidate()


def recover_owner(engine, config, owner):
    role = role_name(owner)
    with engine.begin() as db:
        db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
        account = db.execute(text("SELECT account_id FROM analytics_accounts WHERE owner_key=:owner"), {"owner": owner}).scalar_one()
        owner_lock(db, account, exclusive=True)
        with config.store.begin() as target:
            target.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            target.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
            target.execute(text("SELECT pg_terminate_backend(pid,500) FROM pg_stat_activity WHERE usename=:role"), {"role": role}).all()
            target.exec_driver_sql("SELECT pg_stat_clear_snapshot()")
            if target.execute(text("SELECT 1 FROM pg_stat_activity WHERE usename=:role"), {"role": role}).first():
                raise RuntimeError("Analytics owner still has live database work.")
            target.execute(text("DELETE FROM control.requests WHERE owner_key=:owner"), {"owner": owner})
            target.execute(text("UPDATE control.owners SET fenced=false,enabled=false,generation=NULL WHERE owner_key=:owner"), {"owner": owner})
        db.execute(text("""UPDATE analytics_accounts SET state='pending',generation=:generation,
            source_revision=NULL,lease_token=NULL,leased_until=NULL,available_at=clock_timestamp(),last_error=NULL
            WHERE account_id=:account"""), {"account": account, "generation": uuid.uuid4()})


def prepare_restore(engine, config):
    # Operator-only, with all traffic stopped: old policies/ACLs refer to roles
    # that a later deletion may have dropped. Journal replay removes them again.
    initialize_store(config.store, config.secret)
    with engine.connect() as db:
        owners = db.execute(text("SELECT owner_key FROM analytics_accounts")).scalars().all()
    with config.store.begin() as db:
        for owner in owners:
            provision_role(db, owner, config.secret)


def main():
    parser = argparse.ArgumentParser(description="Run bounded personal analytics maintenance")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--initialize-store", action="store_true")
    parser.add_argument("--recover-owner")
    parser.add_argument("--prepare-restore", action="store_true", help="Recreate restored registry roles before restoring isolated DB policies; traffic must remain stopped")
    args = parser.parse_args()
    config = AnalyticsConfig.from_env()
    if config is None:
        parser.error("Analytics requires explicit service configuration.")
    if args.initialize_store:
        try:
            initialize_store(config.store, config.secret)
        except Exception:
            logger.error("Analytics store initialization failed.")
            raise SystemExit(1) from None
        finally:
            config.store.dispose()
        return
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True, pool_size=4, max_overflow=0,
                           pool_timeout=1, connect_args={"connect_timeout": 1})
    try:
        if args.prepare_restore:
            prepare_restore(engine, config)
        elif args.recover_owner:
            recover_owner(engine, config, args.recover_owner)
        elif args.once:
            run_once(engine, config)
        else:
            while True:
                if not run_once(engine, config):
                    time.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.error("Analytics maintenance failed.")
        raise SystemExit(1) from None
    finally:
        engine.dispose()
        config.store.dispose()


if __name__ == "__main__":
    main()
