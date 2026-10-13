"""Durable operator-supplied OSM import and active-dataset backfill tasks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from backend.app.owner_guard import owner_lock

MAX_OSM_XML_BYTES = 64 * 1024 * 1024
MAX_IMPORT_CITIES = 50
MAX_BACKFILL_SOURCES = 20
MAX_TASK_ATTEMPTS = 5
TASK_LEASE_SECONDS = 3600
_OBJECT_KEY = re.compile(r"[0-9a-f]{32}-[0-9a-f]{12}\.osm\Z")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
ACTIVE_DATASETS_LOCK_KEY = "city-runner:active-dataset-enumeration"
ACTIVE_DATASETS_LOCK_SEED = 11012


def _storage_root(root: str | os.PathLike[str] | None = None) -> Path:
    value = root or os.getenv("MAP_IMPORT_STORAGE_ROOT")
    if not value:
        raise RuntimeError("map_import_storage_not_configured")
    path = Path(value).expanduser()
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _fsync_dir(directory.parent)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError("invalid_map_import_storage")
    os.chmod(path, 0o700)
    return path


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _snapshot(source_path: str | os.PathLike[str], root: Path) -> tuple[str, str]:
    temporary_key = f".{uuid.uuid4().hex}.tmp"
    temporary = root / temporary_key
    destination: Path | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output, Path(source_path).open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("osm_source_must_be_regular_file")
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > MAX_OSM_XML_BYTES:
                    raise ValueError("osm_xml_too_large")
                digest.update(block)
                output.write(block)
            if size == 0:
                raise ValueError("osm_xml_empty")
            output.flush()
            os.fsync(output.fileno())
        checksum = digest.hexdigest()
        key = f"{uuid.uuid4().hex}-{checksum[:12]}.osm"
        destination = root / key
        os.rename(temporary, destination)
        _fsync_dir(root)
        return key, checksum
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if destination is not None:
            try:
                destination.unlink()
                _fsync_dir(root)
            except FileNotFoundError:
                pass
        raise


def _object_path(root: Path, key: str) -> Path:
    if not _OBJECT_KEY.fullmatch(key):
        raise ValueError("invalid_map_import_object_key")
    path = root / key
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("invalid_map_import_object")
    return path


def _options(region: str, city_relation_ids: list[int] | tuple[int, ...],
             coverage_mode: str, coverage_evidence: str) -> tuple[str, list[int], str, str, str]:
    region = region.strip()
    if any(not isinstance(value, int) or isinstance(value, bool) for value in city_relation_ids):
        raise ValueError("invalid_osm_import_selection")
    relation_ids = sorted(set(city_relation_ids))
    if not region or len(region) > 128 or not relation_ids or len(relation_ids) > MAX_IMPORT_CITIES:
        raise ValueError("invalid_osm_import_selection")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
           for value in relation_ids):
        raise ValueError("invalid_osm_import_selection")
    if coverage_mode not in {"complete", "sampled"}:
        raise ValueError("invalid_coverage_mode")
    evidence = coverage_evidence.strip()
    if coverage_mode == "complete" and not evidence:
        raise ValueError("complete_coverage_evidence_required")
    if len(evidence) > 2048:
        raise ValueError("coverage_evidence_too_long")
    selection_key = hashlib.sha256(
        f"{coverage_mode}:{','.join(map(str, relation_ids))}".encode("ascii")
    ).hexdigest()
    return region, relation_ids, coverage_mode, evidence, selection_key


def submit_osm_import(engine: Engine, source_path: str | os.PathLike[str], *, region: str,
                      city_relation_ids: list[int] | tuple[int, ...],
                      source_timestamp: str | None = None, coverage_mode: str = "sampled",
                      coverage_evidence: str = "", storage_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Durably snapshot a public extract and submit one idempotent global task."""
    from backend.app.geography import ELIGIBILITY_RULE_VERSION, _iso_timestamp

    region, relation_ids, coverage_mode, evidence, selection_key = _options(
        region, city_relation_ids, coverage_mode, coverage_evidence
    )
    parsed_timestamp = _iso_timestamp(source_timestamp) if source_timestamp else None
    root = _storage_root(storage_root)
    object_key, checksum = _snapshot(source_path, root)
    retained = False
    identity = {"region": region, "checksum": checksum, "coverage_mode": coverage_mode,
                "selection": selection_key, "rule": ELIGIBILITY_RULE_VERSION}
    try:
        with engine.begin() as db:
            row = db.execute(text("""INSERT INTO map_import_tasks
                (task_type,phase,region,object_key,source_checksum,source_timestamp,
                 city_relation_ids,eligibility_rule_version,selection_key,coverage_mode,coverage_evidence)
                VALUES ('osm_import','import',:region,:object_key,:checksum,:source_timestamp,
                    CAST(:relation_ids AS jsonb),:rule,:selection,:coverage_mode,:evidence)
                ON CONFLICT DO NOTHING RETURNING id"""), {
                "region": region, "object_key": object_key, "checksum": checksum,
                "source_timestamp": parsed_timestamp, "relation_ids": json.dumps(relation_ids),
                "rule": ELIGIBILITY_RULE_VERSION, "selection": selection_key,
                "coverage_mode": coverage_mode, "evidence": evidence,
            }).first()
            if row is not None:
                task_id = int(row[0])
                retained = True
            else:
                existing = db.execute(text("""SELECT id,region,object_key,status,phase,dataset_id,
                        source_cursor,attempts,last_error,source_checksum,created_at,updated_at
                    FROM map_import_tasks WHERE task_type='osm_import' AND region=:region
                      AND source_checksum=:checksum AND coverage_mode=:coverage_mode
                      AND selection_key=:selection AND eligibility_rule_version=:rule"""), identity).mappings().one()
                task_id = int(existing["id"])
                result = _public_task(existing)
        if not retained:
            _remove_object(root, object_key)
            return result
        with engine.connect() as db:
            task = db.execute(text("""SELECT id,region,object_key,status,phase,dataset_id,source_cursor,
                attempts,last_error,source_checksum,created_at,updated_at FROM map_import_tasks WHERE id=:id"""), {
                "id": task_id,
            }).mappings().one()
        return _public_task(task)
    except BaseException:
        # A failed COMMIT can have an uncertain outcome; preserve bytes if the row landed.
        try:
            with engine.connect() as db:
                exists = db.execute(text("""SELECT 1 FROM map_import_tasks WHERE task_type='osm_import'
                    AND region=:region AND source_checksum=:checksum AND coverage_mode=:coverage_mode
                    AND selection_key=:selection AND eligibility_rule_version=:rule
                    AND object_key=:object_key"""), {**identity, "object_key": object_key}).first()
            if exists:
                retained = True
        except Exception:
            retained = True
        if not retained:
            _remove_object(root, object_key)
        raise


