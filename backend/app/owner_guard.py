"""Account-scoped fencing shared by owner writes, exports and deletion."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from backend.app.account_deletion_journal import JournalError, read_records


def owner_lock(db: Connection, account_id: str, *, exclusive: bool = False,
               require_account: bool = True, allow_deleted: bool = False) -> None:
    """Acquire the account lock before any owner row/entity lock."""
    if db.dialect.name == "postgresql":
        function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        db.execute(text(f"SELECT {function}(hashtextextended(:key, 0))"), {
            "key": f"city-runner:owner:{account_id}",
        })
    if not allow_deleted:
        ensure_owner_allowed(db, account_id, require_account=require_account)


def account_lock_key(account_id: str) -> str:
    return f"city-runner:owner:{account_id}"


def ensure_owner_allowed(db: Connection, account_id: str, *, require_account: bool = True) -> None:
    try:
        tombstones = {record["account_id"] for record in read_records(required=False)}
    except JournalError as error:
        raise HTTPException(status_code=503, detail="Account data protection is unavailable.") from error
    if account_id in tombstones:
        raise HTTPException(status_code=410, detail="This account has been deleted.")
    if db.dialect.name == "postgresql":
        receipt = db.execute(text("SELECT 1 FROM account_deletions WHERE account_id=:id"), {
            "id": account_id,
        }).first()
        if receipt:
            raise HTTPException(status_code=410, detail="This account has been deleted.")
        if require_account and db.execute(text("SELECT 1 FROM accounts WHERE id=:id"), {
            "id": account_id,
        }).first() is None:
            raise HTTPException(status_code=401, detail="Unauthenticated.")


@contextmanager
def exclusive_owner_snapshot(engine: Engine, account_id: str):
    """Hold a session advisory lock without creating the data snapshot."""
    with engine.connect() as lock_connection:
        session_lock_acquired = False
        try:
            if lock_connection.dialect.name == "postgresql":
                lock_connection.execute(text("SET lock_timeout = '120s'"))
                try:
                    lock_connection.execute(text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {
                        "key": f"city-runner:owner:{account_id}",
                    })
                    session_lock_acquired = True
                    lock_connection.commit()
                    lock_connection.execution_options(isolation_level="REPEATABLE READ")
                except BaseException:
                    # A failed execute/commit can leave server-side lock state uncertain.
                    lock_connection.invalidate()
                    session_lock_acquired = False
                    raise
            with lock_connection.begin():
                if lock_connection.dialect.name == "postgresql":
                    lock_connection.execute(text("SET LOCAL statement_timeout = '120s'"))
                    lock_connection.execute(text("SET LOCAL lock_timeout = '120s'"))
                ensure_owner_allowed(lock_connection, account_id)
                yield lock_connection
        finally:
            if session_lock_acquired:
                try:
                    lock_connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {
                        "key": f"city-runner:owner:{account_id}",
                    }).scalar_one()
                    lock_connection.commit()
                except BaseException:
                    # Returning this connection to the pool could strand the lock forever.
                    lock_connection.invalidate()
                    raise
