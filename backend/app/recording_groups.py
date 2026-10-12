"""Exact file-recording grouping and source-level removal."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy import Engine, text

from backend.app.coverage import rebuild_account_coverage, _refresh_activity_processing
from backend.app.jobs import enqueue
from backend.app.owner_guard import owner_lock
from backend.app.fit import parse_fit
from backend.app.gpx import GpxError, parse_gpx
from backend.app.recording_identity import (
    RECORDING_IDENTITY_VERSION,
    RecordingIdentity,
    canonical_recording,
)


MIN_ACTIVITY_ID = -(1 << 63)
MAX_ID = (1 << 63) - 1


class ActivitySourceResponse(BaseModel):
    id: str
    source_kind: Literal["gpx", "fit"]
    created_at: datetime


class ActivitySourcePage(BaseModel):
    activity_id: str
    items: list[ActivitySourceResponse]
    page: int
    page_size: int
    total: int


@contextmanager
def _transaction(engine: Engine):
    with engine.connect().execution_options(isolation_level="READ COMMITTED") as db:
        with db.begin():
            yield db


def activity_recording_identity(row) -> RecordingIdentity | None:
    return canonical_recording(row["tracks"], row["timestamps"], row["activity_type"])


def stored_or_legacy_identity(row) -> RecordingIdentity | None:
    fingerprint = row.get("recording_fingerprint") if hasattr(row, "get") else None
    version = row.get("recording_fingerprint_version", 0) if hasattr(row, "get") else 0
    if isinstance(fingerprint, str) and len(fingerprint) == 64 and version == RECORDING_IDENTITY_VERSION:
        canonical = activity_recording_identity(row)
        if canonical is not None and canonical.fingerprint == fingerprint:
            return canonical
        # The persisted fingerprint was computed from this versioned exact value.
        return RecordingIdentity(fingerprint, version, str(row["activity_type"]), (), ())
    return activity_recording_identity(row)


def record_identity_deletion(db, account_id: str, identity: RecordingIdentity | None) -> None:
    if identity is None or len(identity.fingerprint) != 64:
        return
    db.execute(text("""INSERT INTO recording_identity_deletions
        (account_id,fingerprint,fingerprint_version)
        VALUES (:account,:fingerprint,:version)
        ON CONFLICT (account_id,fingerprint,fingerprint_version)
        DO UPDATE SET deleted_at=GREATEST(recording_identity_deletions.deleted_at,clock_timestamp())"""), {
        "account": account_id, "fingerprint": identity.fingerprint, "version": identity.version,
    })


def has_predelete_pending_import(db, account_id: str, source_id: int, source_kind: str,
                                content: bytes) -> bool:
    """Fence a fresh duplicate while an older accepted source awaits its watermark check."""
    source = db.execute(text("""SELECT source.created_at,source.activity_id
        FROM activity_sources source
        WHERE source.id=:source AND source.account_id=:account"""), {
        "source": source_id, "account": account_id,
    }).mappings().first()
    if source is None or source["activity_id"] is not None:
        return False
    try:
        parsed = parse_fit(content) if source_kind == "fit" else parse_gpx(content)
    except GpxError:
        return False
    identity = canonical_recording(parsed.tracks, parsed.timestamps, parsed.activity_type)
    if identity is None:
        return False
    deleted_at = db.execute(text("""SELECT max(deleted_at) FROM recording_identity_deletions
        WHERE account_id=:account AND fingerprint=:fingerprint AND fingerprint_version=:version"""), {
        "account": account_id, "fingerprint": identity.fingerprint, "version": identity.version,
    }).scalar_one_or_none()
    return deleted_at is not None and source["created_at"] <= deleted_at


def _source_jobs(db, account_id: str, source_id: int, revision: int, *, lock: bool = True) -> list[int]:
    lock_clause = " FOR UPDATE" if lock else ""
    return db.execute(text(f"""SELECT id FROM jobs WHERE account_id=:account AND (
            (kind='process_upload' AND dedupe_key=:upload_key) OR
            (kind='match_coverage' AND (payload->>'source_id'=:source_text OR id IN (
                SELECT job_id FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source
            )))
        ) ORDER BY id{lock_clause}"""), {
        "account": account_id, "upload_key": f"upload:{source_id}:r{revision}",
        "source_text": str(source_id), "source": source_id,
    }).scalars().all()


def _terminalize_manifest_items(db, account_id: str, source_id: int) -> None:
    db.execute(text("""UPDATE import_batch_items SET status='deleted',error_code=NULL,
            updated_at=clock_timestamp()
        WHERE account_id=:account AND source_id=:source AND status<>'deleted'"""), {
        "account": account_id, "source": source_id,
    })


def _enqueue_original_cleanup(db, account_id: str, activity_id: int | None,
                              source_id: int, revision: int, object_key: str | None) -> None:
    if object_key:
        dedupe = (f"activity:{activity_id}:source:{source_id}:r{revision}" if activity_id is not None
                  else f"unassociated-source:{source_id}:r{revision}")
        enqueue(db, account_id=account_id, kind="delete_private_object", dedupe_key=dedupe,
                payload={"object_key": object_key}, priority=100)


def create_recording_groups_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/activities/{activity_id}/sources", response_model=ActivitySourcePage)
    def list_activity_sources(
        activity_id: int = Path(ge=MIN_ACTIVITY_ID, le=MAX_ID),
        page: int = Query(1, ge=1, le=1_000_000),
        page_size: int = Query(20, ge=1, le=20),
        account_id: str = Depends(current_user),
    ):
        offset = (page - 1) * page_size
        with engine.connect() as db:
            exists = db.execute(text("SELECT 1 FROM activities WHERE id=:id AND user_id=:account"), {
                "id": activity_id, "account": account_id,
            }).first()
            if exists is None:
                raise HTTPException(status_code=404, detail="Activity not found.")
            total = db.execute(text("""SELECT count(*) FROM activity_sources
                WHERE account_id=:account AND activity_id=:activity AND source_kind IN ('gpx','fit')"""), {
                "account": account_id, "activity": activity_id,
            }).scalar_one()
            rows = db.execute(text("""SELECT id,source_kind,created_at FROM activity_sources
                WHERE account_id=:account AND activity_id=:activity AND source_kind IN ('gpx','fit')
                ORDER BY id LIMIT :limit OFFSET :offset"""), {
                "account": account_id, "activity": activity_id, "limit": page_size, "offset": offset,
            }).mappings().all()
        return {
            "activity_id": str(activity_id),
            "items": [{"id": str(row["id"]), "source_kind": row["source_kind"],
                       "created_at": row["created_at"]} for row in rows],
            "page": page, "page_size": page_size, "total": int(total),
        }

    @router.delete("/api/activities/{activity_id}/sources/{source_id}", status_code=204)
    def delete_activity_source(
        activity_id: int = Path(ge=MIN_ACTIVITY_ID, le=MAX_ID),
        source_id: int = Path(ge=1, le=MAX_ID),
        account_id: str = Depends(current_user),
    ):
        _delete_activity_source(engine, account_id, activity_id, source_id)
        return Response(status_code=204)

    return router


def _delete_activity_source(engine: Engine, account_id: str, activity_id: int,
                            source_id: int) -> None:
    with _transaction(engine) as db:
        owner_lock(db, account_id, exclusive=True)
        hint = db.execute(text("""SELECT id,revision FROM activity_sources
            WHERE account_id=:account AND activity_id=:activity AND id=:source
              AND source_kind IN ('gpx','fit')"""), {
            "account": account_id, "activity": activity_id, "source": source_id,
        }).mappings().first()
        if hint is None:
            raise HTTPException(status_code=404, detail="Activity source not found.")

        job_ids = _source_jobs(db, account_id, source_id, int(hint["revision"]))
        source = db.execute(text("""SELECT id,revision,private_object_key,source_kind,activity_id
            FROM activity_sources WHERE id=:source AND account_id=:account FOR UPDATE"""), {
            "source": source_id, "account": account_id,
        }).mappings().first()
        activity = db.execute(text("""SELECT id,name,date,activity_type,tracks,timestamps,
                recording_fingerprint,recording_fingerprint_version
            FROM activities WHERE id=:activity AND user_id=:account FOR UPDATE"""), {
            "activity": activity_id, "account": account_id,
        }).mappings().first()
        if (source is None or activity is None or source["activity_id"] != activity_id
                or source["revision"] != hint["revision"] or source["source_kind"] not in {"gpx", "fit"}):
            raise HTTPException(status_code=404, detail="Activity source not found.")
        related_jobs = set(_source_jobs(db, account_id, source_id, int(source["revision"]), lock=False))
        if not related_jobs.issubset(set(job_ids)):
            raise HTTPException(status_code=409, detail="Activity source changed; retry the request.")

        datasets = db.execute(text("""SELECT dataset_id FROM coverage_source_runs
            WHERE account_id=:account AND source_id=:source
            UNION SELECT dataset_id FROM source_node_contributions
            WHERE account_id=:account AND source_id=:source ORDER BY dataset_id"""), {
            "account": account_id, "source": source_id,
        }).scalars().all()
        survivor_count = db.execute(text("""SELECT count(*) FROM activity_sources
            WHERE account_id=:account AND activity_id=:activity AND id<>:source"""), {
            "account": account_id, "activity": activity_id, "source": source_id,
        }).scalar_one()
        if not survivor_count:
            identity = stored_or_legacy_identity(activity)
            if identity is not None:
                record_identity_deletion(db, account_id, identity)
        if job_ids:
            db.execute(text("""UPDATE jobs SET status='cancelled',lease_token=NULL,leased_until=NULL,
                    last_error='source_deleted',updated_at=clock_timestamp()
                WHERE account_id=:account AND id=ANY(:ids) AND status IN ('queued','running')"""), {
                "account": account_id, "ids": job_ids,
            })
        _enqueue_original_cleanup(db, account_id, activity_id, source_id,
                                  int(source["revision"]), source["private_object_key"])
        _terminalize_manifest_items(db, account_id, source_id)
        db.execute(text("DELETE FROM activity_sources WHERE id=:source AND account_id=:account"), {
            "source": source_id, "account": account_id,
        })
        if survivor_count:
            _refresh_activity_processing(db, account_id=account_id, source_id=source_id,
                                         source_revision=int(source["revision"]), activity_id=activity_id)
        else:
            db.execute(text("DELETE FROM activities WHERE id=:activity AND user_id=:account"), {
                "activity": activity_id, "account": account_id,
            })
        for dataset_id in datasets:
            rebuild_account_coverage(db, account_id, int(dataset_id))