def _remove_object(root: Path, key: str) -> None:
    try:
        _object_path(root, key).unlink()
        _fsync_dir(root)
    except FileNotFoundError:
        return


def _public_task(row) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "region": row["region"],
        "status": row["status"], "phase": row["phase"],
        "dataset_id": str(row["dataset_id"]) if row["dataset_id"] is not None else None,
        "source_cursor": str(row["source_cursor"]) if row["source_cursor"] is not None else None,
        "attempts": int(row["attempts"]), "last_error": row["last_error"],
        "source_checksum": row["source_checksum"].strip() if row["source_checksum"] else None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def get_map_task(engine: Engine, task_id: int) -> dict[str, Any] | None:
    with engine.connect() as db:
        row = db.execute(text("""SELECT id,region,status,phase,dataset_id,source_cursor,attempts,last_error,
            source_checksum,created_at,updated_at FROM map_import_tasks WHERE id=:id"""), {
            "id": task_id,
        }).mappings().first()
    return _public_task(row) if row is not None else None


def retry_map_task(engine: Engine, task_id: int, *, source_timestamp: str | None = None) -> dict[str, Any] | None:
    timestamp = None
    if source_timestamp:
        from backend.app.geography import _iso_timestamp

        timestamp = _iso_timestamp(source_timestamp)
    with engine.begin() as db:
        row = db.execute(text("""UPDATE map_import_tasks SET
            status=CASE WHEN phase='backfill' THEN 'backfilling' ELSE 'queued' END,
            attempts=0,available_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,
            source_timestamp=COALESCE(:source_timestamp,source_timestamp),
            last_error=NULL,updated_at=clock_timestamp()
            WHERE id=:id AND status='failed' AND (:source_timestamp IS NULL OR phase='import')
            RETURNING id,region,status,phase,dataset_id,source_cursor,attempts,last_error,
              source_checksum,created_at,updated_at"""), {
            "id": task_id, "source_timestamp": timestamp,
        }).mappings().first()
    return _public_task(row) if row is not None else get_map_task(engine, task_id)


