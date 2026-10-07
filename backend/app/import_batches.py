"""Durable, bounded manifests for sequential GPX/FIT uploads."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from backend.app.jobs import enqueue
from backend.app.storage import LocalObjectStore, cleanup_unreferenced_original
from backend.app.uploads import _job_state, _one_activity_file, _safe_error
from backend.app.owner_guard import owner_lock

MAX_BIGINT = 9_223_372_036_854_775_807
MULTIPART_BODY = {"requestBody": {"required": True, "content": {"multipart/form-data": {
    "schema": {"type": "object", "required": ["file"], "properties": {
        "file": {"type": "string", "format": "binary"},
    }},
}}}}

BatchState = Literal["open", "stopped"]
BatchItemState = Literal["awaiting_upload", "queued", "processing", "succeeded", "failed", "deleted"]


class ImportBatchFile(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    format: Literal["gpx", "fit"]

    @field_validator("name", mode="before")
    @classmethod
    def valid_unicode_name(cls, value):
        if isinstance(value, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise HTTPException(status_code=422, detail="Manifest file name is invalid.")
        return value


class ImportBatchCreate(BaseModel):
    request_id: UUID
    files: list[ImportBatchFile] = Field(min_length=1, max_length=20)


class ImportBatchItemResponse(BaseModel):
    id: str
    name: str
    format: Literal["gpx", "fit"]
    status: BatchItemState
    source_id: str | None
    activity_id: str | None
    duplicate: bool
    error: str | None


class ImportBatchCounts(BaseModel):
    awaiting_upload: int
    queued: int
    processing: int
    succeeded: int
    failed: int
    deleted: int


class ImportBatchResponse(BaseModel):
    id: str
    state: BatchState
    created_at: datetime
    items: list[ImportBatchItemResponse]
    counts: ImportBatchCounts


class ImportBatchPage(BaseModel):
    items: list[ImportBatchResponse]
    page: int
    page_size: int
    total: int


def _manifest_name(value: str) -> str:
    name = value.replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable()).strip()
    if not name or len(name) > 200:
        raise HTTPException(status_code=422, detail="Manifest file name is invalid.")
    return name


def _detail(db, account_id: str, batch_id: int) -> dict:
    batch = db.execute(text("""SELECT id,state,created_at FROM import_batches
        WHERE id=:id AND account_id=:account"""), {"id": batch_id, "account": account_id}).mappings().first()
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    rows = db.execute(text("""SELECT item.id,item.name,item.format,item.status AS item_status,
            item.source_id,item.duplicate,item.error_code,source.activity_id,source.status AS source_status,
            source.last_error,job.status AS job_status,job.last_error AS job_error
        FROM import_batch_items item
        LEFT JOIN activity_sources source ON source.id=item.source_id AND source.account_id=item.account_id
        LEFT JOIN jobs job ON job.account_id=source.account_id AND job.kind='process_upload'
            AND job.dedupe_key=concat('upload:',source.id::text,chr(58),'r',source.revision::text)
        WHERE item.batch_id=:batch AND item.account_id=:account ORDER BY item.ordinal"""), {
        "batch": batch_id, "account": account_id,
    }).mappings().all()
    items = []
    for row in rows:
        if row["item_status"] == "awaiting_upload":
            item_status = "awaiting_upload"
        elif row["source_id"] is not None and row["source_status"] is None:
            item_status = "deleted"
        else:
            item_status = _job_state(row["job_status"], row["source_status"] or row["item_status"])
            if item_status == "cancelled":
                item_status = "failed"
        items.append({
            "id": str(row["id"]), "name": row["name"], "format": row["format"],
            "status": item_status, "source_id": str(row["source_id"]) if row["source_id"] else None,
            "activity_id": str(row["activity_id"]) if row["activity_id"] is not None else None,
            "duplicate": row["duplicate"],
            "error": _safe_error(row["job_error"] or row["last_error"] or row["error_code"]),
        })
    counts = {key: 0 for key in ("awaiting_upload","queued","processing","succeeded","failed","deleted")}
    for item in items:
        counts[item["status"]] += 1
    return {"id": str(batch["id"]), "state": batch["state"], "created_at": batch["created_at"],
            "items": items, "counts": counts}


class _SourceChanged(Exception):
    pass


@contextmanager
def _transaction(engine):
    with engine.connect().execution_options(isolation_level="READ COMMITTED") as db:
        with db.begin():
            yield db


def _lock_source(db, account_id, candidate):
    """Lock the discovered revision's job before its source, then revalidate."""
    job = db.execute(text("""SELECT id,status FROM jobs
        WHERE account_id=:account AND kind='process_upload' AND dedupe_key=:key FOR UPDATE"""), {
        "account": account_id, "key": f"upload:{candidate['id']}:r{candidate['revision']}",
    }).mappings().first()
    source = db.execute(text("""SELECT id,revision,status,activity_id FROM activity_sources
        WHERE id=:id AND account_id=:account FOR UPDATE"""), {
        "id": candidate["id"], "account": account_id,
    }).mappings().first()
    if source is None or source["revision"] != candidate["revision"] or job is None:
        raise _SourceChanged()
    return source, job


