"""Owner-only activity deletion and version-scoped manual street labels."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from backend.app.coverage import rebuild_account_coverage
from backend.app.jobs import enqueue
from backend.app.storage import LocalObjectStore

MAX_BIGINT = 9_223_372_036_854_775_807


class ManualCompletionBody(BaseModel):
    reason: str = Field(max_length=2000)


@contextmanager
def _read_committed_transaction(engine: Engine):
    with engine.connect().execution_options(isolation_level="READ COMMITTED") as db:
        with db.begin():
            yield db


def create_corrections_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    @router.delete("/api/activities/{activity_id}", status_code=204)
    def delete_activity_route(
        activity_id: int = Path(ge=1, le=MAX_BIGINT),
        account_id: str = Depends(current_user),
    ):
        if not delete_activity(engine, account_id, activity_id):
            raise HTTPException(status_code=404, detail="Activity not found.")
        return Response(status_code=204)

    @router.put("/api/streets/{street_id}/manual-completion", status_code=204)
    def set_manual_completion(
        body: ManualCompletionBody,
        street_id: int = Path(ge=1, le=MAX_BIGINT),
        dataset_id: int = Query(ge=1, le=MAX_BIGINT),
        account_id: str = Depends(current_user),
    ):
        reason = body.reason.strip()
        if not reason or len(reason) > 500:
            raise HTTPException(status_code=422, detail="Reason must contain 1 to 500 nonblank characters.")
        with engine.begin() as db:
            _writable_street(db, dataset_id, street_id)
            changed = db.execute(text("""INSERT INTO manual_street_completions
                    (account_id,dataset_id,street_id,reason)
                VALUES (:account,:dataset,:street,:reason)
                ON CONFLICT (account_id,dataset_id,street_id) DO UPDATE SET
                    reason=EXCLUDED.reason,updated_at=clock_timestamp()
                WHERE manual_street_completions.reason IS DISTINCT FROM EXCLUDED.reason"""), {
                "account": account_id, "dataset": dataset_id,
                "street": street_id, "reason": reason,
            })
            if changed.rowcount:
                _bump_revision(db, account_id, dataset_id)
        return Response(status_code=204)

    @router.delete("/api/streets/{street_id}/manual-completion", status_code=204)
    def clear_manual_completion(
        street_id: int = Path(ge=1, le=MAX_BIGINT),
        dataset_id: int = Query(ge=1, le=MAX_BIGINT),
        account_id: str = Depends(current_user),
    ):
        with engine.begin() as db:
            _writable_street(db, dataset_id, street_id)
            removed = db.execute(text("""DELETE FROM manual_street_completions
                WHERE account_id=:account AND dataset_id=:dataset AND street_id=:street"""), {
                "account": account_id, "dataset": dataset_id, "street": street_id,
            }).rowcount
            if removed:
                _bump_revision(db, account_id, dataset_id)
        return Response(status_code=204)

    return router


def _writable_street(db, dataset_id: int, street_id: int) -> None:
    row = db.execute(text("""SELECT dataset.status,dataset.coverage_mode,street.eligible_node_count
        FROM map_datasets dataset JOIN streets street ON street.dataset_id=dataset.id
        WHERE dataset.id=:dataset AND street.id=:street FOR SHARE OF dataset,street"""), {
        "dataset": dataset_id, "street": street_id,
    }).mappings().first()
    if row is None or row["eligible_node_count"] <= 0:
        raise HTTPException(status_code=404, detail="Street not found.")
    if row["status"] != "active" or row["coverage_mode"] != "complete":
        raise HTTPException(status_code=409, detail="Manual completion requires active complete geography.")


def _bump_revision(db, account_id: str, dataset_id: int) -> None:
    db.execute(text("""INSERT INTO account_dataset_coverage(account_id,dataset_id)
        VALUES (:account,:dataset) ON CONFLICT DO NOTHING"""), {
        "account": account_id, "dataset": dataset_id,
    })
    db.execute(text("""UPDATE account_dataset_coverage SET progress_revision=progress_revision+1,
        updated_at=clock_timestamp() WHERE account_id=:account AND dataset_id=:dataset"""), {
        "account": account_id, "dataset": dataset_id,
    })


def delete_activity(engine: Engine, account_id: str, activity_id: int) -> bool:
    """Delete all owner source support atomically and queue durable object cleanup."""
    for _ in range(3):
        try:
            return _delete_activity_once(engine, account_id, activity_id)
        except _RetryDelete:
            continue
    raise HTTPException(status_code=503, detail="Activity changed during deletion; retry the request.")


class _RetryDelete(Exception):
    pass


def _delete_activity_once(engine: Engine, account_id: str, activity_id: int) -> bool:
    with _read_committed_transaction(engine) as db:
        # Discover IDs without locking source rows so the shared lock order starts with jobs.
        sources = db.execute(text("""SELECT id,revision FROM activity_sources
            WHERE account_id=:account AND activity_id=:activity ORDER BY id"""), {
            "account": account_id, "activity": activity_id,
        }).mappings().all()
        source_versions = [(int(row["id"]), int(row["revision"])) for row in sources]
        source_ids = [source_id for source_id, _revision in source_versions]
        jobs = []
        if source_ids:
            jobs = db.execute(text("""SELECT id FROM jobs
                WHERE account_id=:account AND (
                    (kind='process_upload' AND dedupe_key=ANY(:upload_keys)) OR
                    (kind='match_coverage' AND (payload->>'source_id'=ANY(:source_ids) OR id IN (
                        SELECT job_id FROM coverage_source_runs WHERE account_id=:account
                          AND source_id=ANY(:numeric_source_ids)
                    )))
                ) ORDER BY id FOR UPDATE"""), {
                "account": account_id,
                "upload_keys": [f"upload:{row['id']}:r{row['revision']}" for row in sources],
                "source_ids": [str(value) for value in source_ids],
                "numeric_source_ids": source_ids,
            }).scalars().all()
        # Always rescan and lock even when the initial source set was empty.
        # The identical owner/activity predicate also verifies source association.
        locked_sources = db.execute(text("""SELECT id,revision,private_object_key FROM activity_sources
            WHERE account_id=:account AND activity_id=:activity ORDER BY id FOR UPDATE"""), {
            "account": account_id, "activity": activity_id,
        }).mappings().all()
        locked_source_ids = [int(row["id"]) for row in locked_sources]
        activity = db.execute(text("""SELECT id FROM activities
            WHERE id=:activity AND user_id=:account FOR UPDATE"""), {
            "activity": activity_id, "account": account_id,
        }).first()
        if activity is None:
            return False

        locked_source_versions = [(int(row["id"]), int(row["revision"])) for row in locked_sources]
        if locked_source_versions != source_versions:
            raise _RetryDelete()
        fresh_source_versions = db.execute(text("""SELECT id,revision FROM activity_sources
            WHERE account_id=:account AND activity_id=:activity ORDER BY id"""), {
            "account": account_id, "activity": activity_id,
        }).all()
        if [(int(row[0]), int(row[1])) for row in fresh_source_versions] != locked_source_versions:
            raise _RetryDelete()
        if source_ids:
            related_jobs = set(db.execute(text("""SELECT id FROM jobs
                WHERE account_id=:account AND (
                    (kind='process_upload' AND dedupe_key=ANY(:upload_keys)) OR
                    (kind='match_coverage' AND (payload->>'source_id'=ANY(:source_ids) OR id IN (
                        SELECT job_id FROM coverage_source_runs WHERE account_id=:account
                          AND source_id=ANY(:numeric_source_ids)
                    )))
                ) ORDER BY id"""), {
                "account": account_id,
                "upload_keys": [f"upload:{row['id']}:r{row['revision']}" for row in locked_sources],
                "source_ids": [str(value) for value in source_ids],
                "numeric_source_ids": source_ids,
            }).scalars().all())
            if not related_jobs.issubset(set(jobs)):
                raise _RetryDelete()
        affected = set()
        if source_ids:
            affected.update(db.execute(text("""SELECT DISTINCT dataset_id FROM coverage_source_runs
                WHERE account_id=:account AND source_id=ANY(:ids)
                UNION SELECT DISTINCT dataset_id FROM source_node_contributions
                WHERE account_id=:account AND source_id=ANY(:ids)"""), {
                "account": account_id, "ids": source_ids,
            }).scalars().all())
        if jobs:
            db.execute(text("""UPDATE jobs SET status='cancelled',lease_token=NULL,leased_until=NULL,
                last_error='activity_deleted',updated_at=clock_timestamp()
                WHERE id=ANY(:ids) AND account_id=:account AND status IN ('queued','running')"""), {
                "ids": jobs, "account": account_id,
            })
        if affected:
            ordered_datasets = sorted(int(value) for value in affected)
            db.execute(text("""INSERT INTO account_dataset_coverage(account_id,dataset_id)
                SELECT :account,id FROM map_datasets WHERE id=ANY(:datasets)
                ON CONFLICT DO NOTHING"""), {"account": account_id, "datasets": ordered_datasets})
            db.execute(text("""SELECT dataset_id FROM account_dataset_coverage
                WHERE account_id=:account AND dataset_id=ANY(:datasets)
                ORDER BY dataset_id FOR UPDATE"""), {
                "account": account_id, "datasets": ordered_datasets,
            }).all()
        for source in locked_sources:
            object_key = source["private_object_key"]
            if object_key:
                enqueue(db, account_id=account_id, kind="delete_private_object",
                        dedupe_key=f"activity:{activity_id}:source:{source['id']}:r{source['revision']}",
                        payload={"object_key": object_key}, priority=100)
        if source_ids:
            db.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=ANY(:ids)"), {
                "account": account_id, "ids": source_ids,
            })
        db.execute(text("DELETE FROM activities WHERE id=:activity AND user_id=:account"), {
            "activity": activity_id, "account": account_id,
        })
        for dataset_id in sorted(int(value) for value in affected):
            rebuild_account_coverage(db, account_id, dataset_id)
    return True


def process_private_object_cleanup(job: dict, store: LocalObjectStore | None = None) -> None:
    payload = job.get("payload")
    key = payload.get("object_key") if isinstance(payload, dict) else None
    if not isinstance(key, str):
        raise ValueError("invalid_cleanup_payload")
    (store or LocalObjectStore()).delete(key)