def _task_lock(db, task_id: int, lease_token: str):
    row = db.execute(text("""SELECT * FROM map_import_tasks WHERE id=:id AND status='running'
        AND lease_token=:token AND leased_until>clock_timestamp() FOR UPDATE"""), {
        "id": task_id, "token": lease_token,
    }).mappings().first()
    if row is None:
        raise RuntimeError("map_task_lease_lost")
    return row


def _active_dataset_fence(db, *, exclusive: bool) -> None:
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    db.execute(text(f"SELECT {function}(hashtextextended(:key,:seed))"), {
        "key": ACTIVE_DATASETS_LOCK_KEY, "seed": ACTIVE_DATASETS_LOCK_SEED,
    }).scalar_one()


def lock_active_datasets_for_enqueue(db) -> None:
    """Fence source activation against active-dataset enumeration in the same transaction."""
    _active_dataset_fence(db, exclusive=False)


def _ensure_backfill_task(db, dataset_id: int) -> None:
    db.execute(text("""INSERT INTO map_import_tasks(task_type,phase,dataset_id,status)
        VALUES ('coverage_backfill','backfill',:dataset,'queued') ON CONFLICT DO NOTHING"""), {
        "dataset": dataset_id,
    })


def _release_or_fail(engine: Engine, task_id: int, lease_token: str, error: Exception) -> None:
    code = "osm_import_failed" if isinstance(error, ValueError) else "map_task_failed"
    if isinstance(error, RuntimeError) and _ERROR_CODE.fullmatch(str(error)):
        code = str(error)
    with engine.begin() as db:
        db.execute(text("""UPDATE map_import_tasks SET
            status=CASE WHEN attempts>=max_attempts THEN 'failed'
                WHEN phase='backfill' THEN 'backfilling' ELSE 'queued' END,
            available_at=CASE WHEN attempts>=max_attempts THEN available_at
                ELSE clock_timestamp()+make_interval(secs=>LEAST(power(2,LEAST(attempts-1,16))::int,300)) END,
            lease_token=NULL,leased_until=NULL,last_error=:error,updated_at=clock_timestamp()
            WHERE id=:id AND status='running' AND lease_token=:token
              AND leased_until>clock_timestamp()"""), {
            "id": task_id, "token": lease_token, "error": code,
        })


