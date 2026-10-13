"""Owner-scoped durable manifests spanning multiple ordinary import batches."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Engine, text
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.app.import_batches import ImportBatchCounts, ImportBatchPage, _detail, _manifest_name
from backend.app.owner_guard import owner_lock


MAX_HISTORY_FILES = 1000
MAX_HISTORY_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CHILD_BATCHES = 50
MAX_BIGINT = 9_223_372_036_854_775_807
MAX_CREATE_BODY_BYTES = 4 * 1024 * 1024
class ImportHistoryFile(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    format: Literal["gpx", "fit"]
    size: int = Field(strict=True, ge=1, le=MAX_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name", mode="before")
    @classmethod
    def basename_only(cls, value):
        if (not isinstance(value, str) or any(c in value for c in ("/", "\\"))
                or any(not c.isprintable() for c in value)
                or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ValueError("File name must be a printable basename.")
        normalized = _manifest_name(value)
        if normalized != value:
            raise ValueError("File name must be a printable basename.")
        return value


class ImportHistoryCreate(BaseModel):
    request_id: UUID
    label: str = Field(min_length=1, max_length=200)
    files: list[ImportHistoryFile] = Field(min_length=1, max_length=MAX_HISTORY_FILES)

    @field_validator("label", mode="before")
    @classmethod
    def basename_label(cls, value):
        if (not isinstance(value, str) or any(c in value for c in ("/", "\\"))
                or any(not c.isprintable() for c in value)
                or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ValueError("History label must be a printable basename.")
        normalized = _manifest_name(value)
        if normalized != value:
            raise ValueError("History label must be a printable basename.")
        return value

    @model_validator(mode="after")
    def bounded_total(self):
        if sum(file.size for file in self.files) > MAX_HISTORY_BYTES:
            raise ValueError("Combined activity files exceed the 256 MiB history limit.")
        return self


class ImportHistoryState(BaseModel):
    id: str
    label: str
    state: Literal["open", "stopped"]
    created_at: datetime
    file_count: int
    batch_ids: list[str] = Field(max_length=MAX_CHILD_BATCHES)
    counts: ImportBatchCounts
    duplicate_count: int


class ImportHistoryPage(BaseModel):
    items: list[ImportHistoryState]
    page: int
    page_size: int
    total: int


class BoundedHistoryRoute(APIRoute):
    """Read only the bounded manifest body before FastAPI parses its JSON model."""

    def get_route_handler(self):
        downstream = super().get_route_handler()

        async def bounded(request: Request):
            if request.method == "POST" and request.url.path == "/api/import-histories":
                length = request.headers.get("content-length")
                if length is not None:
                    try:
                        if int(length) > MAX_CREATE_BODY_BYTES:
                            return JSONResponse(status_code=413, content={"detail": "History manifest is too large."})
                    except ValueError:
                        return JSONResponse(status_code=400, content={"detail": "Invalid request body."})
                chunks = []
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_CREATE_BODY_BYTES:
                        return JSONResponse(status_code=413, content={"detail": "History manifest is too large."})
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            return await downstream(request)

        return bounded


def _manifest_payload(body: ImportHistoryCreate) -> dict:
    return {"label": body.label, "files": [item.model_dump() for item in body.files]}


@contextmanager
def _snapshot(engine: Engine):
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
        with db.begin():
            yield db


def _history_detail(db, account_id: str, history_id: int) -> dict:
    history = db.execute(text("""SELECT id,label,state,created_at FROM import_histories
        WHERE id=:id AND account_id=:account"""), {
        "id": history_id, "account": account_id,
    }).mappings().first()
    if history is None:
        raise HTTPException(status_code=404, detail="Import history not found.")
    batches = db.execute(text("""SELECT id FROM import_batches
        WHERE account_id=:account AND history_id=:history ORDER BY history_ordinal"""), {
        "account": account_id, "history": history_id,
    }).scalars().all()
    totals = db.execute(text("""SELECT count(*) FILTER (WHERE item_status IS NOT NULL) AS file_count,
            count(*) FILTER (WHERE duplicate) AS duplicate_count,
            count(*) FILTER (WHERE item_status='awaiting_upload') AS awaiting_upload,
            count(*) FILTER (WHERE item_status='queued') AS queued,
            count(*) FILTER (WHERE item_status='processing') AS processing,
            count(*) FILTER (WHERE item_status='succeeded') AS succeeded,
            count(*) FILTER (WHERE item_status='failed') AS failed,
            count(*) FILTER (WHERE item_status='deleted') AS deleted
        FROM (
            SELECT item.id,item.duplicate,
                CASE
                    WHEN item.status='deleted' THEN 'deleted'
                    WHEN item.status='awaiting_upload' THEN 'awaiting_upload'
                    WHEN item.source_id IS NOT NULL AND source.id IS NULL THEN 'deleted'
                    WHEN source.status='succeeded' THEN 'succeeded'
                    WHEN job.status='running' THEN 'processing'
                    WHEN job.status='queued' THEN 'queued'
                    WHEN job.status='failed' THEN 'failed'
                    WHEN job.status='cancelled' THEN 'failed'
                    ELSE COALESCE(source.status,item.status)
                END AS item_status
            FROM import_batches batch JOIN import_batch_items item
              ON item.account_id=batch.account_id AND item.batch_id=batch.id
            LEFT JOIN activity_sources source ON source.account_id=item.account_id AND source.id=item.source_id
            LEFT JOIN jobs job ON job.account_id=source.account_id AND job.kind='process_upload'
              AND job.dedupe_key=concat('upload:',source.id::text,chr(58),'r',source.revision::text)
            WHERE batch.account_id=:account AND batch.history_id=:history
        ) status_rows"""), {"account": account_id, "history": history_id}).mappings().one()
    counts = {key: int(totals[key]) for key in
              ("awaiting_upload", "queued", "processing", "succeeded", "failed", "deleted")}
    return {"id": str(history["id"]), "label": history["label"], "state": history["state"],
            "created_at": history["created_at"], "file_count": int(totals["file_count"]),
            "batch_ids": [str(value) for value in batches], "counts": counts,
            "duplicate_count": int(totals["duplicate_count"])}


def create_import_histories_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter(route_class=BoundedHistoryRoute)

    @router.post("/api/import-histories", status_code=201, response_model=ImportHistoryState)
    def create_history(body: ImportHistoryCreate, account_id: str = Depends(current_user)):
        payload = _manifest_payload(body)
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                           separators=(",", ":")).encode("ascii")).hexdigest()
        new_id = None
        with engine.begin() as db:
            owner_lock(db, account_id)
            row = db.execute(text("""INSERT INTO import_histories(account_id,request_id,manifest_hash,label)
                VALUES (:account,:request,:hash,:label) ON CONFLICT (account_id,request_id) DO NOTHING
                RETURNING id"""), {"account": account_id, "request": str(body.request_id),
                                     "hash": digest, "label": body.label}).first()
            if row is None:
                row = db.execute(text("""SELECT id,manifest_hash FROM import_histories
                    WHERE account_id=:account AND request_id=:request FOR UPDATE"""), {
                    "account": account_id, "request": str(body.request_id),
                }).mappings().one()
                if row["manifest_hash"] != digest:
                    raise HTTPException(status_code=409, detail="Request ID already has a different history manifest.")
                history_id = int(row["id"])
            else:
                history_id = int(row[0])
                for batch_ordinal, start in enumerate(range(0, len(body.files), 20)):
                    files = body.files[start:start + 20]
                    batch_payload = [{"name": item.name, "format": item.format} for item in files]
                    batch_hash = hashlib.sha256(json.dumps(batch_payload, ensure_ascii=True,
                        separators=(",", ":")).encode("ascii")).hexdigest()
                    batch_id = db.execute(text("""INSERT INTO import_batches
                        (account_id,request_id,manifest_hash,history_id,history_ordinal)
                        VALUES (:account,:request,:hash,:history,:ordinal) RETURNING id"""), {
                        "account": account_id, "request": str(uuid.uuid4()), "hash": batch_hash,
                        "history": history_id, "ordinal": batch_ordinal,
                    }).scalar_one()
                    for item_ordinal, item in enumerate(files):
                        db.execute(text("""INSERT INTO import_batch_items
                            (account_id,batch_id,ordinal,name,format,expected_sha256,expected_size)
                            VALUES (:account,:batch,:ordinal,:name,:format,:sha256,:size)"""), {
                            "account": account_id, "batch": batch_id, "ordinal": item_ordinal,
                            "name": item.name, "format": item.format, "sha256": item.sha256,
                            "size": item.size,
                        })
            new_id = history_id
        with _snapshot(engine) as db:
            return _history_detail(db, account_id, new_id)

    @router.get("/api/import-histories", response_model=ImportHistoryPage)
    def list_histories(page: int = Query(1, ge=1, le=MAX_BIGINT // 20),
                       page_size: int = Query(20, ge=1, le=20),
                       account_id: str = Depends(current_user)):
        with _snapshot(engine) as db:
            total = db.execute(text("SELECT count(*) FROM import_histories WHERE account_id=:account"), {
                "account": account_id,
            }).scalar_one()
            ids = db.execute(text("""SELECT id FROM import_histories WHERE account_id=:account
                ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"""), {
                "account": account_id, "limit": page_size, "offset": (page - 1) * page_size,
            }).scalars().all()
            return {"items": [_history_detail(db, account_id, int(value)) for value in ids],
                    "page": page, "page_size": page_size, "total": int(total)}

    @router.get("/api/import-histories/{history_id}", response_model=ImportHistoryState)
    def get_history(history_id: int = Path(ge=1, le=MAX_BIGINT),
                    account_id: str = Depends(current_user)):
        with _snapshot(engine) as db:
            return _history_detail(db, account_id, history_id)

    @router.get("/api/import-histories/{history_id}/batches", response_model=ImportBatchPage)
    def list_history_batches(history_id: int = Path(ge=1, le=MAX_BIGINT),
                             page: int = Query(1, ge=1, le=MAX_BIGINT // 20),
                             page_size: int = Query(20, ge=1, le=20),
                             account_id: str = Depends(current_user)):
        offset = (page - 1) * page_size
        with _snapshot(engine) as db:
            if db.execute(text("SELECT 1 FROM import_histories WHERE id=:id AND account_id=:account"), {
                "id": history_id, "account": account_id,
            }).first() is None:
                raise HTTPException(status_code=404, detail="Import history not found.")
            total = db.execute(text("SELECT count(*) FROM import_batches WHERE history_id=:id AND account_id=:account"), {
                "id": history_id, "account": account_id,
            }).scalar_one()
            ids = db.execute(text("""SELECT id FROM import_batches WHERE history_id=:id AND account_id=:account
                ORDER BY history_ordinal LIMIT :limit OFFSET :offset"""), {
                "id": history_id, "account": account_id, "limit": page_size, "offset": offset,
            }).scalars().all()
            return {"items": [_detail(db, account_id, int(value)) for value in ids],
                    "page": page, "page_size": page_size, "total": int(total)}

    def set_state(history_id: int, account_id: str, state: str):
        with engine.begin() as db:
            owner_lock(db, account_id)
            history = db.execute(text("""SELECT id FROM import_histories
                WHERE id=:id AND account_id=:account FOR UPDATE"""), {
                "id": history_id, "account": account_id,
            }).first()
            if history is None:
                raise HTTPException(status_code=404, detail="Import history not found.")
            db.execute(text("UPDATE import_histories SET state=:state,updated_at=clock_timestamp() WHERE id=:id"), {
                "state": state, "id": history_id,
            })
            child_ids = db.execute(text("""SELECT id FROM import_batches
                WHERE account_id=:account AND history_id=:id ORDER BY history_ordinal FOR UPDATE"""), {
                "account": account_id, "id": history_id,
            }).scalars().all()
            for batch_id in child_ids:
                db.execute(text("""UPDATE import_batches SET state=:state,updated_at=clock_timestamp()
                    WHERE account_id=:account AND id=:id"""), {
                    "state": state, "account": account_id, "id": batch_id,
                })

    @router.post("/api/import-histories/{history_id}/stop", status_code=204)
    def stop_history(history_id: int = Path(ge=1, le=MAX_BIGINT),
                     account_id: str = Depends(current_user)):
        set_state(history_id, account_id, "stopped")

    @router.post("/api/import-histories/{history_id}/resume", status_code=204)
    def resume_history(history_id: int = Path(ge=1, le=MAX_BIGINT),
                       account_id: str = Depends(current_user)):
        set_state(history_id, account_id, "open")

    return router
