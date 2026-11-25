"""Media reads: slots, storage usage, visibility and URLs (docs/spec.md 3.5, 5.2, 6.3, 7.6)."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from octoreview.core import etags
from octoreview.media import files, url_cache
from octoreview.media.models import VISIBLE_MODERATION, MediaStatus, ModerationStatus, ReviewMedia
from octoreview.reviews.models import Review, ReviewStatus

PUBLIC, AUTHOR, MODERATOR = "public", "author", "moderator"


def grace() -> timedelta:
    return timedelta(seconds=settings.UPLOAD_GRACE_SECONDS)


def live_pending(now: datetime) -> Q:
    """Pending rows that still hold a slot: within effective expiry, or under a live lease."""

    return Q(status=MediaStatus.PENDING) & (
        Q(upload_expires_at__gt=now - grace()) | Q(completion_lease_until__gt=now)
    )


def slot_counts(review_id, kind: str, *, now: datetime, exclude_id=None) -> tuple[int, int]:
    """(ready_count, pending_count) for one review and kind."""

    queryset = ReviewMedia.objects.filter(review_id=review_id, kind=kind)
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    ready = queryset.filter(status=MediaStatus.READY).count()
    pending = queryset.filter(live_pending(now)).count()
    return ready, pending


def next_slot_frees_at(review_id, kind: str, *, now: datetime) -> datetime | None:
    row = (
        ReviewMedia.objects.filter(review_id=review_id, kind=kind)
        .filter(live_pending(now))
        .order_by("upload_expires_at")
        .first()
    )
    if row is None:
        return None
    candidates = [row.upload_expires_at + grace()]
    if row.completion_lease_until and row.completion_lease_until > now:
        candidates.append(row.completion_lease_until)
    return max(candidates)


def storage_usage(tenant_id: str, application_id: str, *, now: datetime, exclude_id=None) -> dict:
    """Computed, never stored (docs/spec.md section 7.6)."""

    queryset = ReviewMedia.objects.filter(tenant_id=tenant_id, application_id=application_id)
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    stored = queryset.filter(status__in=[MediaStatus.READY, MediaStatus.DELETING]).aggregate(
        total=Sum("size_bytes")
    )["total"]
    reserved = queryset.filter(live_pending(now)).aggregate(total=Sum("size_bytes"))["total"]
    return {"stored_bytes": stored or 0, "reserved_bytes": reserved or 0}


def is_publicly_visible(media: ReviewMedia, review: Review) -> bool:
    return (
        review.status == ReviewStatus.PUBLISHED
        and media.status == MediaStatus.READY
        and media.moderation_status in VISIBLE_MODERATION
    )


def audience_for(media: ReviewMedia, review: Review, *, moderator: bool, author_path: bool):
    """Which URL audience this caller gets for this media, or None for `url: null`."""

    if media.status != MediaStatus.READY:
        return None
    if moderator:
        return MODERATOR
    if is_publicly_visible(media, review):
        return PUBLIC
    if author_path and review.status != ReviewStatus.REMOVED:
        if media.moderation_status != ModerationStatus.REJECTED:
            return AUTHOR
    return None


def media_url(media: ReviewMedia, audience: str | None, *, now: datetime) -> dict:
    """`{"url": ...}`, or `url: null` plus `url_unavailable` when signing failed."""

    if audience is None or not media.storage_key:
        return {"url": None}
    if audience == PUBLIC and settings.MEDIA_PUBLIC_BASE_URL:
        return {"url": f"{settings.MEDIA_PUBLIC_BASE_URL}/{media.storage_key}"}
    cache_key = url_cache.key(media.storage_key, media.id, audience, now)
    cached = url_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        # At least the TTL, and long enough for a body that carries an ETag (docs/spec.md 5.3).
        expires_at = files.url_expiry(etags.url_expiry_target(now))
        url = files.download_url(
            media.storage_key, media_id=media.id, audience=audience, expires_at=expires_at
        )
    except (files.StorageError, files.CredentialsExpiring):
        return {"url": None, "url_unavailable": "signing"}
    result = {"url": url, "url_expires_at": expires_at.isoformat()}
    # Only a URL that outlives its epoch may be reused within it; one capped by short-lived
    # credentials is signed per request (docs/spec.md section 8).
    if expires_at >= etags.url_lifetime_floor(now):
        url_cache.put(cache_key, result)
    return result


def media_representation(media: ReviewMedia, review: Review, *, audience, now=None) -> dict:
    now = now or timezone.now()
    return {
        "id": str(media.id),
        "review_id": str(media.review_id),
        "kind": media.kind,
        "status": media.status,
        "moderation_status": media.moderation_status,
        "content_type": media.content_type,
        "size_bytes": media.size_bytes,
        "width": media.width,
        "height": media.height,
        "duration_seconds": media.duration_seconds,
        "position": media.position,
        "upload_expires_at": media.upload_expires_at.isoformat(),
        "created_at": media.created_at.isoformat(),
        **media_url(media, audience, now=now),
    }


def embedded_media(reviews, *, moderator: bool, author_path: bool) -> dict:
    """Ready media per review for list and detail responses, capped per review.

    One query for the whole page; the cap keeps signing work bounded (docs/spec.md 5.2).
    """

    review_ids = [review.id for review in reviews]
    by_review: dict = {review_id: [] for review_id in review_ids}
    rows = ReviewMedia.objects.filter(review_id__in=review_ids, status=MediaStatus.READY).order_by(
        "review_id", "position", "created_at", "id"
    )
    for row in rows:
        by_review[row.review_id].append(row)
    now = timezone.now()
    limit = settings.LIST_MEDIA_LIMIT
    result = {}
    for review in reviews:
        rows = by_review.get(review.id, [])
        result[review.id] = {
            "media": [
                media_representation(
                    row,
                    review,
                    audience=audience_for(
                        row, review, moderator=moderator, author_path=author_path
                    ),
                    now=now,
                )
                for row in rows[:limit]
            ],
            "media_truncated": len(rows) > limit,
        }
    return result