def claim_map_task(engine: Engine, *, lease_seconds: int = TASK_LEASE_SECONDS) -> dict[str, Any] | None:
    if not 1 <= lease_seconds <= 86_400:
        raise ValueError("invalid_map_task_lease")
    token = uuid.uuid4().hex
    with engine.begin() as db:
        db.execute(text("""WITH expired AS (
            SELECT id FROM map_import_tasks WHERE status='running' AND leased_until<=clock_timestamp()
            ORDER BY leased_until,id FOR UPDATE SKIP LOCKED LIMIT 100
        )
        UPDATE map_import_tasks AS task SET
            status=CASE WHEN attempts>=max_attempts THEN 'failed'
                WHEN phase='backfill' THEN 'backfilling' ELSE 'queued' END,
            last_error=CASE WHEN attempts>=max_attempts THEN COALESCE(last_error,'lease_expired') ELSE last_error END,
            available_at=CASE WHEN attempts>=max_attempts THEN available_at ELSE clock_timestamp() END,
            lease_token=NULL,leased_until=NULL,updated_at=clock_timestamp()
            FROM expired WHERE task.id=expired.id"""))
        row = db.execute(text("""WITH candidate AS (
                SELECT id FROM map_import_tasks
                WHERE status IN ('queued','backfilling') AND available_at<=clock_timestamp()
                  AND attempts<max_attempts
                ORDER BY CASE WHEN task_type='osm_import' THEN 0 ELSE 1 END,available_at,id
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE map_import_tasks task SET status='running',attempts=task.attempts+1,
                lease_token=:token,
                leased_until=clock_timestamp()+make_interval(secs=>:lease_seconds),
                updated_at=clock_timestamp()
            FROM candidate WHERE task.id=candidate.id RETURNING task.*"""), {
            "token": token, "lease_seconds": lease_seconds,
        }).mappings().first()
    return dict(row) if row is not None else None


def _finalize_import(db, *, task_id: int, lease_token: str, dataset_id: int,
                     source_timestamp, activate_and_backfill: bool) -> None:
    state = "backfilling" if activate_and_backfill else "succeeded"
    phase = "backfill" if activate_and_backfill else "import"
    result = db.execute(text("""UPDATE map_import_tasks SET dataset_id=:dataset,
        source_timestamp=:source_timestamp,
        phase=:phase,status=:status,source_cursor=NULL,lease_token=NULL,leased_until=NULL,
        attempts=0,last_error=NULL,updated_at=clock_timestamp()
        WHERE id=:id AND task_type='osm_import' AND status='running'
          AND lease_token=:token AND leased_until>clock_timestamp()"""), {
        "dataset": dataset_id, "source_timestamp": source_timestamp,
        "phase": phase, "status": state,
        "id": task_id, "token": lease_token,
    })
    if result.rowcount != 1:
        raise RuntimeError("map_task_lease_lost")


def _run_import(engine: Engine, task: dict[str, Any]) -> None:
    root = _storage_root()
    path = _object_path(root, str(task["object_key"]))
    timestamp = task.get("source_timestamp")
    timestamp_text = timestamp.isoformat() if timestamp is not None else None
    from backend.app.geography import import_osm_xml

    import_osm_xml(
        engine, path, region=str(task["region"]),
        city_relation_ids=list(task["city_relation_ids"]),
        source_timestamp=timestamp_text, coverage_mode=str(task["coverage_mode"]),
        coverage_evidence=str(task["coverage_evidence"]),
        _map_task=(int(task["id"]), str(task["lease_token"])),
        _expected_checksum=str(task["source_checksum"]).strip(),
    )


