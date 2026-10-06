"""Private local storage for original uploaded source files."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path


_OBJECT_KEY = re.compile(r"[0-9a-f]{32}\.(?:gpx|fit)\Z")
MAX_OBJECT_BYTES = 10 * 1024 * 1024


class LocalObjectStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        configured = root or os.getenv("UPLOAD_STORAGE_DIR")
        self.root = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[1] / ".uploads"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def write(self, content: bytes, *, extension: str = "gpx") -> str:
        if extension not in {"gpx", "fit"}:
            raise ValueError("unsupported source extension")
        key = f"{uuid.uuid4().hex}.{extension}"
        path = self.root / key
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as source:
                source.write(content)
                source.flush()
                os.fsync(source.fileno())
            os.chmod(path, 0o600)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self.root, directory_flags)
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

    def read(self, object_key: str) -> bytes:
        path = self._path(object_key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            content = source.read(MAX_OBJECT_BYTES + 1)
        if len(content) > MAX_OBJECT_BYTES:
            raise ValueError("stored source exceeds its size limit")
        return content

    def delete(self, object_key: str) -> None:
        try:
            self._path(object_key).unlink()
        except FileNotFoundError:
            pass

    def _path(self, object_key: str) -> Path:
        if not isinstance(object_key, str) or not _OBJECT_KEY.fullmatch(object_key):
            raise ValueError("invalid object key")
        return self.root / object_key
