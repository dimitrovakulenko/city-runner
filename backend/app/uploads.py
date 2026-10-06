"""Authenticated GPX upload endpoints and lease-fenced source processing."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import Engine, text
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.concurrency import run_in_threadpool

from backend.app.coverage import queue_source_coverage
from backend.app.fit import parse_fit
from backend.app.gpx import GpxError, MAX_GPX_BYTES, parse_gpx
from backend.app.jobs import complete, enqueue, fail
from backend.app.storage import LocalObjectStore


MAX_UPLOAD_REQUEST_BYTES = MAX_GPX_BYTES + 64 * 1024
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
UploadState = Literal["queued", "processing", "succeeded", "failed", "cancelled"]


class UploadResponse(BaseModel):
    id: str
    status: UploadState
    job_id: str
    duplicate: bool


class UploadStatusResponse(BaseModel):
    id: str
    status: UploadState
    activity_id: str | None
    job_id: str | None
    error: str | None


class UploadBodyTooLarge(MultiPartException):
    pass


async def _one_activity_file(request: Request, *, expected_format: str | None = None) -> tuple[bytes, str]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_UPLOAD_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="Upload exceeds the 10 MiB limit.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid multipart request.") from exc

    async def bounded_stream():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_UPLOAD_REQUEST_BYTES:
                raise UploadBodyTooLarge("Upload exceeds the 10 MiB limit.")
            yield chunk

    try:
        form = await MultiPartParser(
            request.headers, bounded_stream(), max_files=1, max_fields=0, max_part_size=1024,
        ).parse()
    except UploadBodyTooLarge as exc:
        raise HTTPException(status_code=413, detail="Upload exceeds the 10 MiB limit.") from exc
    except MultipartParseError as exc:
        raise HTTPException(status_code=400, detail="Invalid multipart request.") from exc
    except MultiPartException as exc:
        raise HTTPException(status_code=400, detail="Invalid multipart request.") from exc

    try:
        items = form.multi_items()
        if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
            raise HTTPException(status_code=422, detail="Upload one GPX or FIT file in the 'file' field.")
        filename = items[0][1].filename or ""
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if expected_format is not None and expected_format not in {"gpx", "fit"}:
            raise ValueError("unsupported expected format")
        content = await items[0][1].read(MAX_GPX_BYTES + 1)
        if len(content) > MAX_GPX_BYTES:
            raise HTTPException(status_code=413, detail="Activity file exceeds the 10 MiB limit.")
        if not content:
            raise HTTPException(status_code=422, detail="Activity file is empty.")
        actual_format = extension if extension in {"gpx", "fit"} else (
            "fit" if len(content) >= 12 and content[8:12] == b".FIT" else "gpx"
        )
        if expected_format is not None and actual_format != expected_format:
            raise HTTPException(status_code=422, detail="File does not match the batch item's format.")
        return content, actual_format
    finally:
        await form.close()


async def _one_gpx_file(request: Request) -> bytes:
    content, extension = await _one_activity_file(request)
    if extension != "gpx":
        raise HTTPException(status_code=422, detail="Upload one GPX file in the 'file' field.")
    return content


def _job_state(job_status: str | None, source_status: str) -> str:
    if source_status == "succeeded":
        return "succeeded"
    if job_status == "running":
        return "processing"
    if job_status in {"queued", "failed", "cancelled"}:
        return job_status
    return source_status


def _safe_error(value: str | None) -> str | None:
    return value if value and ERROR_CODE.fullmatch(value) else None


def _persist_upload(engine: Engine, object_store: LocalObjectStore, account_id: str,
                    content: bytes, source_kind: str = "gpx") -> dict[str, Any]:
    if source_kind not in {"gpx", "fit"}:
        raise ValueError("unsupported source kind")
    content_hash = hashlib.sha256(content).hexdigest()
    object_key = object_store.write(content, extension=source_kind)
    duplicate = False
    try:
        with engine.begin() as db:
            row = db.execute(text("""INSERT INTO activity_sources
                (account_id, source_kind, content_hash, revision, private_object_key, status)
                VALUES (:account_id, :source_kind, :content_hash, 1, :object_key, 'queued')
                ON CONFLICT (account_id, source_kind, content_hash) DO NOTHING
                RETURNING id, revision, status"""), {
                "account_id": account_id,
                "source_kind": source_kind,
                "content_hash": content_hash,
                "object_key": object_key,
            }).mappings().first()
            if row is None:
                duplicate = True
                row = db.execute(text("""SELECT id, revision, status FROM activity_sources
                    WHERE account_id=:account_id AND source_kind=:source_kind AND content_hash=:content_hash"""), {
                    "account_id": account_id, "source_kind": source_kind, "content_hash": content_hash,
                }).mappings().one()
            dedupe_key = f"upload:{row['id']}:r{row['revision']}"
            job = enqueue(db, account_id=account_id, kind="process_upload", dedupe_key=dedupe_key,
                          payload={"source_id": row["id"], "revision": row["revision"]}, priority=100)
        return {
            "id": str(row["id"]),
            "status": _job_state(job["status"], row["status"]),
            "job_id": str(job["id"]),
            "duplicate": duplicate,
        }
    except BaseException:
        try:
            object_store.delete(object_key)
        except OSError:
            pass
        raise
    finally:
        if duplicate:
            try:
                object_store.delete(object_key)
            except OSError:
                pass


def create_upload_router(engine: Engine, current_user: Callable[..., str],
                         store: LocalObjectStore | None = None) -> APIRouter:
    router = APIRouter()

    def get_store() -> LocalObjectStore:
        return store or LocalObjectStore()

    @router.post("/api/uploads", status_code=status.HTTP_202_ACCEPTED, response_model=UploadResponse,
                 openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {
                     "schema": {"type": "object", "required": ["file"], "properties": {
                         "file": {"type": "string", "format": "binary"},
                     }},
                 }}}})
    async def upload(request: Request, account_id: str = Depends(current_user)):
        content, source_kind = await _one_activity_file(request)
        object_store = get_store()
        return await run_in_threadpool(_persist_upload, engine, object_store, account_id, content, source_kind)

    @router.get("/api/uploads/{source_id}", response_model=UploadStatusResponse)
    def get_upload(source_id: int = Path(ge=1, le=9223372036854775807),
                   account_id: str = Depends(current_user)):
        with engine.connect() as db:
            row = db.execute(text("""SELECT source.id, source.activity_id, source.status AS source_status,
                    source.last_error, job.id AS job_id, job.status AS job_status, job.last_error AS job_error
                FROM activity_sources AS source
                LEFT JOIN jobs AS job ON job.account_id=source.account_id AND job.kind='process_upload'
                    AND job.dedupe_key=concat('upload:', source.id::text, chr(58), 'r', source.revision::text)
                WHERE source.id=:id AND source.account_id=:account_id"""), {
                "id": source_id, "account_id": account_id,
            }).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Upload not found.")
        return {
            "id": str(row["id"]),
            "status": _job_state(row["job_status"], row["source_status"]),
            "activity_id": str(row["activity_id"]) if row["activity_id"] is not None else None,
            "job_id": str(row["job_id"]) if row["job_id"] is not None else None,
            "error": _safe_error(row["job_error"] or row["last_error"]),
        }

    return router


def _locked_context(db, job_id: int, lease_token: str, source_id: int, revision: int):
    job = db.execute(text("""SELECT id, account_id, kind FROM jobs WHERE id=:id AND status='running'
        AND lease_token=:token AND leased_until > clock_timestamp() FOR UPDATE"""), {
        "id": job_id, "token": lease_token,
    }).mappings().first()
    if job is None:
        return None, None
    source = db.execute(text("""SELECT id, account_id, activity_id, revision, private_object_key, status, source_kind
        FROM activity_sources WHERE id=:id FOR UPDATE"""), {"id": source_id}).mappings().first()
    if (source is None or source["account_id"] != job["account_id"] or job["kind"] != "process_upload"
            or source["revision"] != revision):
        return job, None
    return job, source


def _mark_failed(engine: Engine, *, job_id: int, lease_token: str, source_id: int,
                 account_id: str | None, revision: int, error_code: str, permanent: bool) -> bool:
    with engine.begin() as db:
        job, source = _locked_context(db, job_id, lease_token, source_id, revision)
        if job is None:
            return False
        if not fail(db, job_id=job_id, lease_token=lease_token, error_code=error_code, permanent=permanent):
            return False
        if source is not None:
            final_job_status = db.execute(text("SELECT status FROM jobs WHERE id=:id"), {
                "id": job_id,
            }).scalar_one()
            result = db.execute(text("""UPDATE activity_sources SET status=:status, last_error=:error,
                    updated_at=clock_timestamp()
                WHERE id=:id AND account_id=:account_id AND revision=:revision"""), {
                "status": "failed" if permanent or final_job_status == "failed" else "queued",
                "error": error_code,
                "id": source_id, "account_id": account_id, "revision": revision,
            })
            if result.rowcount != 1:
                raise RuntimeError("source revision changed")
        return True


def process_upload(engine: Engine, job: dict[str, Any], store: LocalObjectStore | None = None) -> None:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        fail(engine, job_id=int(job["id"]), lease_token=str(job["lease_token"]),
             error_code="invalid_payload", permanent=True)
        return
    source_id = payload.get("source_id")
    revision = payload.get("revision")
    job_id = int(job["id"])
    lease_token = str(job["lease_token"])
    if not isinstance(source_id, int) or source_id < 1 or not isinstance(revision, int) or revision < 1:
        fail(engine, job_id=job_id, lease_token=lease_token, error_code="invalid_payload", permanent=True)
        return

    with engine.begin() as db:
        locked_job, source = _locked_context(db, job_id, lease_token, source_id, revision)
        if locked_job is None:
            return
        if source is None:
            fail(db, job_id=job_id, lease_token=lease_token, error_code="stale_source", permanent=True)
            return
        if source["status"] == "succeeded" and source["activity_id"] is not None:
            complete(db, job_id=job_id, lease_token=lease_token)
            return
        db.execute(text("""UPDATE activity_sources SET status='processing', updated_at=clock_timestamp()
            WHERE id=:id AND account_id=:account_id AND revision=:revision"""), {
            "id": source_id, "account_id": source["account_id"], "revision": revision,
        })
        account_id = source["account_id"]
        object_key = source["private_object_key"]

    object_store = store or LocalObjectStore()
    try:
        content = object_store.read(object_key)
    except (OSError, ValueError):
        _mark_failed(engine, job_id=job_id, lease_token=lease_token, source_id=source_id,
                     account_id=account_id, revision=revision, error_code="source_unavailable", permanent=False)
        return
    try:
        parser = parse_fit if source["source_kind"] == "fit" else parse_gpx
        parsed = parser(content)
    except GpxError as exc:
        _mark_failed(engine, job_id=job_id, lease_token=lease_token, source_id=source_id,
                     account_id=account_id, revision=revision, error_code=exc.code, permanent=True)
        return

    import json

    with engine.begin() as db:
        locked_job, current_source = _locked_context(db, job_id, lease_token, source_id, revision)
        if locked_job is None:
            return
        if current_source is None:
            fail(db, job_id=job_id, lease_token=lease_token, error_code="stale_source", permanent=True)
            return
        activity_id = db.execute(text("""INSERT INTO activities
            (user_id, name, date, activity_type, processed, unmapped_points, tracks, timestamps)
            VALUES (:account_id, :name, :date, :activity_type, false, 0,
                CAST(:tracks AS json), CAST(:timestamps AS json)) RETURNING id"""), {
            "account_id": current_source["account_id"],
            "name": parsed.name,
            "date": parsed.date,
            "activity_type": parsed.activity_type,
            "tracks": json.dumps(parsed.tracks, allow_nan=False, separators=(",", ":")),
            "timestamps": json.dumps(parsed.timestamps, allow_nan=False, separators=(",", ":")),
        }).scalar_one()
        result = db.execute(text("""UPDATE activity_sources SET activity_id=:activity_id,
                status='succeeded', last_error=NULL, updated_at=clock_timestamp()
            WHERE id=:id AND account_id=:account_id AND revision=:revision"""), {
            "activity_id": activity_id,
            "id": source_id,
            "account_id": current_source["account_id"],
            "revision": revision,
        })
        if result.rowcount != 1:
            raise RuntimeError("source revision changed")
        queue_source_coverage(db, account_id=current_source["account_id"], source_id=source_id,
                              source_revision=revision, activity_id=activity_id)
        if not complete(db, job_id=job_id, lease_token=lease_token):
            raise RuntimeError("job lease expired")