def _advance_backfill_source(engine: Engine, task_id: int, lease_token: str) -> bool:
    from fastapi import HTTPException
    from backend.app.coverage import queue_source_coverage

    with engine.begin() as db:
        task = _task_lock(db, task_id, lease_token)
        if task["dataset_id"] is None:
            raise RuntimeError("map_backfill_dataset_missing")
        dataset = db.execute(text("SELECT status,coverage_mode FROM map_datasets WHERE id=:id"), {
            "id": task["dataset_id"],
        }).mappings().first()
        if dataset is None or dataset["status"] != "active" or dataset["coverage_mode"] != "complete":
            completed = db.execute(text("""UPDATE map_import_tasks SET status='succeeded',lease_token=NULL,
                leased_until=NULL,updated_at=clock_timestamp() WHERE id=:id AND status='running'
                  AND lease_token=:token AND leased_until>clock_timestamp()"""), {
                "id": task_id, "token": lease_token,
            })
            if completed.rowcount != 1:
                raise RuntimeError("map_task_lease_lost")
            return False
        cursor = task["source_cursor"]
        candidate = db.execute(text("""SELECT id,account_id FROM activity_sources
            WHERE (CAST(:cursor AS bigint) IS NULL OR id>CAST(:cursor AS bigint))
            ORDER BY id LIMIT 1"""), {
            "cursor": cursor,
        }).mappings().first()
        if candidate is None:
            completed = db.execute(text("""UPDATE map_import_tasks SET status='succeeded',lease_token=NULL,
                leased_until=NULL,last_error=NULL,updated_at=clock_timestamp()
                WHERE id=:id AND lease_token=:token AND leased_until>clock_timestamp()"""), {
                "id": task_id, "token": lease_token,
            })
            if completed.rowcount != 1:
                raise RuntimeError("map_task_lease_lost")
            return False
        source_id = int(candidate["id"])
        account_id = str(candidate["account_id"])
        deleted = False
        try:
            owner_lock(db, account_id, exclusive=True)
        except HTTPException as error:
            if error.status_code not in {401, 410}:
                raise
            deleted = True
        if not deleted:
            source = db.execute(text("""SELECT id,revision,status,activity_id FROM activity_sources
                WHERE account_id=:account AND id=:id"""), {
                "account": account_id, "id": source_id,
            }).mappings().first()
            if source is not None and source["status"] == "succeeded" and source["activity_id"] is not None:
                queue_source_coverage(
                    db, account_id=account_id, source_id=source_id,
                    source_revision=int(source["revision"]), activity_id=int(source["activity_id"]),
                    dataset_ids=[int(task["dataset_id"])], priority=10,
                )
        advanced = db.execute(text("""UPDATE map_import_tasks SET source_cursor=:cursor,
            attempts=0,
            last_error=NULL,updated_at=clock_timestamp()
            WHERE id=:id AND status='running' AND lease_token=:token
              AND leased_until>clock_timestamp() AND (source_cursor IS NULL OR source_cursor<:cursor)"""), {
            "cursor": source_id, "id": task_id, "token": lease_token,
        })
        if advanced.rowcount != 1:
            raise RuntimeError("map_task_lease_lost")
    return True


def _release_backfill_task(engine: Engine, task_id: int, lease_token: str) -> None:
    with engine.begin() as db:
        row = db.execute(text("""UPDATE map_import_tasks SET status='backfilling',
            lease_token=NULL,leased_until=NULL,updated_at=clock_timestamp()
            WHERE id=:id AND status='running' AND lease_token=:token
              AND leased_until>clock_timestamp()"""), {
            "id": task_id, "token": lease_token,
        })
        if row.rowcount != 1:
            raise RuntimeError("map_task_lease_lost")


def process_map_task_step(engine: Engine, *, max_sources: int = MAX_BACKFILL_SOURCES,
                          lease_seconds: int = TASK_LEASE_SECONDS) -> bool:
    """Process one global import or at most 20 source checkpoints; safe to call from each worker loop."""
    if not 1 <= max_sources <= MAX_BACKFILL_SOURCES:
        raise ValueError("invalid_map_backfill_batch_size")
    task = claim_map_task(engine, lease_seconds=lease_seconds)
    if task is None:
        return False
    task_id, token = int(task["id"]), str(task["lease_token"])
    try:
        if task["task_type"] == "osm_import" and task["phase"] == "import":
            _run_import(engine, task)
            return True
        progressed = False
        for _ in range(max_sources):
            if not _advance_backfill_source(engine, task_id, token):
                return True
            progressed = True
        if progressed:
            _release_backfill_task(engine, task_id, token)
    except Exception as error:
        _release_or_fail(engine, task_id, token, error)
    return True
