"""Fsynced append-only deletion intents retained outside database backups."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OBJECT_KEY = re.compile(r"[0-9a-f]{32}\.(?:gpx|fit)\Z")


class JournalError(RuntimeError):
    pass


def configured_path(*, required: bool = True) -> Path | None:
    value = os.getenv("ACCOUNT_DELETION_LEDGER", "").strip()
    if not value:
        if required:
            raise JournalError("account_deletion_ledger_unconfigured")
        return None
    return Path(value).expanduser()


def initialize_ledger() -> Path:
    path = configured_path()
    assert path is not None
    parent = path.parent
    missing = []
    current = parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _fsync_directory(directory.parent)
    parent_stat = parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise JournalError("account_deletion_ledger_parent_invalid")
    if parent_stat.st_uid != os.geteuid():
        raise JournalError("account_deletion_ledger_parent_owner_invalid")
    os.chmod(parent, 0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    except FileExistsError:
        _validate_file(path)
    else:
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _fsync_directory(parent)
    return path


def read_records(*, required: bool = True) -> list[dict[str, Any]]:
    path = configured_path(required=required)
    if path is None:
        return []
    descriptor = _safe_open(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        info = os.fstat(descriptor)
        if info.st_size > MAX_JOURNAL_BYTES:
            raise JournalError("account_deletion_ledger_too_large")
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise JournalError("account_deletion_ledger_short_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        return _decode(b"".join(chunks))
    finally:
        os.close(descriptor)


def read_records_durable(*, required: bool = True) -> list[dict[str, Any]]:
    """Validate and durably flush the complete ledger before replay mutates the database."""
    path = configured_path(required=required)
    if path is None:
        return []
    descriptor = _safe_open(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        info = os.fstat(descriptor)
        if info.st_size > MAX_JOURNAL_BYTES:
            raise JournalError("account_deletion_ledger_too_large")
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise JournalError("account_deletion_ledger_short_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        records = _decode(b"".join(chunks))
        try:
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        except OSError as error:
            raise JournalError("account_deletion_ledger_sync_failed") from error
        return records
    finally:
        os.close(descriptor)


def append_deletion(account_id: str, object_keys: list[str]) -> dict[str, Any]:
    path = configured_path()
    assert path is not None
    descriptor = _safe_open(path, os.O_RDWR | os.O_APPEND)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        info = os.fstat(descriptor)
        if info.st_size > MAX_JOURNAL_BYTES:
            raise JournalError("account_deletion_ledger_too_large")
        raw = bytearray()
        while len(raw) < info.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, info.st_size - len(raw)))
            if not chunk:
                raise JournalError("account_deletion_ledger_short_read")
            raw.extend(chunk)
        records = _decode(bytes(raw))
        prior = next((entry for entry in records if entry["account_id"] == account_id), None)
        if prior is not None:
            return prior
        keys = sorted(set(object_keys))
        if any(not isinstance(key, str) or not _OBJECT_KEY.fullmatch(key) for key in keys):
            raise JournalError("account_deletion_object_key_invalid")
        record: dict[str, Any] = {
            "version": 1,
            "sequence": len(records) + 1,
            "account_id": account_id,
            "deletion_id": str(uuid.uuid4()),
            "requested_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "object_keys": keys,
            "previous_digest": records[-1]["digest"] if records else "0" * 64,
        }
        record["digest"] = _digest(record)
        encoded = _canonical(record) + b"\n"
        if len(raw) + len(encoded) > MAX_JOURNAL_BYTES:
            raise JournalError("account_deletion_ledger_too_large")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise JournalError("account_deletion_ledger_short_write")
            view = view[written:]
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise JournalError("account_deletion_ledger_sync_failed") from error
        return record
    finally:
        os.close(descriptor)


def _validate_file(path: Path) -> None:
    descriptor = _safe_open(path, os.O_RDONLY)
    os.close(descriptor)


def _safe_open(path: Path, flags: int) -> int:
    try:
        descriptor = os.open(path, flags | _NOFOLLOW)
    except OSError as error:
        raise JournalError("account_deletion_ledger_unavailable") from error
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(descriptor)
        raise JournalError("account_deletion_ledger_permissions_invalid")
    return descriptor


def _decode(raw: bytes) -> list[dict[str, Any]]:
    if raw and not raw.endswith(b"\n"):
        raise JournalError("account_deletion_ledger_incomplete_record")
    records: list[dict[str, Any]] = []
    previous = "0" * 64
    accounts: set[str] = set()
    deletion_ids: set[str] = set()
    for index, line in enumerate(raw.splitlines(), start=1):
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise JournalError("account_deletion_ledger_corrupt") from error
        if not isinstance(record, dict) or set(record) != {
            "version", "sequence", "account_id", "deletion_id", "requested_at", "object_keys",
            "previous_digest", "digest",
        }:
            raise JournalError("account_deletion_ledger_corrupt")
        if (record["version"] != 1 or record["sequence"] != index
                or not isinstance(record["account_id"], str) or not record["account_id"]
                or record["account_id"] in accounts
                or record["previous_digest"] != previous or record["digest"] != _digest(record)
                or not isinstance(record["object_keys"], list)):
            raise JournalError("account_deletion_ledger_corrupt")
        try:
            parsed_id = uuid.UUID(record["deletion_id"])
            if str(parsed_id) != record["deletion_id"] or record["deletion_id"] in deletion_ids:
                raise ValueError("duplicate or noncanonical deletion id")
            datetime.fromisoformat(record["requested_at"])
        except (ValueError, TypeError) as error:
            raise JournalError("account_deletion_ledger_corrupt") from error
        if (any(not isinstance(key, str) or not _OBJECT_KEY.fullmatch(key) for key in record["object_keys"])
                or record["object_keys"] != sorted(set(record["object_keys"]))):
            raise JournalError("account_deletion_ledger_corrupt")
        accounts.add(record["account_id"])
        deletion_ids.add(record["deletion_id"])
        previous = record["digest"]
        records.append(record)
    return records


def _digest(record: dict[str, Any]) -> str:
    values = {key: value for key, value in record.items() if key != "digest"}
    return hashlib.sha256(_canonical(values)).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
