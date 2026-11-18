"""The media module's storage interface for callers outside `octoreview.media`.

Lifecycle services and API views go through these functions; only `octoreview.media` imports
the adapters in `octoreview.media.storage` (an import-linter contract enforces it).
"""

from __future__ import annotations

from datetime import datetime

from octoreview.media.storage import (
    CredentialsExpiring,
    ObjectInfo,
    PreconditionFailed,
    StorageError,
    UploadTarget,
    final_key,
    get_backend,
    staging_key,
)

__all__ = [
    "CredentialsExpiring",
    "ObjectInfo",
    "PreconditionFailed",
    "StorageError",
    "UploadTarget",
    "backend_name",
    "create_upload",
    "delete",
    "download_url",
    "final_key",
    "finalize",
    "free_bytes",
    "open_file",
    "read_head",
    "staging_key",
    "stat",
    "sweep_staging",
    "url_expiry",
    "write_stream",
]


def backend_name() -> str:
    return get_backend().name


def url_expiry(requested: datetime) -> datetime:
    return get_backend().url_expiry(requested)


def create_upload(*, key, content_type, size_bytes, expires_at, media_id) -> UploadTarget:
    return get_backend().create_upload(
        key=key,
        content_type=content_type,
        size_bytes=size_bytes,
        expires_at=expires_at,
        media_id=media_id,
    )


def stat(key: str) -> ObjectInfo | None:
    return get_backend().stat(key)


def read_head(key: str, length: int, *, if_version: str) -> bytes:
    return get_backend().read_head(key, length, if_version=if_version)


def finalize(staging: str, final: str, *, content_type: str, if_version: str) -> None:
    get_backend().finalize(staging, final, content_type=content_type, if_version=if_version)


def delete(key: str) -> None:
    get_backend().delete(key)


def download_url(key, *, media_id, audience: str, expires_at: datetime) -> str:
    return get_backend().download_url(
        key, media_id=media_id, audience=audience, expires_at=expires_at
    )


def free_bytes() -> int | None:
    return get_backend().free_bytes()


def sweep_staging(older_than: datetime) -> int:
    return get_backend().sweep_staging(older_than)


def write_stream(key: str, chunks, *, limit: int) -> int:
    return get_backend().write_stream(key, chunks, limit=limit)


def open_file(key: str):
    return get_backend().open(key)
