"""Account export, deletion receipts and durable cleanup orchestration."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from datetime import date, datetime, timezone
from typing import BinaryIO, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from backend.app.account_deletion_journal import (
    JournalError,
    append_deletion,
    configured_path,
    read_records,
    read_records_durable,
)
from backend.app.owner_guard import exclusive_owner_snapshot, owner_lock
from backend.app.storage import LocalObjectStore

MAX_EXPORT_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
MAX_EXPORT_ARCHIVE = 512 * 1024 * 1024
MAX_EXPORT_ENTRIES = 25_000
MAX_EXPORT_SECONDS = 120
MAX_DELETION_KEYS = 100_000


class AccountDeletionRequest(BaseModel):
    confirmation: Literal["DELETE"]


class AccountDeletionResponse(BaseModel):
    id: str
    status: Literal["cleanup-pending", "complete"]


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(self, *args, close_file: BinaryIO, **kwargs):
        super().__init__(*args, **kwargs)
        self._close_file = close_file

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close_file.close()


def create_account_data_router(engine: Engine, current_user: Callable[..., str],
                               store: LocalObjectStore | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/account/export")
    def export_account(account_id: str = Depends(current_user)):
        archive = build_account_export(engine, account_id, store or LocalObjectStore())

        def stream() -> Iterator[bytes]:
            archive.seek(0)
            while block := archive.read(1024 * 1024):
                yield block

        return _ClosingStreamingResponse(stream(), close_file=archive, media_type="application/zip", headers={
            "Cache-Control": "no-store, private", "Content-Disposition": 'attachment; filename="city-runner-account.zip"',
        })

    @router.delete("/api/account", status_code=202, response_model=AccountDeletionResponse)
    def delete_account(body: AccountDeletionRequest, account_id: str = Depends(current_user)):
        return delete_account_data(engine, account_id, store or LocalObjectStore())

    return router


def build_account_export(engine: Engine, account_id: str, store: LocalObjectStore) -> BinaryIO:
    started = time.monotonic()
    deadline = started + MAX_EXPORT_SECONDS
    archive = tempfile.TemporaryFile(mode="w+b")
    try:
        with exclusive_owner_snapshot(engine, account_id,
                                      timeout_seconds=max(0.001, deadline - time.monotonic())) as db:
            manifest = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                        "account_id": account_id, "scope": "gpx-fit-account-data",
                        "entries": [], "counts": {}}
            uncompressed = 0
            entry_count = 0
            output = None

            def add_size(size: int) -> None:
                nonlocal uncompressed
                uncompressed += size
                if uncompressed > MAX_EXPORT_UNCOMPRESSED:
                    raise HTTPException(status_code=413, detail="Account export exceeds its size limit.")
                if output is not None and output.fp.tell() > MAX_EXPORT_ARCHIVE:
                    raise HTTPException(status_code=413, detail="Account export exceeds its size limit.")
                if time.monotonic() - started > MAX_EXPORT_SECONDS:
                    raise HTTPException(status_code=503, detail="Account export exceeded its time limit.")

            def add_entry() -> None:
                nonlocal entry_count
                entry_count += 1
                if entry_count > MAX_EXPORT_ENTRIES:
                    raise HTTPException(status_code=413, detail="Account export exceeds its entry limit.")

            def set_statement_deadline() -> None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HTTPException(status_code=503, detail="Account export exceeded its time limit.")
                db.execute(text("SELECT set_config('statement_timeout', :timeout, true)"), {
                    "timeout": f"{max(1, int(remaining * 1000))}ms",
                }).scalar_one()

            def json_entry(path: str, query: str, params: dict, *, single: bool = False,
                           batch_size: int = 100) -> int:
                nonlocal entry_count
                add_entry()
                count = 0
                with output.open(path, "w") as target:
                    add_size(2)
                    target.write(b"[")
                    set_statement_deadline()
                    result = db.execute(text(query), params,
                                        execution_options={"stream_results": True, "yield_per": batch_size})
                    cursor = result.cursor
                    try:
                        while True:
                            set_statement_deadline()
                            rows = result.mappings().fetchmany(batch_size)
                            if not rows:
                                break
                            for row in rows:
                                encoded = json.dumps(_jsonable_row(dict(row)), ensure_ascii=False,
                                                     separators=(",", ":"), allow_nan=False).encode("utf-8")
                                if count:
                                    target.write(b",")
                                target.write(encoded)
                                add_size(len(encoded) + int(count > 0))
                                count += 1
                                if single and count > 1:
                                    raise HTTPException(status_code=500, detail="Account export data is inconsistent.")
                    finally:
                        result.close()
                        cursor.close()
                    target.write(b"]")
                manifest["entries"].append(path)
                return count

            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                manifest["counts"]["account"] = json_entry("data/account.json", "SELECT id,created_at FROM accounts WHERE id=:account", {"account": account_id}, single=True)
                if manifest["counts"]["account"] != 1:
                    raise HTTPException(status_code=401, detail="Unauthenticated.")
                set_statement_deadline()
                source_result = db.execute(text("SELECT id,source_kind,private_object_key FROM activity_sources WHERE account_id=:account ORDER BY id LIMIT :limit"), {
                    "account": account_id, "limit": MAX_EXPORT_ENTRIES,
                }).mappings()
                try:
                    source_rows = source_result.fetchmany(MAX_EXPORT_ENTRIES)
                finally:
                    source_result.close()
                if len(source_rows) >= MAX_EXPORT_ENTRIES:
                    raise HTTPException(status_code=413, detail="Account export exceeds its entry limit.")
                source_files = []
                for source in source_rows:
                    kind = source["source_kind"]
                    if kind not in {"gpx", "fit"}:
                        raise HTTPException(status_code=409, detail="Account export contains an unsupported source format.")
                    if not source["private_object_key"]:
                        raise HTTPException(status_code=409, detail="An original activity file is unavailable for export.")
                    try:
                        content = store.read(source["private_object_key"], owner_id=account_id)
                    except (OSError, ValueError) as error:
                        raise HTTPException(status_code=409, detail="An original activity file is unavailable for export.") from error
                    name = f"originals/source-{source['id']}.{kind}"
                    add_entry()
                    _zip_write(output, name, content)
                    add_size(len(content))
                    manifest["entries"].append(name)
                    source_files.append({"source_id": str(source["id"]), "path": name})
                manifest["counts"]["original_files"] = len(source_files)

                queries = {
                    "activities": "SELECT id,name,date,activity_type,processed,unmapped_points,tracks,timestamps FROM activities WHERE user_id=:account ORDER BY id",
                    "sources": "SELECT id,activity_id,source_kind,source_connection_id,external_id,content_hash,revision,status,last_error,created_at,updated_at FROM activity_sources WHERE account_id=:account ORDER BY id",
                    "import_batches": "SELECT id,request_id,manifest_hash,state,created_at,updated_at FROM import_batches WHERE account_id=:account ORDER BY id",
                    "import_batch_items": "SELECT id,batch_id,ordinal,name,format,status,content_hash,source_id,duplicate,error_code,created_at,updated_at FROM import_batch_items WHERE account_id=:account ORDER BY batch_id,ordinal",
                    "source_node_contributions": "SELECT dataset_id,source_id,source_revision,node_id,created_at FROM source_node_contributions WHERE account_id=:account ORDER BY dataset_id,source_id,node_id",
                    "coverage_source_runs": "SELECT dataset_id,source_id,source_revision,job_id,status,sample_count,supported_sample_count,unsupported_sample_count,matched_node_count,last_error,updated_at FROM coverage_source_runs WHERE account_id=:account ORDER BY dataset_id,source_id",
                    "account_dataset_coverage": "SELECT dataset_id,status,pending_sources,failed_sources,visited_node_count,unsupported_sample_count,progress_revision,updated_at FROM account_dataset_coverage WHERE account_id=:account ORDER BY dataset_id",
                    "account_street_coverage": "SELECT dataset_id,street_id,visited_node_count,updated_at FROM account_street_coverage WHERE account_id=:account ORDER BY dataset_id,street_id",
                    "manual_street_completions": "SELECT dataset_id,street_id,reason,created_at,updated_at FROM manual_street_completions WHERE account_id=:account ORDER BY dataset_id,street_id",
                    "saved_routes": "SELECT id,name,revision,waypoints,ST_AsGeoJSON(geometry,17) AS geometry,distance_m,duration_s,provider,attribution,routed_at,created_at,updated_at FROM saved_routes WHERE account_id=:account ORDER BY id",
                }
                for name, query in queries.items():
                    manifest["counts"][name] = json_entry(f"data/{name}.json", query, {"account": account_id},
                                                            batch_size=1 if name == "activities" else 100)
                source_map_payload = json.dumps(_jsonable(source_files), ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8")
                add_entry()
                _zip_write(output, "data/source_originals.json", source_map_payload)
                add_size(len(source_map_payload))
                manifest["counts"]["source_originals"] = len(source_files)
                manifest["entries"].append("data/source_originals.json")
                manifest["entries"].append("manifest.json")
                manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                add_entry()
                _zip_write(output, "manifest.json", manifest_bytes)
                add_size(len(manifest_bytes))
        if archive.tell() > MAX_EXPORT_ARCHIVE:
            raise HTTPException(status_code=413, detail="Account export exceeds its size limit.")
        archive.seek(0)
        return archive
    except SQLAlchemyError as error:
        archive.close()
        raise HTTPException(status_code=503, detail="Account export is temporarily unavailable.") from error
    except BaseException:
        archive.close()
        raise


def _zip_write(output: zipfile.ZipFile, name: str, payload: bytes) -> None:
    if len(output.filelist) >= MAX_EXPORT_ENTRIES:
        raise HTTPException(status_code=413, detail="Account export exceeds its entry limit.")
    output.writestr(name, payload)


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _jsonable_row(row: dict):
    result = {}
    for key, value in row.items():
        if (key == "id" or key.endswith("_id")) and value is not None:
            result[key] = str(value)
        elif key == "geometry" and isinstance(value, str):
            result[key] = json.loads(value)
        else:
            result[key] = _jsonable(value)
    return result


def delete_account_data(engine: Engine, account_id: str, store: LocalObjectStore | None = None) -> AccountDeletionResponse:
    try:
        configured_path()
    except JournalError as error:
        raise HTTPException(status_code=503, detail="Account deletion requires a configured deletion ledger.") from error
    object_store = store or LocalObjectStore()
    with engine.begin() as db:
        identity_rows = db.execute(text("SELECT provider,subject FROM login_identities WHERE account_id=:account ORDER BY provider,subject"), {
            "account": account_id,
        }).all()
        if db.dialect.name == "postgresql":
            for provider, subject in identity_rows:
                db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {
                    "key": f"{provider}:{subject}",
                })
        owner_lock(db, account_id, exclusive=True)
        locked_identities = db.execute(text("""SELECT provider,subject FROM login_identities
            WHERE account_id=:account ORDER BY provider,subject FOR UPDATE"""), {
            "account": account_id,
        }).all()
        if locked_identities != identity_rows:
            raise HTTPException(status_code=409, detail="Account identity changed during deletion; retry.")
        account = db.execute(text("SELECT id FROM accounts WHERE id=:account FOR UPDATE"), {
            "account": account_id,
        }).first()
        if account is None:
            raise HTTPException(status_code=401, detail="Unauthenticated.")
        keys = set(object_store.list_owned_keys(account_id))
        keys.update(db.execute(text("SELECT private_object_key FROM activity_sources WHERE account_id=:account AND private_object_key IS NOT NULL"), {
            "account": account_id,
        }).scalars().all())
        keys.update(db.execute(text("""SELECT payload->>'object_key' FROM jobs
            WHERE account_id=:account AND kind='delete_private_object'
              AND status IN ('queued','running','failed') AND payload->>'object_key' IS NOT NULL"""), {
            "account": account_id,
        }).scalars().all())
        if len(keys) > MAX_DELETION_KEYS:
            raise HTTPException(status_code=413, detail="Account has too many original files to delete safely.")
        try:
            record = append_deletion(account_id, list(keys))
        except JournalError as error:
            raise HTTPException(status_code=503, detail="Account deletion ledger is unavailable.") from error
        deletion_id = uuid.UUID(record["deletion_id"])
        db.execute(text("""INSERT INTO account_deletions(id,account_id,requested_at,status)
            VALUES (:id,:account,:requested,'cleanup-pending') ON CONFLICT (account_id) DO NOTHING"""), {
            "id": deletion_id, "account": account_id,
            "requested": datetime.fromisoformat(record["requested_at"]),
        })
        receipt = db.execute(text("SELECT id,status FROM account_deletions WHERE account_id=:account FOR UPDATE"), {
            "account": account_id,
        }).mappings().one()
        deletion_id = receipt["id"]
        for key in record["object_keys"]:
            db.execute(text("""INSERT INTO account_deletion_outbox(deletion_id,account_id,object_key)
                VALUES (:deletion,:account,:key) ON CONFLICT (deletion_id,object_key) DO NOTHING"""), {
                "deletion": deletion_id, "account": account_id, "key": key,
            })
        # activities deliberately have no cascading owner FK.
        db.execute(text("DELETE FROM activities WHERE user_id=:account"), {"account": account_id})
        db.execute(text("DELETE FROM accounts WHERE id=:account"), {"account": account_id})
        pending = db.execute(text("SELECT 1 FROM account_deletion_outbox WHERE deletion_id=:id LIMIT 1"), {
            "id": deletion_id,
        }).first()
        if pending is None:
            db.execute(text("UPDATE account_deletions SET status='complete',completed_at=clock_timestamp() WHERE id=:id"), {
                "id": deletion_id,
            })
            status = "complete"
        else:
            status = "cleanup-pending"
        return AccountDeletionResponse(id=str(deletion_id), status=status)


def process_account_deletion_cleanup(engine: Engine, store: LocalObjectStore | None = None) -> bool:
    object_store = store or LocalObjectStore()
    token = uuid.uuid4()
    with engine.begin() as db:
        db.execute(text("""UPDATE account_deletion_outbox SET status='failed',lease_token=NULL,
            leased_until=NULL,last_error='cleanup_attempts_exhausted',updated_at=clock_timestamp()
            WHERE status='running' AND leased_until<=clock_timestamp() AND attempts>=max_attempts"""))
        row = db.execute(text("""WITH candidate AS (
                SELECT id FROM account_deletion_outbox
                WHERE (status='queued' AND available_at<=clock_timestamp())
                   OR (status='running' AND leased_until<=clock_timestamp() AND attempts<max_attempts)
                ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE account_deletion_outbox outbox SET status='running',lease_token=:token,
                leased_until=clock_timestamp()+interval '60 seconds',attempts=LEAST(attempts+1,max_attempts),updated_at=clock_timestamp()
              FROM candidate WHERE outbox.id=candidate.id RETURNING outbox.*"""), {"token": token}).mappings().first()
    if row is None:
        return False
    try:
        with engine.begin() as db:
            owner_lock(db, row["account_id"], exclusive=True, require_account=False, allow_deleted=True)
            object_store.delete(row["object_key"], owner_id=row["account_id"])
            changed = db.execute(text("""UPDATE account_deletion_outbox SET status='complete',lease_token=NULL,
                leased_until=NULL,last_error=NULL,updated_at=clock_timestamp()
                WHERE id=:id AND status='running' AND lease_token=:token
                  AND leased_until>clock_timestamp()"""), {
                "id": row["id"], "token": token,
            }).rowcount
            if changed != 1:
                return False
            db.execute(text("""UPDATE account_deletions SET status='complete',completed_at=clock_timestamp()
                WHERE id=:deletion AND NOT EXISTS (
                    SELECT 1 FROM account_deletion_outbox WHERE deletion_id=:deletion AND status<>'complete'
                )"""), {"deletion": row["deletion_id"]})
        return True
    except Exception as error:
        with engine.begin() as db:
            db.execute(text("""UPDATE account_deletion_outbox SET
                status=CASE WHEN attempts>=max_attempts THEN 'failed' ELSE 'queued' END,
                lease_token=NULL,leased_until=NULL,last_error='object_cleanup_failed',
                available_at=clock_timestamp()+make_interval(secs=>LEAST(300,1<<LEAST(attempts,8))),
                updated_at=clock_timestamp() WHERE id=:id AND status='running' AND lease_token=:token
                  AND leased_until>clock_timestamp()"""), {
                "id": row["id"], "token": token,
            })
        return True


def replay_account_deletions(engine: Engine, store: LocalObjectStore | None = None, *,
                            force_cleanup: bool = False, require_ledger: bool = True) -> int:
    records = read_records_durable(required=require_ledger)
    object_store = store or LocalObjectStore()
    replayed = 0
    for record in records:
        account_id = record["account_id"]
        with engine.begin() as db:
            if db.dialect.name == "postgresql":
                identities = db.execute(text("SELECT provider,subject FROM login_identities WHERE account_id=:account ORDER BY provider,subject"), {
                    "account": account_id,
                }).all()
                for provider, subject in identities:
                    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"{provider}:{subject}"})
            owner_lock(db, account_id, exclusive=True, require_account=False, allow_deleted=True)
            keys = set(record["object_keys"])
            keys.update(object_store.list_owned_keys(account_id))
            keys.update(db.execute(text("SELECT private_object_key FROM activity_sources WHERE account_id=:account AND private_object_key IS NOT NULL"), {
                "account": account_id,
            }).scalars().all())
            keys.update(db.execute(text("SELECT payload->>'object_key' FROM jobs WHERE account_id=:account AND kind='delete_private_object' AND payload->>'object_key' IS NOT NULL"), {
                "account": account_id,
            }).scalars().all())
            requested = datetime.fromisoformat(record["requested_at"])
            db.execute(text("""INSERT INTO account_deletions(id,account_id,requested_at,status)
                VALUES (:id,:account,:requested,'cleanup-pending') ON CONFLICT(account_id) DO NOTHING"""), {
                "id": record["deletion_id"], "account": account_id, "requested": requested,
            })
            deletion_id = db.execute(text("SELECT id FROM account_deletions WHERE account_id=:account"), {
                "account": account_id,
            }).scalar_one()
            for key in keys:
                conflict = ("ON CONFLICT (deletion_id,object_key) DO UPDATE SET status='queued',attempts=0,"
                            "available_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,last_error=NULL,"
                            "updated_at=clock_timestamp()" if force_cleanup else "ON CONFLICT DO NOTHING")
                db.execute(text(f"""INSERT INTO account_deletion_outbox(deletion_id,account_id,object_key)
                    VALUES (:deletion,:account,:key) {conflict}"""), {
                "deletion": deletion_id, "account": account_id, "key": key,
            })
            db.execute(text("UPDATE account_deletions SET status='cleanup-pending',completed_at=NULL WHERE id=:id"), {
                "id": deletion_id,
            })
            db.execute(text("DELETE FROM activities WHERE user_id=:account"), {"account": account_id})
            db.execute(text("DELETE FROM accounts WHERE id=:account"), {"account": account_id})
            db.execute(text("""UPDATE account_deletions SET status='complete',completed_at=clock_timestamp()
                WHERE id=:id AND NOT EXISTS (
                    SELECT 1 FROM account_deletion_outbox WHERE deletion_id=:id AND status<>'complete'
                )"""), {"id": deletion_id})
        replayed += 1
    return replayed


def retry_account_deletion_cleanup(engine: Engine, deletion_id: str | None = None) -> int:
    with engine.begin() as db:
        where = "status='failed'"
        params = {}
        if deletion_id is not None:
            where += " AND deletion_id=:deletion"
            params["deletion"] = deletion_id
        result = db.execute(text(f"""UPDATE account_deletion_outbox SET status='queued',attempts=0,
            available_at=clock_timestamp(),last_error=NULL,updated_at=clock_timestamp()
            WHERE {where} RETURNING deletion_id"""), params)
        queued = result.rowcount
        deletion_ids = set(result.scalars().all())
        for item_id in deletion_ids:
            db.execute(text("UPDATE account_deletions SET status='cleanup-pending',completed_at=NULL WHERE id=:id"), {
                "id": item_id,
            })
        return queued


def main() -> None:
    import argparse
    from sqlalchemy import create_engine

    parser = argparse.ArgumentParser(description="Account deletion ledger and cleanup operator tools")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--init-ledger", action="store_true")
    actions.add_argument("--replay", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--retry", metavar="DELETION_ID", nargs="?", const="all")
    actions.add_argument("--drain", action="store_true")
    actions.add_argument("--restore", action="store_true",
                         help="force journal replay, drain cleanup, and verify every tombstoned owner is absent")
    args = parser.parse_args()
    if args.init_ledger:
        from backend.app.account_deletion_journal import initialize_ledger
        print(json.dumps({"ledger": str(initialize_ledger())}))
        return
    engine = create_engine(os.getenv("DATABASE_URL", "postgresql+psycopg:///activities"), pool_pre_ping=True)
    try:
        if args.replay:
            print(json.dumps({"replayed": replay_account_deletions(engine, force_cleanup=True)}))
        elif args.restore:
            count = replay_account_deletions(engine, force_cleanup=True)
            processed = 0
            while process_account_deletion_cleanup(engine):
                processed += 1
            if not verify_account_deletions(engine):
                raise SystemExit("account deletion restore verification failed")
            print(json.dumps({"replayed": count, "cleaned": processed, "verified": True}))
        elif args.status:
            with engine.connect() as db:
                rows = db.execute(text("""SELECT id,status,requested_at,completed_at,
                    (SELECT count(*) FROM account_deletion_outbox item WHERE item.deletion_id=receipt.id
                        AND item.status<>'complete') AS pending,
                    (SELECT count(*) FROM account_deletion_outbox item WHERE item.deletion_id=receipt.id
                        AND item.status='failed') AS failed
                    FROM account_deletions receipt ORDER BY requested_at DESC""")).mappings().all()
            print(json.dumps(_jsonable([dict(row) for row in rows]), separators=(",", ":")))
        elif args.retry is not None:
            count = retry_account_deletion_cleanup(engine, None if args.retry == "all" else args.retry)
            print(json.dumps({"queued": count}))
        else:
            processed = 0
            while process_account_deletion_cleanup(engine):
                processed += 1
            print(json.dumps({"processed": processed}))
    finally:
        engine.dispose()


def verify_account_deletions(engine: Engine, store: LocalObjectStore | None = None) -> bool:
    object_store = store or LocalObjectStore()
    records = read_records()
    with engine.connect() as db:
        for record in records:
            account_id = record["account_id"]
            exists = db.execute(text("""SELECT
                EXISTS(SELECT 1 FROM accounts WHERE id=:account) OR
                EXISTS(SELECT 1 FROM activities WHERE user_id=:account) OR
                EXISTS(SELECT 1 FROM activity_sources WHERE account_id=:account) OR
                EXISTS(SELECT 1 FROM jobs WHERE account_id=:account) OR
                EXISTS(SELECT 1 FROM import_batches WHERE account_id=:account) OR
                EXISTS(SELECT 1 FROM import_batch_items WHERE account_id=:account) OR
                EXISTS(SELECT 1 FROM manual_street_completions WHERE account_id=:account) OR
                EXISTS(SELECT 1 FROM saved_routes WHERE account_id=:account) OR
                EXISTS(SELECT 1 FROM account_dataset_coverage WHERE account_id=:account) OR
                EXISTS(SELECT 1 FROM account_street_coverage WHERE account_id=:account)"""), {
                "account": account_id,
            }).scalar_one()
            pending = db.execute(text("""SELECT EXISTS(SELECT 1 FROM account_deletion_outbox
                WHERE account_id=:account AND status<>'complete')"""), {"account": account_id}).scalar_one()
            receipt = db.execute(text("SELECT status FROM account_deletions WHERE account_id=:account"), {
                "account": account_id,
            }).scalar_one_or_none()
            if exists or pending or receipt != "complete" or object_store.list_owned_keys(account_id):
                return False
            for key in record["object_keys"]:
                if object_store._path(key).exists() or (object_store._owner_dir(account_id) / key).exists():
                    return False
    return True


if __name__ == "__main__":
    main()
