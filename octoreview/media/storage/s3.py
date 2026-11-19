"""AWS S3 and S3-compatible stores through boto3 (docs/spec.md sections 7.1-7.4 and 7.8).

Bytes never pass through the server: clients PUT to a presigned URL that signs `Content-Type`
and `Content-Length`, and readers GET a presigned URL. `finalize` is one `CopyObject` with
`CopySourceIfMatch` on the version `complete` validated, so a re-PUT during completion fails
the copy (409 upload_changed) instead of publishing unvalidated bytes. `docs/storage.md` lists the
IAM permissions, bucket CORS and the `_staging/` lifecycle rule this backend expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.utils import timezone

from octoreview.media.storage import (
    CredentialsExpiring,
    FinalExists,
    ObjectInfo,
    PreconditionFailed,
    StorageBackend,
    StorageError,
    UploadTarget,
)

# SigV4 presigned URLs cannot live longer than 7 days.
PRESIGN_CAP = timedelta(days=7)
# No URL outlives its signing credential: URLs stop this long before the credential expires, and
# a credential with less usable life than this left gets 503 storage_credentials_expiring.
CREDENTIAL_MARGIN = timedelta(seconds=60)
MIN_URL_LIFETIME = timedelta(seconds=60)


def credential_expiry(credentials) -> datetime | None:
    """When the signing credential expires, or None for static keys.

    The one adapter around botocore's credential object (spec section 7.8). Refreshable
    credentials (IRSA, instance roles, assumed roles) refresh first when botocore's advisory
    window is reached, so the expiry read here belongs to the credential that will sign.
    """

    if credentials is None:
        raise StorageError("credentials", "No AWS credentials were found in the default chain.")
    try:
        credentials.get_frozen_credentials()
    except (BotoCoreError, ClientError) as exc:
        # A mandatory STS refresh can fail with either (an IRSA or assumed-role outage).
        raise _error("credentials", exc) from exc
    expiry = getattr(credentials, "_expiry_time", None)
    if expiry is None:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry.astimezone(UTC)


def _status(exc: ClientError) -> int | None:
    return exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _missing(exc: ClientError) -> bool:
    return _status(exc) == 404 or _code(exc) in {"NoSuchKey", "NotFound", "404"}


def _precondition(exc: ClientError) -> bool:
    return _status(exc) == 412 or _code(exc) == "PreconditionFailed"


def _error(operation: str, exc: Exception) -> StorageError:
    if isinstance(exc, ClientError):
        return StorageError(operation, f"{_code(exc) or 'error'} (HTTP {_status(exc)})")
    return StorageError(operation, type(exc).__name__)


def _seconds_until(expires_at: datetime) -> int:
    # Floor, so the URL never outlives `expires_at`; at least 1 s, which botocore requires.
    return max(1, int((expires_at - timezone.now()).total_seconds()))


@dataclass(frozen=True)
class ExpirationRule:
    rule_id: str
    prefix: str
    days: int | None  # None for a date-based expiration

    def covers(self, prefix: str) -> bool:
        """True when the rule expires objects under `prefix` (all of them, or some)."""

        return prefix.startswith(self.prefix) or self.prefix.startswith(prefix)


class S3Backend(StorageBackend):
    name = "s3"
    supports_conditional_copy = True

    def __init__(self, session=None) -> None:
        self.bucket = settings.STORAGE_S3_BUCKET
        self.session = session or boto3.session.Session()
        base = {
            "signature_version": "s3v4",
            "s3": {"addressing_style": settings.STORAGE_S3_ADDRESSING_STYLE},
            "retries": {"mode": "standard", "max_attempts": 3},
            "connect_timeout": 5,
        }
        common = {
            "region_name": settings.STORAGE_S3_REGION,
            "endpoint_url": settings.STORAGE_S3_ENDPOINT_URL or None,
        }
        self.client = self.session.client("s3", config=Config(**base, read_timeout=60), **common)
        # A store that does not stream progress during a long CopyObject sends nothing until the
        # copy ends, so the copy client may wait for the whole completion lease (spec 7.4). It
        # never retries on its own: a retried copy could start after the lease ended. The client
        # repeats `complete` instead, which takes a fresh lease.
        copy_config = Config(
            **{**base, "retries": {"mode": "standard", "max_attempts": 1}},
            read_timeout=settings.COMPLETE_LEASE_SECONDS,
        )
        self.copy_client = self.session.client("s3", config=copy_config, **common)
        # Presigning is local computation, but the URL embeds the host it was signed for.
        self.presign_client = self.session.client(
            "s3",
            config=Config(**base),
            region_name=settings.STORAGE_S3_REGION,
            endpoint_url=settings.STORAGE_S3_PUBLIC_ENDPOINT_URL or None,
        )

    def url_expiry(self, requested: datetime) -> datetime:
        now = timezone.now()
        limit = min(requested, now + PRESIGN_CAP)
        try:
            credentials = self.session.get_credentials()
        except (BotoCoreError, ClientError) as exc:
            raise _error("credentials", exc) from exc
        expiry = credential_expiry(credentials)
        if expiry is None:
            return limit
        usable = expiry - CREDENTIAL_MARGIN
        if usable - now < MIN_URL_LIFETIME:
            raise CredentialsExpiring()
        return min(limit, usable)

    def create_upload(self, *, key, content_type, size_bytes, expires_at, media_id) -> UploadTarget:
        self.guard()
        try:
            url = self.presign_client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentType": content_type,
                    # Signed, so the store refuses a body of any other size (spec 7.3).
                    "ContentLength": size_bytes,
                },
                ExpiresIn=_seconds_until(expires_at),
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as exc:
            raise _error("sign_upload", exc) from exc
        return UploadTarget(
            method="PUT", url=url, expires_at=expires_at, headers={"Content-Type": content_type}
        )

    def stat(self, key: str) -> ObjectInfo | None:
        self.guard()
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _missing(exc):
                return None
            if _status(exc) == 403:
                # Without s3:ListBucket a missing key answers 403, not 404 (docs/storage.md).
                raise StorageError("stat", "AccessDenied (HTTP 403); grant s3:ListBucket") from exc
            raise _error("stat", exc) from exc
        except BotoCoreError as exc:
            raise _error("stat", exc) from exc
        return ObjectInfo(
            size=head["ContentLength"], version=head["ETag"], content_type=head.get("ContentType")
        )

    def read_head(self, key: str, length: int, *, if_version: str) -> bytes:
        self.guard()
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}", IfMatch=if_version
            )
        except ClientError as exc:
            if _precondition(exc) or _missing(exc):
                raise PreconditionFailed() from exc
            raise _error("read_head", exc) from exc
        except BotoCoreError as exc:
            raise _error("read_head", exc) from exc
        body = response["Body"]
        try:
            # A store that ignores Range sends the whole object; read only what was asked for.
            return body.read(length)
        except BotoCoreError as exc:
            raise _error("read_head", exc) from exc
        finally:
            body.close()

    def finalize(self, staging_key, final_key, *, content_type, if_version) -> None:
        self.guard()
        try:
            self.copy_client.copy_object(
                Bucket=self.bucket,
                Key=final_key,
                CopySource={"Bucket": self.bucket, "Key": staging_key},
                CopySourceIfMatch=if_version,
                # Never overwrite: a copy that outlives its completion lease must not replace the
                # bytes another attempt already published (spec 7.4).
                IfNoneMatch="*",
                # Served metadata is chosen by the server, never inherited from the upload.
                MetadataDirective="REPLACE",
                ContentType=content_type,
            )
        except ClientError as exc:
            if _precondition(exc) or _missing(exc):
                # Both preconditions answer 412; the final key tells them apart.
                if self.stat(final_key) is not None:
                    raise FinalExists() from exc
                raise PreconditionFailed() from exc
            raise _error("finalize", exc) from exc
        except BotoCoreError as exc:
            raise _error("finalize", exc) from exc

    def delete(self, key: str) -> None:
        self.guard()
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _missing(exc):
                return
            raise _error("delete", exc) from exc
        except BotoCoreError as exc:
            raise _error("delete", exc) from exc

    def download_url(self, key, *, media_id, audience, expires_at: datetime) -> str:
        # The audience is enforced when the URL is issued; a presigned URL cannot carry it.
        try:
            return self.presign_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=_seconds_until(expires_at),
            )
        except (BotoCoreError, ClientError) as exc:
            raise _error("sign_download", exc) from exc

    def expiration_rules(self) -> list[ExpirationRule]:
        """The bucket's enabled lifecycle rules that expire objects (empty without any)."""

        self.guard()
        try:
            rules = self.client.get_bucket_lifecycle_configuration(Bucket=self.bucket)["Rules"]
        except ClientError as exc:
            if _code(exc) == "NoSuchLifecycleConfiguration" or _status(exc) == 404:
                return []
            raise _error("lifecycle", exc) from exc
        except BotoCoreError as exc:
            raise _error("lifecycle", exc) from exc
        found = []
        for rule in rules:
            expiration = rule.get("Expiration", {})
            if rule.get("Status") != "Enabled" or not (
                expiration.get("Days") or expiration.get("Date")
            ):
                continue
            rule_filter = rule.get("Filter", {})
            prefix = rule_filter.get("Prefix", rule_filter.get("And", {}).get("Prefix"))
            if prefix is None:
                prefix = rule.get("Prefix", "")  # the legacy top-level form; absent = whole bucket
            found.append(ExpirationRule(rule.get("ID", ""), prefix, expiration.get("Days")))
        return found
