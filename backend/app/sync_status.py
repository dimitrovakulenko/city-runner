"""Owner-only GPX/FIT synchronization status and retry endpoints."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from backend.app.coverage import _pending_summary
from backend.app.owner_guard import owner_lock


MAX_BIGINT = 9_223_372_036_854_775_807
MAX_ACTIVE_DATASETS_PER_RETRY = 32
STATUS_WORK_BUDGET_SECONDS = 1.5
SAFE_SYNC_ERRORS = {
    "invalid_gpx_size", "invalid_gpx_xml", "invalid_gpx_root", "invalid_gpx_coordinate",
    "gpx_point_limit", "gpx_no_track_points", "invalid_fit_size", "invalid_fit_file",
    "fit_multiple_streams", "fit_message_limit", "fit_not_activity", "fit_record_limit",
    "fit_no_track_points", "unsupported_coverage_dataset", "lease_expired",
    "coverage_dataset_unavailable", "source_revision_mismatch",
}


FileKind = Literal["gpx", "fit"]
FailureStage = Literal["import", "coverage"]


class SyncProviderCapability(BaseModel):
    provider: Literal["garmin", "strava"]
    available: Literal[False]
    reason: str


class SyncFileCounts(BaseModel):
    queued: int
    processing: int
    imported: int
    failed: int
    unavailable: int


class SyncFiles(BaseModel):
    gpx: SyncFileCounts
    fit: SyncFileCounts


class SyncCoverage(BaseModel):
    ready: int
    pending: int
    failed: int
    unavailable: int


class SyncBatches(BaseModel):
    waiting: int
    accepted: int
    duplicates: int
    deleted: int
    stopped: int


class SyncStatusResponse(BaseModel):
    change_token: str
    activity_count: int
    oldest_activity_date: date | None
    last_import_at: datetime | None
    files: SyncFiles
    coverage: SyncCoverage
    batches: SyncBatches
    providers: list[SyncProviderCapability]


class SyncFailure(BaseModel):
    source_id: str
    file_kind: FileKind
    activity_id: str | None
    stage: FailureStage
    error: str | None
    retry_action: Literal["retry-import", "retry-coverage"]


class SyncFailurePage(BaseModel):
    items: list[SyncFailure]
    page: int
    page_size: int
    total: int
    change_token: str


def _token(account_id: str, owner_revision: int, geography_revision: int) -> str:
    owner_key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    payload = json.dumps([1, owner_key, owner_revision, geography_revision], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _safe_sync_error(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in SAFE_SYNC_ERRORS else "processing_failed"


def _set_budget(db, deadline: float) -> None:
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise HTTPException(status_code=503, detail="Sync status exceeded its work limit.")
    db.execute(text("SELECT set_config('statement_timeout',:value,true),set_config('lock_timeout',:lock,true)"), {
        "value": f"{remaining_ms}ms", "lock": f"{min(250, remaining_ms)}ms",
    }).all()


@contextmanager
def _transaction(engine: Engine, isolation: str, *, read_only: bool = False):
    try:
        with engine.connect().execution_options(isolation_level=isolation) as db:
            with db.begin():
                if read_only:
                    db.execute(text("SET TRANSACTION READ ONLY"))
                yield db
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
        if code in {"57014", "55P03"}:
            raise HTTPException(status_code=503, detail="Sync request exceeded its database work limit.") from error
        raise


def _read_version(db, account_id: str, deadline: float) -> str:
    _set_budget(db, deadline)
    version = db.execute(text("""SELECT COALESCE(owner.revision,0) AS owner_revision,
            geography.revision AS geography_revision
        FROM accounts account CROSS JOIN sync_geography_revision geography
        LEFT JOIN sync_account_revisions owner ON owner.account_id=account.id
        WHERE account.id=:account AND geography.singleton=TRUE"""), {
        "account": account_id,
    }).mappings().first()
    if version is None:
        raise HTTPException(status_code=401, detail="Unauthenticated.")
    return _token(account_id, int(version["owner_revision"]), int(version["geography_revision"]))


def _status_snapshot(db, account_id: str, deadline: float, token: str) -> dict:
    _set_budget(db, deadline)
    activity = db.execute(text("""SELECT count(*) AS count,
            min(CASE WHEN date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND substring(date,1,4)<>'0000'
                AND pg_input_is_valid(date,'date')
                THEN date::date END) AS oldest
        FROM activities WHERE user_id=:account"""), {"account": account_id}).mappings().one()

    _set_budget(db, deadline)
    file_rows = db.execute(text("""SELECT source.source_kind,
            count(*) FILTER (WHERE job.status='queued') AS queued,
            count(*) FILTER (WHERE job.status='running') AS processing,
            count(*) FILTER (WHERE job.status='succeeded' AND source.activity_id IS NOT NULL) AS imported,
            count(*) FILTER (WHERE job.status IN ('failed','cancelled')) AS failed,
            count(*) FILTER (WHERE job.id IS NULL OR
                (job.status='succeeded' AND source.activity_id IS NULL)) AS unavailable,
            max(job.updated_at) FILTER (WHERE job.status='succeeded' AND source.activity_id IS NOT NULL) AS last_import_at
        FROM activity_sources source
        LEFT JOIN jobs job ON job.account_id=source.account_id AND job.kind='process_upload'
          AND job.dedupe_key=concat('upload:',source.id::text,chr(58),'r',source.revision::text)
        WHERE source.account_id=:account AND source.source_kind IN ('gpx','fit')
        GROUP BY source.source_kind"""), {"account": account_id}).mappings().all()
    files = {kind: {"queued": 0, "processing": 0, "imported": 0, "failed": 0, "unavailable": 0}
             for kind in ("gpx", "fit")}
    import_times = []
    for row in file_rows:
        files[row["source_kind"]] = {key: int(row[key]) for key in files[row["source_kind"]]}
        if row["last_import_at"] is not None:
            import_times.append(row["last_import_at"])

    _set_budget(db, deadline)
    coverage = db.execute(text("""WITH active AS (
            SELECT id FROM map_datasets WHERE status='active' AND coverage_mode='complete'
        ), imported AS (
            SELECT source.id, source.revision FROM activity_sources source
            JOIN jobs ingestion ON ingestion.account_id=source.account_id AND ingestion.kind='process_upload'
              AND ingestion.dedupe_key=concat('upload:',source.id::text,chr(58),'r',source.revision::text)
            WHERE source.account_id=:account AND source.source_kind IN ('gpx','fit')
              AND source.activity_id IS NOT NULL AND ingestion.status='succeeded'
        ), readiness AS (
            SELECT source.id,
              count(active.id) AS active_count,
              count(*) FILTER (WHERE match_job.status IN ('failed','cancelled')
                  OR (match_job.id IS NOT NULL AND run.status='failed')) AS failed_count,
              count(*) FILTER (WHERE active.id IS NOT NULL AND
                  (run.status IS DISTINCT FROM 'succeeded' OR match_job.status IS DISTINCT FROM 'succeeded')) AS pending_count
            FROM imported source CROSS JOIN (SELECT count(*) FROM active) active_total
            LEFT JOIN active ON TRUE
            LEFT JOIN coverage_source_runs run ON run.account_id=:account AND run.source_id=source.id
              AND run.source_revision=source.revision AND run.dataset_id=active.id
            LEFT JOIN jobs match_job ON match_job.account_id=run.account_id AND match_job.id=run.job_id
              AND match_job.kind='match_coverage'
              AND match_job.payload->>'source_id'=source.id::text
              AND match_job.payload->>'source_revision'=source.revision::text
              AND match_job.payload->>'dataset_id'=active.id::text
            GROUP BY source.id, active_total.count
        )
        SELECT count(*) FILTER (WHERE active_count=0) AS unavailable,
          count(*) FILTER (WHERE active_count>0 AND failed_count>0) AS failed,
          count(*) FILTER (WHERE active_count>0 AND failed_count=0 AND pending_count>0) AS pending,
          count(*) FILTER (WHERE active_count>0 AND failed_count=0 AND pending_count=0) AS ready
        FROM readiness"""), {"account": account_id}).mappings().one()

    _set_budget(db, deadline)
    batches = db.execute(text("""SELECT
            count(*) FILTER (WHERE item.status='awaiting_upload') AS waiting,
            count(*) FILTER (WHERE item.status<>'awaiting_upload' AND NOT item.duplicate
              AND item.source_id IS NOT NULL AND source.id IS NOT NULL AND item.status<>'deleted') AS accepted,
            count(*) FILTER (WHERE item.duplicate) AS duplicates,
            count(*) FILTER (WHERE item.status='deleted' OR
              (item.source_id IS NOT NULL AND source.id IS NULL)) AS deleted,
            (SELECT count(*) FROM import_batches stopped WHERE stopped.account_id=:account
              AND stopped.state='stopped') AS stopped
        FROM import_batch_items item
        LEFT JOIN activity_sources source ON source.id=item.source_id AND source.account_id=item.account_id
        WHERE item.account_id=:account"""), {"account": account_id}).mappings().one()
    return {
        "change_token": token,
        "activity_count": int(activity["count"]),
        "oldest_activity_date": activity["oldest"],
        "last_import_at": max(import_times) if import_times else None,
        "files": files,
        "coverage": {key: int(coverage[key]) for key in ("ready", "pending", "failed", "unavailable")},
        "batches": {key: int(batches[key]) for key in ("waiting", "accepted", "duplicates", "deleted", "stopped")},
        "providers": [
            {"provider": "garmin", "available": False, "reason": "The Garmin adapter is not implemented."},
            {"provider": "strava", "available": False, "reason": "The Strava adapter is not implemented."},
        ],
    }


def create_sync_status_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/sync/status", response_model=SyncStatusResponse)
    def sync_status(response: Response, since: str | None = Query(None, max_length=200),
                    account_id: str = Depends(current_user)):
        deadline = time.monotonic() + STATUS_WORK_BUDGET_SECONDS
        with _transaction(engine, "REPEATABLE READ", read_only=True) as db:
            _set_budget(db, deadline)
            owner_lock(db, account_id)
            token = _read_version(db, account_id, deadline)
            if since is not None and since == token:
                return Response(status_code=204, headers={"Cache-Control": "no-store, private"})
            result = _status_snapshot(db, account_id, deadline, token)
        response.headers["Cache-Control"] = "no-store, private"
        return result

    @router.get("/api/sync/failures", response_model=SyncFailurePage)
    def sync_failures(response: Response, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=20),
                      account_id: str = Depends(current_user)):
        offset = (page - 1) * page_size
        if offset > 2_147_483_647:
            raise HTTPException(status_code=422, detail="Page is out of range.")
        deadline = time.monotonic() + STATUS_WORK_BUDGET_SECONDS
        with _transaction(engine, "REPEATABLE READ", read_only=True) as db:
            _set_budget(db, deadline)
            owner_lock(db, account_id)
            token = _read_version(db, account_id, deadline)
            filters = """WITH failures AS (
                    SELECT source.id AS source_id,source.source_kind,source.activity_id,
                        'import'::text AS stage,job.last_error AS error
                    FROM activity_sources source JOIN jobs job ON job.account_id=source.account_id
                      AND job.kind='process_upload' AND job.dedupe_key=concat('upload:',source.id::text,chr(58),'r',source.revision::text)
                    WHERE source.account_id=:account AND source.source_kind IN ('gpx','fit')
                      AND job.status IN ('failed','cancelled')
                    UNION ALL
                    SELECT source.id,source.source_kind,source.activity_id,'coverage'::text,MIN(job.last_error)
                    FROM activity_sources source JOIN jobs ingestion ON ingestion.account_id=source.account_id
                      AND ingestion.kind='process_upload' AND ingestion.dedupe_key=concat('upload:',source.id::text,chr(58),'r',source.revision::text)
                      AND ingestion.status='succeeded'
                    JOIN coverage_source_runs run ON run.account_id=source.account_id
                      AND run.source_id=source.id AND run.source_revision=source.revision
                    JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id
                      AND job.kind='match_coverage'
                      AND job.payload->>'source_id'=source.id::text
                      AND job.payload->>'source_revision'=source.revision::text
                      AND job.payload->>'dataset_id'=run.dataset_id::text
                    JOIN map_datasets dataset ON dataset.id=run.dataset_id
                    WHERE source.account_id=:account AND source.source_kind IN ('gpx','fit')
                      AND source.activity_id IS NOT NULL
                      AND dataset.status='active' AND dataset.coverage_mode='complete'
                      AND job.status IN ('failed','cancelled')
                    GROUP BY source.id,source.source_kind,source.activity_id
            ) """
            _set_budget(db, deadline)
            total = db.execute(text(filters + "SELECT count(*) FROM failures"), {
                "account": account_id,
            }).scalar_one()
            _set_budget(db, deadline)
            rows = db.execute(text(filters + """SELECT source_id,source_kind,activity_id,stage,error
                FROM failures ORDER BY source_id,stage LIMIT :limit OFFSET :offset"""), {
                "account": account_id, "limit": page_size, "offset": offset,
            }).mappings().all()
        response.headers["Cache-Control"] = "no-store, private"
        return {"items": [{
            "source_id": str(row["source_id"]), "file_kind": row["source_kind"],
            "activity_id": str(row["activity_id"]) if row["activity_id"] is not None else None,
            "stage": row["stage"], "error": _safe_sync_error(row["error"]),
            "retry_action": "retry-import" if row["stage"] == "import" else "retry-coverage",
        } for row in rows], "page": page, "page_size": page_size, "total": int(total),
            "change_token": token}

    @router.post("/api/uploads/{source_id}/retry-coverage", status_code=204)
    def retry_coverage(source_id: int = Path(ge=1, le=MAX_BIGINT), account_id: str = Depends(current_user)):
        deadline = time.monotonic() + STATUS_WORK_BUDGET_SECONDS
        with _transaction(engine, "READ COMMITTED") as db:
            _set_budget(db, deadline)
            owner_lock(db, account_id)
            source_hint = db.execute(text("""SELECT source.revision,ingestion.status AS ingestion_status,
                    ingestion.id AS ingestion_job_id
                FROM activity_sources source LEFT JOIN jobs ingestion
                  ON ingestion.account_id=source.account_id AND ingestion.kind='process_upload'
                  AND ingestion.dedupe_key=concat('upload:',source.id::text,chr(58),'r',source.revision::text)
                WHERE source.id=:source AND source.account_id=:account AND source.source_kind IN ('gpx','fit')"""), {
                "source": source_id, "account": account_id,
            }).mappings().first()
            if source_hint is None:
                raise HTTPException(status_code=404, detail="Upload not found.")
            if source_hint["ingestion_status"] != "succeeded":
                raise HTTPException(status_code=409, detail="Upload has not finished importing.")
            datasets = db.execute(text("""SELECT id FROM map_datasets
                WHERE status='active' AND coverage_mode='complete' ORDER BY id LIMIT :limit"""), {
                "limit": MAX_ACTIVE_DATASETS_PER_RETRY + 1,
            }).scalars().all()
            if len(datasets) > MAX_ACTIVE_DATASETS_PER_RETRY:
                raise HTTPException(status_code=409, detail="Too many active datasets to retry in one request.")
            _set_budget(db, deadline)
            locked_jobs = db.execute(text("""SELECT job.id,job.kind,job.status,job.payload,
                    run.dataset_id,run.status AS run_status,run.job_id,run.source_revision
                FROM jobs job
                LEFT JOIN coverage_source_runs run ON run.account_id=job.account_id AND run.job_id=job.id
                  AND run.source_id=:source AND run.source_revision=:revision
                LEFT JOIN map_datasets dataset ON dataset.id=run.dataset_id
                WHERE job.account_id=:account AND (
                    (job.kind='process_upload' AND job.dedupe_key=:upload_key) OR
                    (job.kind='match_coverage' AND run.account_id=:account
                      AND dataset.status='active' AND dataset.coverage_mode='complete'))
                ORDER BY job.id FOR UPDATE OF job"""), {
                "account": account_id, "source": source_id, "revision": source_hint["revision"],
                "upload_key": f"upload:{source_id}:r{source_hint['revision']}",
            }).mappings().all()
            source = db.execute(text("""SELECT id,revision,status,activity_id FROM activity_sources
                WHERE id=:source AND account_id=:account FOR UPDATE"""), {
                "source": source_id, "account": account_id,
            }).mappings().first()
            if source is None:
                raise HTTPException(status_code=404, detail="Upload not found.")
            if source["revision"] != source_hint["revision"]:
                raise HTTPException(status_code=409, detail="Upload changed; retry again.")
            if source["status"] != "succeeded" or source["activity_id"] is None:
                raise HTTPException(status_code=409, detail="Upload has not finished importing.")
            process_job = next((row for row in locked_jobs if row["kind"] == "process_upload"), None)
            if (process_job is None or process_job["id"] != source_hint["ingestion_job_id"]
                    or process_job["status"] != "succeeded"):
                raise HTTPException(status_code=409, detail="Upload has not finished importing.")
            activity_exists = db.execute(text("""SELECT id FROM activities
                WHERE id=:activity AND user_id=:account FOR UPDATE"""), {
                "activity": source["activity_id"], "account": account_id,
            }).scalar_one_or_none()
            if activity_exists is None:
                raise HTTPException(status_code=409, detail="Upload has not finished importing.")
            run_rows = db.execute(text("""SELECT * FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source AND source_revision=:revision
                  AND dataset_id=ANY(:datasets) ORDER BY dataset_id FOR UPDATE"""), {
                "account": account_id, "source": source_id, "revision": source["revision"],
                "datasets": datasets,
            }).mappings().all() if datasets else []
            fresh_datasets = db.execute(text("""SELECT id FROM map_datasets
                WHERE status='active' AND coverage_mode='complete' ORDER BY id
                LIMIT :limit FOR SHARE"""), {
                "limit": MAX_ACTIVE_DATASETS_PER_RETRY + 1,
            }).scalars().all()
            if list(map(int, fresh_datasets)) != list(map(int, datasets)):
                raise HTTPException(status_code=409, detail="Active datasets changed; retry again.")
            run_by_id = {int(row["job_id"]): row for row in run_rows}
            reset_datasets = []
            for row in locked_jobs:
                run = run_by_id.get(int(row["id"]))
                if row["kind"] != "match_coverage" or row["dataset_id"] is None:
                    continue
                payload = row["payload"]
                expected_payload = {"source_id": source_id, "source_revision": source["revision"],
                                    "dataset_id": int(row["dataset_id"])}
                if (run is None or int(row["job_id"]) != int(row["id"])
                        or row.get("kind") != "match_coverage" or payload != expected_payload):
                    continue
                if row["status"] not in {"failed", "cancelled"}:
                    continue
                changed = db.execute(text("""UPDATE jobs SET status='queued',attempts=0,
                        available_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,last_error=NULL,
                        updated_at=clock_timestamp()
                    WHERE account_id=:account AND id=:id AND status IN ('failed','cancelled')"""), {
                    "account": account_id, "id": row["id"],
                })
                if changed.rowcount != 1:
                    continue
                db.execute(text("""UPDATE coverage_source_runs SET status='queued',last_error=NULL,
                        updated_at=clock_timestamp()
                    WHERE account_id=:account AND source_id=:source AND source_revision=:revision
                      AND dataset_id=:dataset AND job_id=:job"""), {
                    "account": account_id, "source": source_id, "revision": source["revision"],
                    "dataset": row["dataset_id"], "job": row["id"],
                })
                reset_datasets.append(int(row["dataset_id"]))
            for dataset_id in sorted(set(reset_datasets)):
                _pending_summary(db, account_id, dataset_id)
            if reset_datasets:
                db.execute(text("UPDATE activities SET processed=FALSE WHERE id=:activity AND user_id=:account"), {
                    "activity": source["activity_id"], "account": account_id,
                })
        return Response(status_code=204, headers={"Cache-Control": "no-store, private"})

    return router