def _attach_once(db, store, account_id, batch_id, item_id, content, content_hash, file_format, created_keys):
    owner_lock(db, account_id)
    batch = db.execute(text("""SELECT state FROM import_batches
            WHERE id=:batch AND account_id=:account FOR UPDATE"""), {
            "batch": batch_id, "account": account_id,
        }).mappings().first()
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    item = db.execute(text("""SELECT id,format,status,content_hash,source_id
            FROM import_batch_items WHERE id=:item AND batch_id=:batch AND account_id=:account
            FOR UPDATE"""), {"item": item_id, "batch": batch_id, "account": account_id}).mappings().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Import batch item not found.")
    if item["status"] == "deleted":
        raise HTTPException(status_code=409, detail="Deleted batch items cannot be uploaded again.")
    if item["format"] != file_format:
        raise HTTPException(status_code=422, detail="File does not match the batch item's format.")
    if item["content_hash"] is not None:
        if item["content_hash"] != content_hash:
            raise HTTPException(status_code=409, detail="Batch item is already pinned to different bytes.")
        candidate = db.execute(text("""SELECT id,revision FROM activity_sources
                WHERE id=:id AND account_id=:account"""), {
                "id": item["source_id"], "account": account_id,
            }).mappings().first()
        if candidate is None:
            raise HTTPException(status_code=409, detail="Deleted batch items cannot be uploaded again.")
        _lock_source(db, account_id, candidate)
        return False
    if batch["state"] != "open":
        raise HTTPException(status_code=409, detail="Batch is stopped.")
    object_key = store.write(content, extension=file_format, owner_id=account_id)
    created_keys.append(object_key)
    source = db.execute(text("""INSERT INTO activity_sources
            (account_id,source_kind,content_hash,revision,private_object_key,status)
            VALUES (:account,:kind,:hash,1,:object,'queued')
            ON CONFLICT (account_id,source_kind,content_hash) DO NOTHING
            RETURNING id,revision,status"""), {
            "account": account_id, "kind": file_format, "hash": content_hash, "object": object_key,
        }).mappings().first()
    duplicate = source is None
    if source is None:
        source = db.execute(text("""SELECT id,revision,status FROM activity_sources
                WHERE account_id=:account AND source_kind=:kind AND content_hash=:hash"""), {
                "account": account_id, "kind": file_format, "hash": content_hash,
            }).mappings().first()
        if source is None:
            raise _SourceChanged()
        source, job = _lock_source(db, account_id, source)
        store.delete(object_key, owner_id=account_id)
    else:
        job = enqueue(db, account_id=account_id, kind="process_upload",
                dedupe_key=f"upload:{source['id']}:r{source['revision']}",
                payload={"source_id": source["id"], "revision": source["revision"]}, priority=100)
    db.execute(text("""UPDATE import_batch_items SET content_hash=:hash,source_id=:source,
                duplicate=:duplicate,status=:status,error_code=NULL,updated_at=clock_timestamp()
            WHERE id=:item AND batch_id=:batch AND account_id=:account"""), {
            "hash": content_hash, "source": source["id"], "duplicate": duplicate,
            "status": "queued",
            "item": item_id, "batch": batch_id, "account": account_id,
        })
    return not duplicate


