"""Validators for cacheable reads: ETag, If-None-Match and 304 (docs/spec.md section 5.3).

A view computes the ETag from rows it has already loaded, before it builds the body, so a 304
skips the embedded-media query and URL signing. Signed media URLs are the one part of a body
that ages: the signing epoch is in the hash, and a body only gets an ETag when every URL in it
outlives the epoch by `max(TTL/2, max-age)` (`url_lifetime_floor`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.utils.http import parse_etags
from rest_framework.response import Response

MAX_AGE_SECONDS = 60
CACHE_CONTROL = f"private, max-age={MAX_AGE_SECONDS}"


def epoch_seconds() -> int:
    # OCTOREVIEW_MEDIA_URL_TTL_SECONDS is at least 60, so an epoch lasts at least 30 seconds.
    return settings.MEDIA_URL_TTL_SECONDS // 2


def signing_epoch(now: datetime) -> int:
    """`floor(now / (TTL/2))`: a new epoch means new signed URLs, so a new ETag."""

    return int(now.timestamp()) // epoch_seconds()


def url_lifetime_floor(now: datetime) -> datetime:
    """The earliest expiry a signed URL may have in a body that carries an ETag.

    A 304 can come up to the end of the epoch, and the client may then reuse the body for
    max-age, so every URL in it must outlive the epoch end by max(TTL/2, max-age).
    """

    length = epoch_seconds()
    end = datetime.fromtimestamp((signing_epoch(now) + 1) * length, tz=UTC)
    return end + timedelta(seconds=max(length, MAX_AGE_SECONDS))


def url_expiry_target(now: datetime) -> datetime:
    """The expiry to request for a read URL signed at `now`: the TTL, or the floor if later.

    The floor is later only when TTL < 2 * max-age (TTL under 120 seconds).
    """

    ttl = now + timedelta(seconds=settings.MEDIA_URL_TTL_SECONDS)
    return max(ttl, url_lifetime_floor(now))


def deployment() -> tuple:
    """What changes every body without touching a row: the release and the URL settings."""

    return (
        settings.SPECTACULAR_SETTINGS["VERSION"],
        settings.STORAGE_BACKEND,
        settings.MEDIA_URL_TTL_SECONDS,
        settings.LIST_MEDIA_LIMIT,
        settings.MEDIA_PUBLIC_BASE_URL,
        settings.PUBLIC_BASE_URL,
    )


def etag(*parts) -> str:
    """A strong ETag over JSON-serializable parts."""

    encoded = json.dumps(parts, default=str, separators=(",", ":"), sort_keys=True)
    return '"' + hashlib.sha256(encoded.encode()).hexdigest()[:32] + '"'


def _opaque(tag: str) -> str:
    # If-None-Match uses the weak comparison (RFC 9110 section 13.1.2): W/"x" matches "x".
    return tag[2:] if tag.startswith("W/") else tag


def matches(request, tag: str) -> bool:
    """True when If-None-Match names this ETag, or is `*` (the resource exists)."""

    header = request.headers.get("If-None-Match")
    if not header:
        return False
    candidates = parse_etags(header)
    return "*" in candidates or _opaque(tag) in {_opaque(c) for c in candidates}


def not_modified(tag: str) -> Response:
    # A 304 repeats the validator and the caching headers of the 200 it stands for
    # (RFC 9110 section 15.4.5); the middleware adds Vary.
    return Response(status=304, headers={"ETag": tag, "Cache-Control": CACHE_CONTROL})


def media_urls_outlive(media_items, now: datetime) -> bool:
    """Every URL in these media representations was signed and outlives the floor."""

    floor = url_lifetime_floor(now)
    for item in media_items:
        if item.get("url_unavailable"):
            return False
        expires_at = item.get("url_expires_at")
        if expires_at and datetime.fromisoformat(expires_at) < floor:
            return False
    return True


def with_validators(response: Response, tag: str, *, valid: bool = True) -> Response:
    """Cache-Control always; the ETag only when the body may be revalidated (`valid`)."""

    response["Cache-Control"] = CACHE_CONTROL
    if valid:
        response["ETag"] = tag
    return response
