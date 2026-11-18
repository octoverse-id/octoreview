"""Signed local media tokens (docs/spec.md section 7.5).

A token binds purpose, media id, storage key, declared size or audience, expiry and key id. It
is `<kid>.<payload>.<signature>`: the signature is an HMAC over a purpose-specific prefix, so an
upload token can never be used as a download token. The previous secret (`kid = p`) is accepted
for verification during rotation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from django.conf import settings

UPLOAD = "upload"
DOWNLOAD = "download"
AUDIENCES = ("public", "author", "moderator")


class TokenInvalid(Exception):
    pass


class TokenExpired(Exception):
    pass


@dataclass(frozen=True)
class Token:
    purpose: str
    media_id: str
    key: str
    expires_at: datetime
    size_bytes: int | None = None
    audience: str | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _secrets() -> dict[str, str]:
    secrets = {"c": settings.MEDIA_TOKEN_SECRET}
    if settings.MEDIA_TOKEN_SECRET_PREVIOUS:
        secrets["p"] = settings.MEDIA_TOKEN_SECRET_PREVIOUS
    return secrets


def _sign(secret: str, purpose: str, payload: str) -> str:
    message = f"octoreview.media.{purpose}|{payload}".encode()
    return _b64(hmac.new(secret.encode(), message, hashlib.sha256).digest())


def _issue(purpose: str, claims: dict) -> str:
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"c.{payload}.{_sign(settings.MEDIA_TOKEN_SECRET, purpose, payload)}"


def upload_token(*, media_id, key: str, size_bytes: int, expires_at: datetime) -> str:
    return _issue(
        UPLOAD,
        {"m": str(media_id), "k": key, "s": size_bytes, "e": int(expires_at.timestamp())},
    )


def download_token(*, media_id, key: str, audience: str, expires_at: datetime) -> str:
    if audience not in AUDIENCES:
        raise ValueError(f"Unknown audience {audience!r}")
    return _issue(
        DOWNLOAD,
        {"m": str(media_id), "k": key, "a": audience, "e": int(expires_at.timestamp())},
    )


def verify(token: str, purpose: str, *, now: datetime) -> Token:
    try:
        kid, payload, signature = token.split(".")
    except ValueError as exc:
        raise TokenInvalid() from exc
    secret = _secrets().get(kid)
    if secret is None or not hmac.compare_digest(_sign(secret, purpose, payload), signature):
        raise TokenInvalid()
    try:
        claims = json.loads(_unb64(payload))
        expires_at = datetime.fromtimestamp(int(claims["e"]), tz=UTC)
        parsed = Token(
            purpose=purpose,
            media_id=str(claims["m"]),
            key=str(claims["k"]),
            expires_at=expires_at,
            size_bytes=claims.get("s"),
            audience=claims.get("a"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenInvalid() from exc
    if now >= expires_at:
        raise TokenExpired()
    return parsed
