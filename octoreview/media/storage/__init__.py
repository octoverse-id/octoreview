"""The storage protocol (docs/spec.md section 7.1). Only `octoreview.media` imports this package.

Every adapter call asserts that no row lock is held: a network or disk call under a lock would
let a slow bucket stall every writer of that review (docs/spec.md section 12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from django.conf import settings

from octoreview.core.db import locks_held


class StorageError(Exception):
    """The backend failed; callers answer 503 storage_unavailable with Retry-After."""

    def __init__(self, operation: str, message: str = "") -> None:
        super().__init__(message or operation)
        self.operation = operation


class PreconditionFailed(Exception):
    """The object changed since it was validated (a re-PUT); callers answer 409 upload_changed."""


class FinalExists(Exception):
    """The final key already holds an object. Published objects are never overwritten, so a copy
    that outlives its completion lease cannot replace another attempt's bytes (spec 7.4)."""


class CredentialsExpiring(Exception):
    """Less than the minimum URL lifetime of credential remains (503, docs/spec.md 7.8)."""


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    version: str
    content_type: str | None = None


@dataclass(frozen=True)
class UploadTarget:
    method: str
    url: str
    expires_at: datetime
    headers: dict[str, str] = field(default_factory=dict)


class StorageBackend:
    name = "base"
    supports_conditional_copy = True
    # True only for `local`, whose upload and download URLs point at this server's token views.
    serves_token_urls = False

    def guard(self) -> None:
        if locks_held():
            raise RuntimeError("Storage calls must not run while row locks are held.")

    def create_upload(
        self, *, key: str, content_type: str, size_bytes: int, expires_at: datetime, media_id
    ) -> UploadTarget:
        raise NotImplementedError

    def stat(self, key: str) -> ObjectInfo | None:
        raise NotImplementedError

    def read_head(self, key: str, length: int, *, if_version: str) -> bytes:
        raise NotImplementedError

    def finalize(self, staging_key: str, final_key: str, *, content_type: str, if_version: str):
        """Copy exactly the validated version to a final key that does not exist yet.

        PreconditionFailed when the staging object changed or vanished; FinalExists when the
        final key is already taken (never overwritten).
        """

        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def download_url(self, key: str, *, media_id, audience: str, expires_at: datetime) -> str:
        raise NotImplementedError

    def url_expiry(self, requested: datetime) -> datetime:
        """The latest expiry this backend can sign (credential and backend caps)."""

        return requested

    def free_bytes(self) -> int | None:
        """Free space for an intent's disk check; None when the backend has no such limit."""

        return None

    def sweep_staging(self, older_than: datetime) -> int:
        """Delete orphaned staging files; cloud backends rely on bucket lifecycle rules."""

        return 0

    def expiration_rules(self) -> list | None:
        """The bucket's enabled expiration rules, or None when the backend has no lifecycle."""

        return None


@lru_cache(maxsize=1)
def get_backend() -> StorageBackend:
    if settings.STORAGE_BACKEND == "local":
        from octoreview.media.storage.local import LocalBackend

        return LocalBackend()
    if settings.STORAGE_BACKEND == "s3":
        from octoreview.media.storage.s3 import S3Backend

        return S3Backend()
    raise RuntimeError(f"Unsupported storage backend {settings.STORAGE_BACKEND!r}")


STAGING_PREFIX = "_staging"
MEDIA_PREFIX = "media"


def staging_key(tenant_id: str, application_id: str, media_id) -> str:
    return f"{STAGING_PREFIX}/{tenant_id}/{application_id}/{media_id}"


def final_key(tenant_id, application_id, entity_id, review_id, media_id, extension) -> str:
    scope = f"{tenant_id}/{application_id}/{entity_id}/{review_id}"
    return f"{MEDIA_PREFIX}/{scope}/{media_id}.{extension}"