def _attach_file(engine: Engine, store: LocalObjectStore, account_id: str, batch_id: int,
                 item_id: int, content: bytes, file_format: str) -> None:
    content_hash = hashlib.sha256(content).hexdigest()
    for _ in range(3):
        created_keys = []
        try:
            with _transaction(engine) as db:
                _attach_once(db, store, account_id, batch_id, item_id, content, content_hash,
                             file_format, created_keys)
            return
        except _SourceChanged:
            for key in created_keys:
                cleanup_unreferenced_original(engine, store, account_id, key)
            continue
        except Exception:
            for key in created_keys:
                try:
                    with engine.connect() as db:
                        retained = db.execute(text("SELECT 1 FROM activity_sources WHERE account_id=:account AND private_object_key=:key"), {
                            "account": account_id, "key": key,
                        }).first()
                    if retained is None:
                        cleanup_unreferenced_original(engine, store, account_id, key)
                except Exception:
                    # Keep the sidecar as a durable orphan discovery record.
                    pass
            raise
    raise HTTPException(status_code=503, detail="Upload changed during submission; retry the request.")


def _retry_upload(engine, account_id, source_id):
    for _ in range(3):
        try:
            with _transaction(engine) as db:
                owner_lock(db, account_id)
                candidate = db.execute(text("""SELECT id,revision FROM activity_sources
                    WHERE id=:id AND account_id=:account"""), {
                    "id": source_id, "account": account_id,
                }).mappings().first()
                if candidate is None:
                    raise HTTPException(status_code=404, detail="Upload not found.")
                source, job = _lock_source(db, account_id, candidate)
                if source["activity_id"] is not None or source["status"] == "succeeded":
                    raise HTTPException(status_code=409, detail="Upload already succeeded.")
                if job["status"] in {"queued", "running"}:
                    return
                if job["status"] != "failed":
                    raise HTTPException(status_code=409, detail="Only failed imports can be retried.")
                db.execute(text("""UPDATE jobs SET lease_token=NULL,leased_until=NULL,updated_at=clock_timestamp()
                    WHERE id=:id"""), {"id": job["id"]})
                revision = source["revision"] + 1
                db.execute(text("""UPDATE activity_sources SET revision=:revision,status='queued',
                    last_error=NULL,updated_at=clock_timestamp() WHERE id=:id AND account_id=:account"""), {
                    "revision": revision, "id": source_id, "account": account_id,
                })
                enqueue(db, account_id=account_id, kind="process_upload", dedupe_key=f"upload:{source_id}:r{revision}",
                        payload={"source_id": source_id, "revision": revision}, priority=100)
            return
        except _SourceChanged:
            continue
    raise HTTPException(status_code=503, detail="Upload changed during retry; retry the request.")


