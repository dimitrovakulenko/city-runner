"""Private local storage for original uploaded source files."""

from __future__ import annotations

import os
import hashlib
import re
import stat
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import Engine, text

from backend.app.owner_guard import owner_lock


_OBJECT_KEY = re.compile(r"[0-9a-f]{32}\.(?:gpx|fit)\Z")
MAX_OBJECT_BYTES = 10 * 1024 * 1024


class LocalObjectStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        configured = root or os.getenv("UPLOAD_STORAGE_DIR")
        self.root = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[1] / ".uploads"
        missing = []
        current = self.root
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self._fsync_dir(directory.parent)
        root_info = self.root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid():
            raise ValueError("invalid private upload storage directory")
        os.chmod(self.root, 0o700)

    def write(self, content: bytes, *, extension: str = "gpx", owner_id: str | None = None) -> str:
        if extension not in {"gpx", "fit"}:
            raise ValueError("unsupported source extension")
        key = f"{uuid.uuid4().hex}.{extension}"
        directory = self._owner_dir(owner_id, create=True) if owner_id is not None else self.root
        path = directory / key
        if owner_id is not None:
            self._write_owner_metadata(directory, key, owner_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as source:
                source.write(content)
                source.flush()
                os.fsync(source.fileno())
            os.chmod(path, 0o600)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(directory, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return key

    def read(self, object_key: str, *, owner_id: str | None = None) -> bytes:
        path = self._owned_path(object_key, owner_id) if owner_id is not None else self._path(object_key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            content = source.read(MAX_OBJECT_BYTES + 1)
        if len(content) > MAX_OBJECT_BYTES:
            raise ValueError("stored source exceeds its size limit")
        return content

    def delete(self, object_key: str, *, owner_id: str | None = None) -> None:
        self._path(object_key)
        if owner_id is not None:
            directory = self._owner_dir(owner_id)
            if directory.exists():
                try:
                    (directory / object_key).unlink()
                except FileNotFoundError:
                    pass
                self._fsync_dir(directory)
                metadata = directory / f"{object_key}.owner"
                try:
                    metadata.unlink()
                except FileNotFoundError:
                    pass
                self._fsync_dir(directory)
                try:
                    directory.rmdir()
                    self._fsync_dir(self.root)
                except OSError:
                    pass
            # Compatibility for objects written before owner directories existed.
            try:
                self._path(object_key).unlink()
                self._fsync_dir(self.root)
            except FileNotFoundError:
                pass
            return
        try:
            self._path(object_key).unlink()
            self._fsync_dir(self.root)
        except FileNotFoundError:
            pass

    def list_owned_keys(self, owner_id: str) -> list[str]:
        directory = self._owner_dir(owner_id)
        try:
            entries = list(directory.iterdir())
        except FileNotFoundError:
            return []
        keys = []
        for entry in entries:
            if not entry.name.endswith(".owner"):
                continue
            key = entry.name[:-6]
            if not _OBJECT_KEY.fullmatch(key):
                raise ValueError("invalid owner storage metadata name")
            value = self._read_owner_metadata(entry)
            if value == owner_id:
                keys.append(key)
            else:
                raise ValueError("owner storage metadata does not match its directory")
        return sorted(set(keys))

    def _owner_dir(self, owner_id: str | None, *, create: bool = False) -> Path:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("invalid owner id")
        name = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        path = self.root / name
        if create:
            try:
                path.mkdir(mode=0o700)
                self._fsync_dir(self.root)
            except FileExistsError:
                pass
        try:
            info = path.lstat()
        except FileNotFoundError:
            return path
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("invalid owner storage directory")
        if create:
            if info.st_uid != os.geteuid():
                raise ValueError("invalid owner storage directory owner")
            os.chmod(path, 0o700)
        return path

    def _owned_path(self, object_key: str, owner_id: str) -> Path:
        self._path(object_key)
        directory = self._owner_dir(owner_id)
        try:
            metadata = self._read_owner_metadata(directory / f"{object_key}.owner")
        except FileNotFoundError:
            if (directory / object_key).exists():
                raise FileNotFoundError("source owner metadata is missing")
            return self._path(object_key)
        if metadata != owner_id:
            raise FileNotFoundError("source owner metadata mismatch")
        return directory / object_key

    @staticmethod
    def _write_owner_metadata(directory: Path, key: str, owner_id: str) -> None:
        path = directory / f"{key}.owner"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as metadata:
                metadata.write(owner_id.encode("utf-8"))
                metadata.flush()
                os.fsync(metadata.fileno())
            LocalObjectStore._fsync_dir(directory)
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_owner_metadata(path: Path) -> str:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError("invalid owner metadata permissions")
            raw = os.read(descriptor, 1024)
            if os.read(descriptor, 1):
                raise OSError("invalid owner metadata size")
            return raw.decode("utf-8")
        finally:
            os.close(descriptor)

    def _path(self, object_key: str) -> Path:
        if not isinstance(object_key, str) or not _OBJECT_KEY.fullmatch(object_key):
            raise ValueError("invalid object key")
        return self.root / object_key


def cleanup_unreferenced_original(engine: Engine, store: LocalObjectStore,
                                  account_id: str, object_key: str) -> None:
    """Remove a failed/duplicate write while the account mutation guard is held."""
    try:
        with engine.begin() as db:
            owner_lock(db, account_id)
            referenced = db.execute(text("""SELECT 1 FROM activity_sources
                WHERE account_id=:account AND private_object_key=:key"""), {
                "account": account_id, "key": object_key,
            }).first()
            if referenced is None:
                store.delete(object_key, owner_id=account_id)
    except HTTPException as error:
        if error.status_code != 410:
            raise
