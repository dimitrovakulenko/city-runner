"""Durable PostgreSQL jobs with lease-token fencing."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Connection, Engine, text

MAX_LEASE_SECONDS = 3600
MAX_BACKOFF_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


@contextmanager
def _transaction(bind: Engine | Connection):
    if isinstance(bind, Engine):
        with bind.begin() as db:
            yield db
    else:
        yield bind


def _lease_seconds(value: int) -> int:
    if not 1 <= value <= MAX_LEASE_SECONDS:
        raise ValueError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")
    return value


def enqueue(
    engine: Engine | Connection,
    *,
    account_id: str,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    priority: int = 100,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Insert idempotently; a Connection uses the caller's transaction."""
    if not account_id or not kind or not dedupe_key:
        raise ValueError("account_id, kind and dedupe_key are required")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    if not isinstance(priority, int):
        raise ValueError("priority must be an integer")
    with _transaction(engine) as db:
        row = db.execute(text("""INSERT INTO jobs
            (account_id, kind, dedupe_key, payload, priority, max_attempts)
            VALUES (:account_id, :kind, :dedupe_key, CAST(:payload AS jsonb), :priority, :max_attempts)
            ON CONFLICT (account_id, kind, dedupe_key) DO NOTHING
            RETURNING *"""), {
                "account_id": account_id,
                "kind": kind,
                "dedupe_key": dedupe_key,
                "payload": json.dumps(payload),
                "priority": priority,
                "max_attempts": max_attempts,
            }).mappings().first()
        if row is None:
            row = db.execute(text("""SELECT * FROM jobs
                WHERE account_id=:account_id AND kind=:kind AND dedupe_key=:dedupe_key"""), {
                    "account_id": account_id,
                    "kind": kind,
                    "dedupe_key": dedupe_key,
                }).mappings().one()
        return dict(row)


def claim(engine: Engine, *, lease_seconds: int = 60, kind: str | Sequence[str] | None = None) -> dict[str, Any] | None:
    lease_seconds = _lease_seconds(lease_seconds)
    token = uuid.uuid4().hex
    with engine.begin() as db:
        db.execute(text("""WITH expired AS (
                SELECT id FROM jobs WHERE status='running' AND leased_until <= clock_timestamp()
                ORDER BY leased_until, id FOR UPDATE SKIP LOCKED LIMIT 100
            )
            UPDATE jobs AS job SET
                status=CASE WHEN job.attempts >= job.max_attempts THEN 'failed' ELSE 'queued' END,
                available_at=CASE WHEN job.attempts >= job.max_attempts THEN job.available_at ELSE clock_timestamp() END,
                last_error=CASE WHEN job.attempts >= job.max_attempts THEN 'lease_expired' ELSE job.last_error END,
                lease_token=NULL, leased_until=NULL, updated_at=clock_timestamp()
            FROM expired WHERE job.id=expired.id"""))
        params: dict[str, Any] = {"token": token, "lease_seconds": lease_seconds}
        if isinstance(kind, str):
            where_kind = "AND kind=:kind"
            params["kind"] = kind
        elif kind:
            names = [f"kind_{index}" for index in range(len(kind))]
            where_kind = "AND kind IN (" + ",".join(f":{name}" for name in names) + ")"
            params.update(zip(names, kind))
        elif kind is not None:
            return None
        else:
            where_kind = ""
        row = db.execute(text(f"""WITH candidate AS (
                SELECT id FROM jobs
                WHERE status='queued' AND available_at <= clock_timestamp()
                  AND attempts < max_attempts {where_kind}
                ORDER BY priority DESC, available_at, id
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE jobs AS job SET status='running', attempts=job.attempts+1,
                lease_token=:token,
                leased_until=clock_timestamp() + make_interval(secs => :lease_seconds),
                updated_at=clock_timestamp()
            FROM candidate WHERE job.id=candidate.id
            RETURNING job.*"""), {
            **params,
        }).mappings().first()
        return dict(row) if row is not None else None


def complete(engine: Engine | Connection, *, job_id: int, lease_token: str) -> bool:
    with _transaction(engine) as db:
        result = db.execute(text("""UPDATE jobs SET status='succeeded', lease_token=NULL,
            leased_until=NULL, last_error=NULL, updated_at=clock_timestamp()
            WHERE id=:id AND status='running' AND lease_token=:token
              AND leased_until > clock_timestamp()"""), {"id": job_id, "token": lease_token})
        return result.rowcount == 1


def extend_lease(engine: Engine | Connection, *, job_id: int, lease_token: str, lease_seconds: int = 60) -> bool:
    lease_seconds = _lease_seconds(lease_seconds)
    with _transaction(engine) as db:
        result = db.execute(text("""UPDATE jobs SET
            leased_until=GREATEST(leased_until, clock_timestamp() + make_interval(secs => :lease_seconds)),
            updated_at=clock_timestamp()
            WHERE id=:id AND status='running' AND lease_token=:token
              AND leased_until > clock_timestamp()"""), {
                "id": job_id,
                "token": lease_token,
                "lease_seconds": lease_seconds,
            })
        return result.rowcount == 1


def fail(
    engine: Engine | Connection,
    *,
    job_id: int,
    lease_token: str,
    error_code: str = "handler_error",
    permanent: bool = False,
) -> bool:
    if not _ERROR_CODE.fullmatch(error_code):
        error_code = "handler_error"
    with _transaction(engine) as db:
        result = db.execute(text("""UPDATE jobs SET
            status=CASE WHEN :permanent OR attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
            available_at=CASE WHEN :permanent OR attempts >= max_attempts THEN available_at
                ELSE clock_timestamp() + make_interval(secs => LEAST(power(2, LEAST(attempts - 1, 16))::int, :max_backoff)) END,
            lease_token=NULL, leased_until=NULL, last_error=:error_code,
            updated_at=clock_timestamp()
            WHERE id=:id AND status='running' AND lease_token=:token
              AND leased_until > clock_timestamp()"""), {
                "id": job_id,
                "token": lease_token,
                "permanent": permanent,
                "error_code": error_code,
                "max_backoff": MAX_BACKOFF_SECONDS,
            })
        return result.rowcount == 1


def cancel(engine: Engine | Connection, *, job_id: int, account_id: str) -> bool:
    with _transaction(engine) as db:
        result = db.execute(text("""UPDATE jobs SET status='cancelled', lease_token=NULL,
            leased_until=NULL, updated_at=clock_timestamp()
            WHERE id=:id AND account_id=:account_id AND status IN ('queued','running')"""), {
                "id": job_id,
                "account_id": account_id,
            })
        return result.rowcount == 1