def create_import_batch_router(engine: Engine, current_user: Callable[..., str],
                              store: LocalObjectStore | None = None) -> APIRouter:
    router = APIRouter()

    def get_store():
        return store or LocalObjectStore()

    @router.post("/api/import-batches", status_code=201, response_model=ImportBatchResponse)
    def create_batch(body: ImportBatchCreate, account_id: str = Depends(current_user)):
        files = [{"name": _manifest_name(file.name), "format": file.format} for file in body.files]
        encoded = json.dumps([file.model_dump() for file in body.files], separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with engine.begin() as db:
            owner_lock(db, account_id)
            row = db.execute(text("""INSERT INTO import_batches(account_id,request_id,manifest_hash)
                VALUES (:account,:request,:hash) ON CONFLICT (account_id,request_id) DO NOTHING
                RETURNING id"""), {"account": account_id, "request": str(body.request_id), "hash": digest}).first()
            if row is None:
                row = db.execute(text("""SELECT id,manifest_hash FROM import_batches
                    WHERE account_id=:account AND request_id=:request FOR UPDATE"""), {
                    "account": account_id, "request": str(body.request_id),
                }).mappings().one()
                if row["manifest_hash"] != digest:
                    raise HTTPException(status_code=409, detail="Request ID already has a different manifest.")
                batch_id = row["id"]
            else:
                batch_id = row[0]
                for ordinal, file in enumerate(files):
                    db.execute(text("""INSERT INTO import_batch_items
                        (account_id,batch_id,ordinal,name,format) VALUES (:account,:batch,:ordinal,:name,:format)"""), {
                        "account": account_id, "batch": batch_id, "ordinal": ordinal,
                        "name": file["name"], "format": file["format"],
                    })
        with engine.connect() as db:
            return _detail(db, account_id, batch_id)

    @router.get("/api/import-batches", response_model=ImportBatchPage)
    def list_batches(page: int = Query(1, ge=1, le=MAX_BIGINT // 20), page_size: int = Query(20, ge=1, le=20),
                     account_id: str = Depends(current_user)):
        with engine.connect() as db:
            total = db.execute(text("SELECT count(*) FROM import_batches WHERE account_id=:account"),
                               {"account": account_id}).scalar_one()
            rows = db.execute(text("""SELECT id FROM import_batches WHERE account_id=:account
                ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"""), {
                "account": account_id, "limit": page_size, "offset": (page - 1) * page_size,
            }).scalars().all()
            return {"items": [_detail(db, account_id, batch_id) for batch_id in rows],
                    "page": page, "page_size": page_size, "total": total}

    @router.get("/api/import-batches/{batch_id}", response_model=ImportBatchResponse)
    def get_batch(batch_id: int = Path(ge=1, le=MAX_BIGINT), account_id: str = Depends(current_user)):
        with engine.connect() as db:
            return _detail(db, account_id, batch_id)

    @router.post("/api/import-batches/{batch_id}/items/{item_id}/upload", status_code=202,
                 response_model=ImportBatchItemResponse, openapi_extra=MULTIPART_BODY)
    async def upload_item(request: Request, batch_id: int = Path(ge=1, le=MAX_BIGINT),
                          item_id: int = Path(ge=1, le=MAX_BIGINT),
                          account_id: str = Depends(current_user)):
        with engine.connect() as db:
            item = db.execute(text("""SELECT format FROM import_batch_items
                WHERE id=:item AND batch_id=:batch AND account_id=:account"""), {
                "item": item_id, "batch": batch_id, "account": account_id,
            }).scalar_one_or_none()
        if item is None:
            # Also hide a batch owned by another account.
            with engine.connect() as db:
                owned = db.execute(text("SELECT 1 FROM import_batches WHERE id=:batch AND account_id=:account"),
                                   {"batch": batch_id, "account": account_id}).first()
            if owned is None:
                raise HTTPException(status_code=404, detail="Import batch not found.")
            raise HTTPException(status_code=404, detail="Import batch item not found.")
        content, actual_format = await _one_activity_file(request, expected_format=item)
        await run_in_threadpool(_attach_file, engine, get_store(), account_id, batch_id, item_id,
                                content, actual_format)
        with engine.connect() as db:
            detail = _detail(db, account_id, batch_id)
        return next(item for item in detail["items"] if item["id"] == str(item_id))

    def set_state(batch_id: int, account_id: str, state: str):
        with engine.begin() as db:
            owner_lock(db, account_id)
            result = db.execute(text("""UPDATE import_batches SET state=:state,updated_at=clock_timestamp()
                WHERE id=:id AND account_id=:account"""), {
                "state": state, "id": batch_id, "account": account_id,
            })
            if result.rowcount != 1:
                raise HTTPException(status_code=404, detail="Import batch not found.")

    @router.post("/api/import-batches/{batch_id}/stop", status_code=204)
    def stop_batch(batch_id: int = Path(ge=1, le=MAX_BIGINT), account_id: str = Depends(current_user)):
        set_state(batch_id, account_id, "stopped")
        return Response(status_code=204)

    @router.post("/api/import-batches/{batch_id}/resume", status_code=204)
    def resume_batch(batch_id: int = Path(ge=1, le=MAX_BIGINT), account_id: str = Depends(current_user)):
        set_state(batch_id, account_id, "open")
        return Response(status_code=204)

    @router.post("/api/uploads/{source_id}/retry", status_code=204)
    def retry_upload(source_id: int = Path(ge=1, le=MAX_BIGINT), account_id: str = Depends(current_user)):
        _retry_upload(engine, account_id, source_id)
        return Response(status_code=204)

    return router
