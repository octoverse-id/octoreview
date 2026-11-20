"""The media module's storage interface for callers outside `octoreview.media`.

Lifecycle services and API views go through these functions; only `octoreview.media` imports
the adapters in `octoreview.media.storage` (an import-linter contract enforces it).
"""

from __future__ import annotations

import time
from datetime import datetime

from octoreview.core import metrics
from octoreview.media.storage import (
    MEDIA_PREFIX,
    STAGING_PREFIX,
    CredentialsExpiring,
    FinalExists,
    ObjectInfo,
    PreconditionFailed,
    StorageError,
    UploadTarget,
    final_key,
    get_backend,
    staging_key,
)

__all__ = [
    "MEDIA_PREFIX",
    "STAGING_PREFIX",
    "CredentialsExpiring",
    "FinalExists",
    "ObjectInfo",
    "PreconditionFailed",
    "StorageError",
    "UploadTarget",
    "backend_name",
    "create_upload",
    "delete",
    "download_url",
    "expiration_rules",
    "final_key",
    "finalize",
    "free_bytes",
    "open_file",
    "read_head",
    "serves_token_urls",
    "staging_key",
    "stat",
    "sweep_staging",
    "url_expiry",
    "write_stream",
]


def backend_name() -> str:
    return get_backend().name


def _timed(operation: str, func, *args, **kwargs):
    """Run one storage protocol call and emit `storage.call` (docs/operations.md).

    `error` is the outcome the storage error-rate alert counts; a missing object, a changed
    upload or a taken final key are expected answers, not errors. Read-path URL signing is not
    timed: a review list signs one URL per media item and would flood the log.
    """

    started = time.perf_counter()
    outcome = "ok"
    try:
        result = func(*args, **kwargs)
        if result is None and operation == "stat":
            outcome = "missing"
        return result
    except PreconditionFailed:
        outcome = "precondition_failed"
        raise
    except FinalExists:
        outcome = "final_exists"
        raise
    except CredentialsExpiring:
        outcome = "credentials_expiring"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        metrics.emit_metric(
            metrics.STORAGE_CALL,
            backend=backend_name(),
            operation=operation,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            outcome=outcome,
        )


def serves_token_urls() -> bool:
    """Whether `/media-uploads/{token}` and `/media-files/{token}` are live (local backend only)."""

    return get_backend().serves_token_urls


def url_expiry(requested: datetime) -> datetime:
    return get_backend().url_expiry(requested)


def create_upload(*, key, content_type, size_bytes, expires_at, media_id) -> UploadTarget:
    return _timed(
        "sign_upload",
        get_backend().create_upload,
        key=key,
        content_type=content_type,
        size_bytes=size_bytes,
        expires_at=expires_at,
        media_id=media_id,
    )


def stat(key: str) -> ObjectInfo | None:
    return _timed("stat", get_backend().stat, key)


def read_head(key: str, length: int, *, if_version: str) -> bytes:
    return _timed("read_head", get_backend().read_head, key, length, if_version=if_version)


def finalize(staging: str, final: str, *, content_type: str, if_version: str) -> None:
    _timed(
        "finalize",
        get_backend().finalize,
        staging,
        final,
        content_type=content_type,
        if_version=if_version,
    )


def delete(key: str) -> None:
    _timed("delete", get_backend().delete, key)


def download_url(key, *, media_id, audience: str, expires_at: datetime) -> str:
    return get_backend().download_url(
        key, media_id=media_id, audience=audience, expires_at=expires_at
    )


def free_bytes() -> int | None:
    return get_backend().free_bytes()


def sweep_staging(older_than: datetime) -> int:
    return _timed("sweep_staging", get_backend().sweep_staging, older_than)


def expiration_rules() -> list | None:
    return _timed("lifecycle", get_backend().expiration_rules)


def write_stream(key: str, chunks, *, limit: int) -> int:
    return get_backend().write_stream(key, chunks, limit=limit)


def open_file(key: str):
    return get_backend().open(key)
