"""`manage.py check_storage`: a real round trip through the configured backend (spec section 11).

Each step is named so an operator sees exactly which permission or setting is wrong. The local
backend's upload and download URLs point back at this server, so for `local` the command drives
the same code the token views run (stream write, token round trip, open) instead of HTTP.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from octoreview.media import files, signatures, tokens

PROBE_CONTENT_TYPE = "application/octet-stream"


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

    def _round_trip(self, probe_id, body: bytes, staging: str, final: str) -> None:
        now = timezone.now()
        expires_at = now + timedelta(seconds=settings.UPLOAD_URL_TTL_SECONDS)
        self._step(
            "sign_upload",
            lambda: files.create_upload(
                key=staging,
                content_type=PROBE_CONTENT_TYPE,
                size_bytes=len(body),
                expires_at=files.url_expiry(expires_at),
                media_id=probe_id,
            ),
        )
        self._step("put", lambda: files.write_stream(staging, [body], limit=len(body)))
        info = self._step("stat", lambda: files.stat(staging))
        if info is None or info.size != len(body):
            raise CommandError(f"check_storage failed at step 'stat': got {info!r}")
        head = self._step(
            "read_head",
            lambda: files.read_head(staging, signatures.HEAD_BYTES, if_version=info.version),
        )
        if head != body[: signatures.HEAD_BYTES]:
            raise CommandError("check_storage failed at step 'read_head': bytes differ")
        self._step(
            "finalize",
            lambda: files.finalize(
                staging, final, content_type=PROBE_CONTENT_TYPE, if_version=info.version
            ),
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
        self._step("delete", lambda: files.delete(final))
