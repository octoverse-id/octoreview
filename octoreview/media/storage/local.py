"""Filesystem backend under OCTOREVIEW_MEDIA_ROOT (docs/spec.md section 7.5).

Development and single-node only. Uploads land through `PUT /api/v1/media-uploads/{token}`,
which writes a part file and `os.replace`s it onto the staging key, so a reader never sees a
half-written file. `finalize` renames the staging file to a private attempt path first, then
checks it is still the validated version, then renames it onto the final key: a re-PUT during
completion lands on a new inode and fails the version check instead of slipping through.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

from django.conf import settings

from octoreview.media import tokens
from octoreview.media.storage import (
    STAGING_PREFIX,
    ObjectInfo,
    PreconditionFailed,
    StorageBackend,
    StorageError,
    UploadTarget,
)


class LocalBackend(StorageBackend):
    name = "local"
    supports_conditional_copy = True
    serves_token_urls = True

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or settings.MEDIA_ROOT).resolve()

    def path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # Keys are built from validated ids and UUIDs, but the root check makes a crafted key
        # harmless anyway (docs/spec.md section 7.5).
        if path != self.root and self.root not in path.parents:
            raise StorageError("path", "Resolved path escapes the media root.")
        return path

    @staticmethod
    def _version(stat: os.stat_result) -> str:
        return f"{stat.st_ino}-{stat.st_mtime_ns}-{stat.st_size}"

    def create_upload(self, *, key, content_type, size_bytes, expires_at, media_id) -> UploadTarget:
        self.guard()
        token = tokens.upload_token(
            media_id=media_id, key=key, size_bytes=size_bytes, expires_at=expires_at
        )
        return UploadTarget(
            method="PUT",
            url=f"{settings.PUBLIC_BASE_URL}/api/v1/media-uploads/{token}",
            expires_at=expires_at,
            headers={"Content-Type": content_type},
        )

    def stat(self, key: str) -> ObjectInfo | None:
        self.guard()
        try:
            stat = self.path_for(key).stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError("stat", str(exc)) from exc
        return ObjectInfo(size=stat.st_size, version=self._version(stat))

    def read_head(self, key: str, length: int, *, if_version: str) -> bytes:
        self.guard()
        path = self.path_for(key)
        try:
            with path.open("rb") as handle:
                if self._version(os.fstat(handle.fileno())) != if_version:
                    raise PreconditionFailed()
                return handle.read(length)
        except FileNotFoundError as exc:
            raise PreconditionFailed() from exc
        except OSError as exc:
            raise StorageError("read_head", str(exc)) from exc

    def finalize(self, staging_key, final_key, *, content_type, if_version) -> None:
        self.guard()
        source = self.path_for(staging_key)
        target = self.path_for(final_key)
        attempt = source.with_name(f"{source.name}.attempt-{uuid.uuid4().hex}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, attempt)
        except FileNotFoundError as exc:
            raise PreconditionFailed() from exc
        except OSError as exc:
            raise StorageError("finalize", str(exc)) from exc
        try:
            if self._version(attempt.stat()) != if_version:
                attempt.unlink(missing_ok=True)
                raise PreconditionFailed()
            os.replace(attempt, target)
        except OSError as exc:
            attempt.unlink(missing_ok=True)
            raise StorageError("finalize", str(exc)) from exc

    def delete(self, key: str) -> None:
        self.guard()
        try:
            self.path_for(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("delete", str(exc)) from exc

    def download_url(self, key, *, media_id, audience, expires_at: datetime) -> str:
        token = tokens.download_token(
            media_id=media_id, key=key, audience=audience, expires_at=expires_at
        )
        return f"{settings.PUBLIC_BASE_URL}/api/v1/media-files/{token}"

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        stat = os.statvfs(self.root)
        return stat.f_bavail * stat.f_frsize

    def sweep_staging(self, older_than: datetime) -> int:
        """Delete staging, part and attempt files last modified before `older_than`."""

        self.guard()
        staging_root = self.root / STAGING_PREFIX
        if not staging_root.is_dir():
            return 0
        cutoff = older_than.timestamp()
        swept = 0
        try:
            for path in staging_root.rglob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    swept += 1
        except OSError as exc:
            raise StorageError("sweep_staging", str(exc)) from exc
        return swept

    def write_stream(self, key: str, chunks, *, limit: int) -> int:
        """Stream chunks to a part file, then atomically replace the key. Returns the size.

        Raises ValueError when more than `limit` bytes arrive (413 upload_too_large).
        """

        self.guard()
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f"{path.name}.part-{uuid.uuid4().hex}")
        written = 0
        try:
            with part.open("wb") as handle:
                for chunk in chunks:
                    written += len(chunk)
                    if written > limit:
                        raise ValueError("upload exceeds the declared size")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, path)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return written

    def open(self, key: str):
        self.guard()
        return self.path_for(key).open("rb")
