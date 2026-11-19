"""`manage.py check_storage`: a real round trip through the configured backend (spec section 11).

Each step is named so an operator sees exactly which permission or setting is wrong. The local
backend's upload and download URLs point back at this server, so for `local` the command drives
the same code the token views run (stream write, token round trip, open) instead of HTTP. A cloud
backend gets real HTTP through its presigned URLs, plus the guarantees `complete` relies on: the
store refuses a body of another size, refuses a copy of a stale version, and stores the
server-chosen content type. Its bucket lifecycle rules are checked last (docs/storage.md).
"""

from __future__ import annotations

import urllib.error
import urllib.request
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from octoreview.media import files, signatures, tokens

PROBE_CONTENT_TYPE = "application/octet-stream"
# Differs from the upload's type, so the copy proves the server-chosen metadata replaced it.
FINAL_CONTENT_TYPE = "application/x-octoreview-probe"
# A version no store produces: a copy conditioned on it must be refused.
STALE_VERSION = '"00000000000000000000000000000000"'
HTTP_TIMEOUT_SECONDS = 30


def http(method: str, url: str, *, body: bytes | None = None, headers=None) -> tuple[int, bytes]:
    """One HTTP request through a signed URL; returns (status, body) for every status."""

    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def preflight(url: str, origin: str) -> str | None:
    """A browser's CORS preflight for a PUT to `url`; returns Access-Control-Allow-Origin."""

    request = urllib.request.Request(
        url,
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.headers.get("Access-Control-Allow-Origin")
    except urllib.error.HTTPError as exc:
        return exc.headers.get("Access-Control-Allow-Origin")


class Command(BaseCommand):
    help = "Round-trip a probe object through the storage backend and name any failing step."

    def handle(self, *args, **options):
        probe_id = uuid.uuid4()
        body = f"octoreview check_storage {probe_id}\n".encode()
        staging = f"_staging/_check/{probe_id}"
        final = f"media/_check/{probe_id}.bin"
        self.stdout.write(f"backend={files.backend_name()} location={settings.STORAGE_LOCATION_ID}")
        try:
            self._round_trip(probe_id, body, staging, final)
        finally:
            for key in (staging, final):
                try:
                    files.delete(key)
                except files.StorageError as exc:
                    self.stderr.write(f"cleanup: could not delete {key}: {exc}")
        self._check_lifecycle()
        free = files.free_bytes()
        if free is not None:
            self.stdout.write(f"free_bytes={free}")
        self.stdout.write("check_storage: ok")

    def _step(self, name: str, func):
        try:
            result = func()
        except (
            files.StorageError,
            files.PreconditionFailed,
            files.CredentialsExpiring,
            tokens.TokenInvalid,
            tokens.TokenExpired,
            OSError,
            ValueError,
        ) as exc:
            raise CommandError(f"check_storage failed at step '{name}': {exc!r}") from exc
        self.stdout.write(f"  {name}: ok")
        return result

    def _warn(self, message: str) -> None:
        self.stderr.write(f"warning: {message}")

    def _round_trip(self, probe_id, body: bytes, staging: str, final: str) -> None:
        local = files.serves_token_urls()
        now = timezone.now()
        expires_at = now + timedelta(seconds=settings.UPLOAD_URL_TTL_SECONDS)
        target = self._step(
            "sign_upload",
            lambda: files.create_upload(
                key=staging,
                content_type=PROBE_CONTENT_TYPE,
                size_bytes=len(body),
                expires_at=files.url_expiry(expires_at),
                media_id=probe_id,
            ),
        )
        if local:
            self._step("put", lambda: files.write_stream(staging, [body], limit=len(body)))
        else:
            self._remote_put(target, body, staging)
        info = self._step("stat", lambda: files.stat(staging))
        if info is None or info.size != len(body):
            raise CommandError(f"check_storage failed at step 'stat': got {info!r}")
        head = self._step(
            "read_head",
            lambda: files.read_head(staging, signatures.HEAD_BYTES, if_version=info.version),
        )
        if head != body[: signatures.HEAD_BYTES]:
            raise CommandError("check_storage failed at step 'read_head': bytes differ")
        if not local:
            self._conditional_copy(staging, final)
        self._step(
            "finalize",
            lambda: files.finalize(
                staging, final, content_type=FINAL_CONTENT_TYPE, if_version=info.version
            ),
        )
        if not local:
            self._no_overwrite(staging, final, info.version)
            stored = self._step("final_metadata", lambda: files.stat(final))
            if stored is None or stored.content_type != FINAL_CONTENT_TYPE:
                raise CommandError(
                    "check_storage failed at step 'final_metadata': the copy kept the upload's "
                    f"metadata ({stored!r}); the store must honor MetadataDirective=REPLACE."
                )
        url = self._step(
            "sign_download",
            lambda: files.download_url(
                final,
                media_id=probe_id,
                audience="moderator",
                expires_at=files.url_expiry(now + timedelta(seconds=60)),
            ),
        )
        if local:
            self._local_get(url, final, body, now)
        else:
            status, got = self._step("get", lambda: http("GET", url))
            if status != 200 or got != body:
                raise CommandError(f"check_storage failed at step 'get': HTTP {status}")
        self._step("delete", lambda: files.delete(final))

    def _remote_put(self, target, body: bytes, staging: str) -> None:
        # A missing key must read as missing: without s3:ListBucket, S3 answers 403 instead, and
        # `complete` would report storage_unavailable where it means upload_not_found.
        missing = self._step("stat_missing", lambda: files.stat(staging))
        if missing is not None:
            raise CommandError("check_storage failed at step 'stat_missing': the probe key exists")
        self._check_cors(target.url)
        oversize = body + b"!"
        status, _ = self._step(
            "size_cap",
            lambda: http("PUT", target.url, body=oversize, headers=dict(target.headers)),
        )
        if 200 <= status < 300:
            self._warn(
                "the store accepted a body larger than the signed Content-Length; `complete` "
                "still rejects it, but oversize uploads cost storage until cleanup "
                "(docs/storage.md)."
            )
        status, reply = self._step(
            "put", lambda: http("PUT", target.url, body=body, headers=dict(target.headers))
        )
        if not 200 <= status < 300:
            raise CommandError(
                f"check_storage failed at step 'put': HTTP {status} {reply[:200]!r}; check "
                "s3:PutObject and that the public endpoint is reachable from here."
            )

    def _check_cors(self, url: str) -> None:
        """Browser uploads need the bucket's CORS rule to allow each configured origin."""

        origins = settings.CORS_ALLOWED_ORIGINS
        if not origins:
            self.stdout.write("  cors: skipped (OCTOREVIEW_MEDIA_CORS_ORIGINS is empty)")
            return
        answers = self._step("cors", lambda: {origin: preflight(url, origin) for origin in origins})
        for origin, allowed in answers.items():
            if allowed not in (origin, "*"):
                self._warn(
                    f"the bucket's CORS rule does not allow {origin}; browser uploads and "
                    "downloads from it will fail. Add the rule from docs/storage.md."
                )

    def _conditional_copy(self, staging: str, final: str) -> None:
        try:
            files.finalize(
                staging, final, content_type=FINAL_CONTENT_TYPE, if_version=STALE_VERSION
            )
        except files.PreconditionFailed:
            self.stdout.write("  conditional_copy: ok")
            return
        except files.StorageError as exc:
            raise CommandError(f"check_storage failed at step 'conditional_copy': {exc!r}") from exc
        raise CommandError(
            "check_storage failed at step 'conditional_copy': the store copied a stale version, "
            "so it ignores CopySourceIfMatch. octoreview needs conditional copy to publish only "
            "validated bytes; see docs/storage.md for supported stores."
        )

    def _no_overwrite(self, staging: str, final: str, version: str) -> None:
        """A second copy onto the published key must be refused (If-None-Match, spec 7.4)."""

        try:
            files.finalize(staging, final, content_type=FINAL_CONTENT_TYPE, if_version=version)
        except files.FinalExists:
            self.stdout.write("  no_overwrite: ok")
            return
        except (files.StorageError, files.PreconditionFailed) as exc:
            raise CommandError(f"check_storage failed at step 'no_overwrite': {exc!r}") from exc
        self._warn(
            "the store overwrote a published object despite If-None-Match; the completion lease "
            "is then the only guard against a late copy replacing published media "
            "(docs/storage.md)."
        )

    def _local_get(self, url: str, final: str, body: bytes, now) -> None:
        token = url.rsplit("/", 1)[-1]
        claims = self._step(
            "verify_download_token", lambda: tokens.verify(token, tokens.DOWNLOAD, now=now)
        )
        if claims.key != final:
            raise CommandError("check_storage failed at step 'verify_download_token': wrong key")

        def read_back():
            with files.open_file(final) as handle:
                return handle.read()

        if self._step("get", read_back) != body:
            raise CommandError("check_storage failed at step 'get': bytes differ")

    def _check_lifecycle(self) -> None:
        try:
            rules = files.expiration_rules()
        except files.StorageError as exc:
            self._warn(
                f"could not read the bucket lifecycle rules ({exc}); grant "
                "s3:GetLifecycleConfiguration to let check_storage verify them."
            )
            return
        if rules is None:
            return  # the backend has no bucket lifecycle (local sweeps staging files itself)
        media = [rule for rule in rules if rule.covers(f"{files.MEDIA_PREFIX}/")]
        if media:
            names = ", ".join(repr(rule.rule_id or rule.prefix) for rule in media)
            raise CommandError(
                f"check_storage failed at step 'lifecycle': rule(s) {names} expire objects under "
                f"{files.MEDIA_PREFIX}/, which would delete published media; scope them to "
                f"{files.STAGING_PREFIX}/."
            )
        staging = [r for r in rules if f"{files.STAGING_PREFIX}/".startswith(r.prefix)]
        if not staging:
            self._warn(
                f"no lifecycle rule expires {files.STAGING_PREFIX}/; abandoned uploads and re-PUTs "
                "stay in the bucket. Add the 1-day rule from docs/storage.md."
            )
            return
        days = min((r.days for r in staging if r.days is not None), default=None)
        self.stdout.write(f"  lifecycle: ok ({files.STAGING_PREFIX}/ expires after {days} day(s))")
