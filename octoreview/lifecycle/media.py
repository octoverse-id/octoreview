"""Media state changes (docs/spec.md sections 4.3, 6, 7.3, 7.4 and 7.7).

Lifecycle owns every write to a media row's status, moderation, lease and cleanup claim, and to
the review's `media_version` and `ready_media_count`. Storage calls never run under a row lock:
each operation is split into short locked transactions with the storage work between them.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from octoreview.audit import services as audit
from octoreview.core import metrics
from octoreview.core.audit_context import AuditContext
from octoreview.core.db import holding_locks
from octoreview.core.errors import DomainError, NotFoundError
from octoreview.core.media_types import MEDIA_TYPES
from octoreview.entities.models import ApplicationSettings, ModerationMode
from octoreview.entities.policy import effective_policy
from octoreview.entities.services import get_settings
from octoreview.events import services as events
from octoreview.lifecycle import transitions
from octoreview.lifecycle.services import check_expected_versions, entity_inactive
from octoreview.media import files, selectors, signatures
from octoreview.media.models import (
    VISIBLE_MODERATION,
    MediaStatus,
    ModerationStatus,
    ReviewMedia,
    StorageLocation,
)
from octoreview.reviews.models import Review, ReviewStatus

ACCEPTS_MEDIA = (ReviewStatus.PENDING, ReviewStatus.PUBLISHED)
# Orphaned part, attempt and staging files older than this are swept (docs/spec.md 7.7). Upload
# URLs live at most OCTOREVIEW_UPLOAD_URL_TTL_SECONDS, so a day leaves a wide margin.
STAGING_SWEEP_AGE_SECONDS = 24 * 3600
_completion_slots = threading.BoundedSemaphore(settings.MAX_CONCURRENT_COMPLETIONS)


# --- shared helpers -----------------------------------------------------------------------------


def _scope(row) -> dict:
    return {
        "tenant_id": row.tenant_id,
        "application_id": row.application_id,
        "namespace_type": row.namespace_type,
        "namespace_id": row.namespace_id,
    }


def _event(media: ReviewMedia, event_type: str, ctx: AuditContext, **extra) -> None:
    events.enqueue(
        **_scope(media),
        event_type=event_type,
        resource_type="review_media",
        resource_id=media.id,
        payload={
            "media_id": str(media.id),
            "review_id": str(media.review_id),
            "entity_id": str(media.review.entity_id),
            "kind": media.kind,
            "status": media.status,
            "moderation_status": media.moderation_status,
            **extra,
        },
        audit_context=ctx,
    )


def _audit(media: ReviewMedia, action: str, ctx: AuditContext, **metadata) -> None:
    audit.record(
        **_scope(media),
        action=action,
        aggregate_type="review_media",
        aggregate_id=media.id,
        audit_context=ctx,
        metadata={"review_id": str(media.review_id), **metadata},
    )


def touch_review(review: Review) -> None:
    """Bump media_version and recount publicly visible media (the single writer of both)."""

    review.media_version += 1
    review.ready_media_count = ReviewMedia.objects.filter(
        review_id=review.id, status=MediaStatus.READY, moderation_status__in=VISIBLE_MODERATION
    ).count()
    review.save(update_fields=["media_version", "ready_media_count", "updated_at"])


@contextmanager
def _locked(review_id, *, media_id=None, settings_row: bool = False):
    """Lock in the documented order: ApplicationSettings → Review → ReviewMedia."""

    review_meta = Review.objects.filter(pk=review_id).values("tenant_id", "application_id").first()
    if review_meta is None:
        raise NotFoundError()
    if settings_row:
        ApplicationSettings.objects.select_for_update().filter(
            tenant_id=review_meta["tenant_id"], application_id=review_meta["application_id"]
        ).first()
    # `of=("self",)`: without it PostgreSQL's FOR UPDATE also locks the joined entity row, which
    # inverts the Entity → Review order and deadlocks against review writes.
    review = (
        Review.objects.select_for_update(of=("self",)).select_related("entity").get(pk=review_id)
    )
    media = None
    if media_id is not None:
        media = ReviewMedia.objects.select_for_update().filter(pk=media_id, review=review).first()
        if media is None:
            raise NotFoundError()
    with holding_locks():
        yield review, media


def _metric_scope(media: ReviewMedia) -> dict:
    return {
        "tenant_id": media.tenant_id,
        "application_id": media.application_id,
        "kind": media.kind,
        "backend": files.backend_name(),
    }


def _storage_error(exc: files.StorageError) -> DomainError:
    return DomainError(
        "The storage backend failed; retry after the Retry-After delay.",
        {"backend": files.backend_name(), "operation": exc.operation},
        code="storage_unavailable",
        retry_after=5,
    )


def _credentials_expiring() -> DomainError:
    return DomainError(
        "Storage credentials are about to expire; retry shortly.",
        code="storage_credentials_expiring",
        retry_after=30,
    )


def _location() -> StorageLocation:
    location, _ = StorageLocation.objects.get_or_create(
        id=settings.STORAGE_LOCATION_ID, defaults={"backend": files.backend_name()}
    )
    return location


def _quota_set(review: Review) -> int | None:
    row = get_settings(review.tenant_id, review.application_id)
    return row.max_storage_bytes if row is not None else None


# --- intent -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentRequest:
    kind: str
    content_type: str
    size_bytes: int
    client_upload_id: str | None = None
    position: int = 0
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class IntentResult:
    media: ReviewMedia
    target: files.UploadTarget | None  # None when a replayed row is no longer uploadable
    created: bool  # False when a client_upload_id retry replayed an existing row


def _check_review_accepts_media(review: Review) -> None:
    if review.status not in ACCEPTS_MEDIA:
        raise DomainError(
            f"A {review.status} review does not accept media; only pending and published "
            "reviews do.",
            {"status": review.status},
            code="review_not_accepting_media",
        )
    if not review.entity.is_active:
        raise entity_inactive(review.entity)


def create_intent(review_id, request: IntentRequest, ctx: AuditContext) -> IntentResult:
    """Upload intent: validate, sign before any lock, then reserve the slot in one transaction."""

    now = timezone.now()
    review = Review.objects.select_related("entity").get(pk=review_id)
    _check_review_accepts_media(review)
    media_type = MEDIA_TYPES.get(request.content_type)
    app_settings = get_settings(review.tenant_id, review.application_id)
    policy = effective_policy(review.entity, app_settings)
    if media_type is None or request.content_type not in policy.allowed_content_types:
        raise DomainError(
            "This content type is not allowed; convert the file to one of the allowed types.",
            {"allowed_content_types": list(policy.allowed_content_types)},
            code="content_type_not_allowed",
        )
    if media_type.kind != request.kind:
        raise DomainError(
            f"{request.content_type} is a {media_type.kind}; send kind={media_type.kind}.",
            {"fields": {"kind": [f"Must be {media_type.kind} for {request.content_type}."]}},
        )
    max_bytes = policy.max_bytes_for(request.kind)
    if request.size_bytes > max_bytes:
        raise DomainError(
            f"The file is larger than {max_bytes} bytes; compress or trim it.",
            {"max_bytes": max_bytes, "kind": request.kind},
            code="media_too_large",
        )

    if request.client_upload_id:
        existing = _existing_upload(review.id, request.client_upload_id)
        if existing is not None:
            return IntentResult(existing, _replay_target(existing, now), created=False)

    free = files.free_bytes()
    if free is not None and free < request.size_bytes:
        raise DomainError(
            "The media volume is nearly full; free space or move to a cloud backend.",
            {"backend": files.backend_name(), "operation": "free_space"},
            code="storage_unavailable",
            retry_after=60,
        )

    # Sign before the transaction: a signing failure has no side effects (spec section 7.3).
    media_id = uuid.uuid4()
    staging = files.staging_key(review.tenant_id, review.application_id, media_id)
    requested = now + timedelta(seconds=settings.UPLOAD_URL_TTL_SECONDS)
    try:
        expires_at = files.url_expiry(requested)
        target = files.create_upload(
            key=staging,
            content_type=request.content_type,
            size_bytes=request.size_bytes,
            expires_at=expires_at,
            media_id=media_id,
        )
    except files.CredentialsExpiring:
        raise _credentials_expiring()
    except files.StorageError as exc:
        raise _storage_error(exc)

    location = _location()
    quota = app_settings.max_storage_bytes if app_settings is not None else None
    replayed = None
    with transaction.atomic(), _locked(review.id, settings_row=quota is not None) as (review, _):
        _check_review_accepts_media(review)
        limit = policy.limit_for(request.kind)
        ready, pending = selectors.slot_counts(review.id, request.kind, now=now)
        if ready + pending >= limit:
            raise DomainError(
                f"This review already has its {limit} {request.kind} slot(s) in use; delete one "
                "or wait for an abandoned upload to expire.",
                {
                    "kind": request.kind,
                    "limit": limit,
                    "ready_count": ready,
                    "pending_count": pending,
                    "next_slot_frees_at": _iso(
                        selectors.next_slot_frees_at(review.id, request.kind, now=now)
                    ),
                    "policy_source": policy.sources[f"max_{request.kind}s"],
                },
                code="media_limit_exceeded",
            )
        if quota is not None:
            usage = selectors.storage_usage(review.tenant_id, review.application_id, now=now)
            if usage["stored_bytes"] + usage["reserved_bytes"] + request.size_bytes > quota:
                # Emitted now, not on commit: this transaction rolls back with the 409.
                metrics.emit_metric(
                    metrics.QUOTA_REJECTED,
                    tenant_id=review.tenant_id,
                    application_id=review.application_id,
                    kind=request.kind,
                    backend=files.backend_name(),
                    stage="intent",
                )
                raise DomainError(
                    "The application's storage quota is full; delete media or raise "
                    "max_storage_bytes.",
                    {
                        "max_storage_bytes": quota,
                        **usage,
                        "requested_bytes": request.size_bytes,
                    },
                    code="storage_quota_exceeded",
                )
        try:
            with transaction.atomic():
                media = ReviewMedia.objects.create(
                    id=media_id,
                    **_scope(review),
                    review=review,
                    kind=request.kind,
                    status=MediaStatus.PENDING,
                    storage_location=location,
                    staging_key=staging,
                    content_type=request.content_type,
                    size_bytes=request.size_bytes,
                    width=request.width,
                    height=request.height,
                    duration_seconds=request.duration_seconds,
                    position=request.position,
                    client_upload_id=request.client_upload_id,
                    upload_expires_at=target.expires_at,
                )
        except IntegrityError:
            # A concurrent retry with the same client_upload_id won; replay its row once the
            # locks are released (re-signing is a storage call).
            replayed = _existing_upload(review.id, request.client_upload_id)
            if replayed is None:
                raise
        else:
            _audit(media, "review_media.intent", ctx, size_bytes=request.size_bytes)
            metrics.emit_on_commit(
                metrics.MEDIA_INTENT, **_metric_scope(media), size_bytes=request.size_bytes
            )
    if replayed is not None:
        return IntentResult(replayed, _replay_target(replayed, now), created=False)
    return IntentResult(media, target, created=True)


def _existing_upload(review_id, client_upload_id: str) -> ReviewMedia | None:
    return (
        ReviewMedia.objects.filter(review_id=review_id, client_upload_id=client_upload_id)
        .exclude(status=MediaStatus.DELETED)
        .first()
    )


def _replay_target(media: ReviewMedia, now) -> files.UploadTarget | None:
    """A retried intent gets a fresh URL for the same staging key, valid until the row's expiry.

    The retry exists because the first response can be lost, and that response carried the only
    upload URL. Rows that are no longer uploadable (completed, expired) replay without one.
    """

    if media.status != MediaStatus.PENDING or media.upload_expires_at <= now:
        return None
    try:
        return files.create_upload(
            key=media.staging_key,
            content_type=media.content_type,
            size_bytes=media.size_bytes,
            # The signing credential may now expire before the row does (spec section 7.8).
            expires_at=files.url_expiry(media.upload_expires_at),
            media_id=media.id,
        )
    except files.CredentialsExpiring:
        raise _credentials_expiring()
    except files.StorageError as exc:
        raise _storage_error(exc)


def _iso(value):
    return value.isoformat() if value is not None else None


def effective_expiry(media: ReviewMedia):
    return media.upload_expires_at + selectors.grace()


# --- complete -----------------------------------------------------------------------------------


@contextmanager
def completion_slot():
    if not _completion_slots.acquire(blocking=False):
        raise DomainError(
            "Too many uploads are completing on this server; retry after the delay.",
            code="too_many_completions",
            retry_after=5,
        )
    try:
        yield
    finally:
        _completion_slots.release()


def _invalid(media: ReviewMedia, reason: str) -> DomainError:
    return DomainError(
        f"The upload was rejected ({reason}); upload a matching file with a new intent.",
        {"reason": reason},
        code="media_rejected",
    )


def complete(review_id, media_id, ctx: AuditContext) -> ReviewMedia:
    with completion_slot():
        media, attempt = _phase_one(review_id, media_id)
        if attempt is None:
            return media
        try:
            outcome = _phase_two(media)
        except DomainError:
            _release_lease(review_id, media_id, attempt)
            raise
        return _phase_three(review_id, media_id, attempt, outcome, ctx)


def _phase_one(review_id, media_id):
    now = timezone.now()
    with transaction.atomic(), _locked(review_id, media_id=media_id) as (review, media):
        if media.status == MediaStatus.READY:
            return media, None
        if media.status == MediaStatus.INVALID:
            raise _invalid(media, media.invalid_reason or "invalid")
        if media.status != MediaStatus.PENDING:
            raise NotFoundError()
        if media.completion_lease_until and media.completion_lease_until > now:
            raise DomainError(
                "Another request is completing this upload; retry after the delay.",
                code="complete_in_progress",
                retry_after=max(1, int((media.completion_lease_until - now).total_seconds())),
            )
        if now > effective_expiry(media):
            raise DomainError(
                "The upload URL expired; create a new intent and upload again.",
                {"upload_expires_at": media.upload_expires_at.isoformat()},
                code="upload_expired",
            )
        _check_review_accepts_media(review)
        attempt = uuid.uuid4()
        media.completion_attempt_id = attempt
        media.completion_lease_until = now + timedelta(seconds=settings.COMPLETE_LEASE_SECONDS)
        media.save(update_fields=["completion_attempt_id", "completion_lease_until", "updated_at"])
        return media, attempt


@dataclass(frozen=True)
class Outcome:
    final_key: str | None
    invalid_reason: str | None = None


def _phase_two(media: ReviewMedia) -> Outcome:
    """Validate the staged bytes, then copy exactly that version to the final key (no locks)."""

    try:
        info = files.stat(media.staging_key)
        if info is None:
            raise DomainError(
                "No uploaded file was found; PUT the file to upload.url, then complete.",
                code="upload_not_found",
            )
        if info.size != media.size_bytes:
            return Outcome(final_key=None, invalid_reason="size_mismatch")
        head = files.read_head(
            media.staging_key, min(info.size, signatures.HEAD_BYTES), if_version=info.version
        )
        if signatures.detect(head) != media.content_type:
            return Outcome(final_key=None, invalid_reason="type_mismatch")
        review = media.review
        final = files.final_key(
            media.tenant_id,
            media.application_id,
            review.entity_id,
            review.id,
            media.id,
            MEDIA_TYPES[media.content_type].extension,
        )
        try:
            files.finalize(
                media.staging_key, final, content_type=media.content_type, if_version=info.version
            )
        except files.FinalExists:
            # An earlier attempt already published this media's bytes (its lease ended during the
            # copy, or it died before phase 3). Published objects are never overwritten, so the
            # bytes to validate are the ones already there.
            return _validate_published(media, final)
        return Outcome(final_key=final)
    except files.PreconditionFailed:
        raise DomainError(
            "The file changed while it was being validated; call complete again.",
            code="upload_changed",
        )
    except files.StorageError as exc:
        raise _storage_error(exc)


def _validate_published(media: ReviewMedia, final: str) -> Outcome:
    info = files.stat(final)
    if info is None:
        raise files.PreconditionFailed()  # removed meanwhile; the client completes again
    if info.size != media.size_bytes:
        return Outcome(final_key=final, invalid_reason="size_mismatch")
    head = files.read_head(final, min(info.size, signatures.HEAD_BYTES), if_version=info.version)
    if signatures.detect(head) != media.content_type:
        return Outcome(final_key=final, invalid_reason="type_mismatch")
    return Outcome(final_key=final)


def _release_lease(review_id, media_id, attempt) -> None:
    with transaction.atomic(), _locked(review_id, media_id=media_id) as (_review, media):
        if media.completion_attempt_id == attempt:
            media.completion_attempt_id = None
            media.completion_lease_until = None
            media.save(
                update_fields=["completion_attempt_id", "completion_lease_until", "updated_at"]
            )


def _phase_three(review_id, media_id, attempt, outcome: Outcome, ctx) -> ReviewMedia:
    now = timezone.now()
    review_meta = Review.objects.get(pk=review_id)
    quota = _quota_set(review_meta)
    delete_after = []
    error = None
    with (
        transaction.atomic(),
        _locked(review_id, media_id=media_id, settings_row=quota is not None) as (review, media),
    ):
        if media.status != MediaStatus.PENDING or media.completion_attempt_id != attempt:
            # Another attempt finished first; its result stands, ours is discarded.
            if outcome.final_key and outcome.final_key != media.storage_key:
                delete_after.append(outcome.final_key)
            result = media
        else:
            reason = outcome.invalid_reason
            if reason is None:
                try:
                    _check_review_accepts_media(review)
                except DomainError as exc:
                    media.completion_attempt_id = None
                    media.completion_lease_until = None
                    media.save(
                        update_fields=[
                            "completion_attempt_id",
                            "completion_lease_until",
                            "updated_at",
                        ]
                    )
                    delete_after.append(outcome.final_key)
                    error = exc
                else:
                    policy = effective_policy(
                        review.entity, get_settings(review.tenant_id, review.application_id)
                    )
                    ready, _ = selectors.slot_counts(
                        review.id, media.kind, now=now, exclude_id=media.id
                    )
                    if ready >= policy.limit_for(media.kind):
                        reason = "over_limit"
                    elif quota is not None:
                        usage = selectors.storage_usage(
                            review.tenant_id, review.application_id, now=now, exclude_id=media.id
                        )
                        if usage["stored_bytes"] + media.size_bytes > quota:
                            reason = "over_quota"
            if error is None and reason is not None:
                media.status = MediaStatus.INVALID
                media.invalid_reason = reason
                metrics.emit_on_commit(
                    metrics.MEDIA_REJECTED, **_metric_scope(media), reason=reason
                )
                if reason == "over_quota":
                    metrics.emit_on_commit(
                        metrics.QUOTA_REJECTED, **_metric_scope(media), stage="complete"
                    )
                media.completion_attempt_id = None
                media.completion_lease_until = None
                media.save()
                touch_review(review)
                _audit(media, "review_media.invalid", ctx, reason=reason)
                _event(media, "review_media.invalid", ctx, reason=reason)
                delete_after += [media.staging_key, outcome.final_key]
                error = _invalid(media, reason)
            elif error is None:
                settings_row = get_settings(review.tenant_id, review.application_id)
                pre_moderated = (
                    settings_row is not None
                    and settings_row.effective_moderation_mode == ModerationMode.PRE_MODERATION
                )
                media.status = MediaStatus.READY
                media.storage_key = outcome.final_key
                media.moderation_status = (
                    ModerationStatus.PENDING
                    if pre_moderated and review.status == ReviewStatus.PUBLISHED
                    else ModerationStatus.NONE
                )
                media.completion_attempt_id = None
                media.completion_lease_until = None
                media.save()
                touch_review(review)
                _audit(media, "review_media.ready", ctx)
                _event(media, "review_media.ready", ctx)
                metrics.emit_on_commit(
                    metrics.MEDIA_READY, **_metric_scope(media), size_bytes=media.size_bytes
                )
                delete_after.append(media.staging_key)
            result = media
    _delete_quietly(delete_after)
    if error is not None:
        raise error
    return result


def _delete_quietly(keys) -> None:
    """Best-effort deletes after commit; lifecycle rules and cleanup_media back them up."""

    for key in keys:
        if not key:
            continue
        try:
            files.delete(key)
        except files.StorageError:
            pass


# --- delete and moderation ----------------------------------------------------------------------


def delete_media(review_id, media_id, ctx: AuditContext) -> ReviewMedia:
    with transaction.atomic(), _locked(review_id, media_id=media_id) as (review, media):
        if review.status == ReviewStatus.REMOVED:
            raise transitions.invalid_transition(review.status, "delete_media")
        if media.status in (MediaStatus.DELETED, MediaStatus.DELETING):
            return media
        if media.status == MediaStatus.INVALID:
            media.status = MediaStatus.DELETED
            media.save()
            touch_review(review)
            return media
        media.status = MediaStatus.DELETING
        media.completion_attempt_id = None
        media.completion_lease_until = None
        media.save()
        touch_review(review)
        _audit(media, "review_media.deleting", ctx)
    return finish_delete(media, ctx)


def finish_delete(media: ReviewMedia, ctx: AuditContext) -> ReviewMedia:
    """Delete the objects outside any lock; the row becomes `deleted` only after success."""

    try:
        for key in (media.storage_key, media.staging_key):
            if key:
                files.delete(key)
    except files.StorageError:
        return media  # stays `deleting`; cleanup_media retries it
    with transaction.atomic(), _locked(media.review_id, media_id=media.id) as (review, row):
        if row.status != MediaStatus.DELETING:
            return row
        row.status = MediaStatus.DELETED
        row.save()
        touch_review(review)
        _event(row, "review_media.deleted", ctx)
        return row


def moderate_media(
    review_id,
    media_id,
    action: str,
    ctx: AuditContext,
    *,
    reason=None,
    expected_version=None,
    expected_media_version=None,
) -> ReviewMedia:
    with transaction.atomic(), _locked(review_id, media_id=media_id) as (review, media):
        if media.status != MediaStatus.READY:
            raise DomainError(
                f"Only ready media can be moderated; this media is {media.status}.",
                {"from_status": media.status, "action": action, "allowed_actions": []},
                code="invalid_transition",
            )
        if action == "approve":
            check_expected_versions(review, expected_version, expected_media_version)
            media.moderation_status = ModerationStatus.APPROVED
            media.moderation_reason = None
        else:
            media.moderation_status = ModerationStatus.REJECTED
            media.moderation_reason = reason
        media.save()
        touch_review(review)
        _audit(media, f"review_media.{action}", ctx, reason=reason)
        _event(media, "review_media.moderated", ctx)
        return media


# --- cleanup ------------------------------------------------------------------------------------


@dataclass
class CleanupResult:
    deleted: int = 0
    swept: int = 0
    failed: list = None

    def __post_init__(self):
        self.failed = self.failed or []


def _claim(queryset, now) -> list:
    """Claim a batch in its own short transaction; leased or claimed rows are skipped."""

    unclaimed = Q(cleanup_claimed_until__isnull=True) | Q(cleanup_claimed_until__lt=now)
    with transaction.atomic():
        # Lock only the media rows; the candidate filter joins reviews.
        rows = list(
            queryset.filter(unclaimed).select_for_update(skip_locked=True, of=("self",))[:200]
        )
        for row in rows:
            row.cleanup_claimed_until = now + timedelta(minutes=10)
            row.save(update_fields=["cleanup_claimed_until"])
    return [row.id for row in rows]


def cleanup_media(ctx: AuditContext | None = None) -> CleanupResult:
    """Expired pending uploads, media of removed reviews, and `deleting` retries."""

    ctx = ctx or AuditContext(actor_id="cleanup_media")
    now = timezone.now()
    result = CleanupResult()
    expired_cutoff = now - selectors.grace() - timedelta(seconds=settings.CLEANUP_DELAY_SECONDS)
    candidates = ReviewMedia.objects.filter(
        Q(
            status=MediaStatus.PENDING,
            upload_expires_at__lt=expired_cutoff,
        )
        & (Q(completion_lease_until__isnull=True) | Q(completion_lease_until__lt=now))
        | Q(status=MediaStatus.DELETING)
        | Q(
            review__status=ReviewStatus.REMOVED,
            status__in=[MediaStatus.READY, MediaStatus.PENDING],
        )
    ).order_by("id")
    for media_id in _claim(candidates, now):
        media = ReviewMedia.objects.select_related("review").get(pk=media_id)
        if media.status != MediaStatus.DELETING:
            with transaction.atomic(), _locked(media.review_id, media_id=media.id) as (review, row):
                # The claim read the row without its review lock; a completion lease or a
                # status change since then wins, and the row is left for a later run.
                if not _still_eligible(row, review, now, expired_cutoff):
                    continue
                row.status = MediaStatus.DELETING
                row.save()
                touch_review(review)
                media = row
        media = finish_delete(media, ctx)
        if media.status == MediaStatus.DELETED:
            result.deleted += 1
        else:
            result.failed.append(media.storage_key or media.staging_key)
    try:
        result.swept = files.sweep_staging(now - timedelta(seconds=STAGING_SWEEP_AGE_SECONDS))
    except files.StorageError as exc:
        result.failed.append(f"sweep:{exc.operation}")
    backend = files.backend_name()
    metrics.emit_metric(
        metrics.CLEANUP_DELETED, count=result.deleted, swept=result.swept, backend=backend
    )
    metrics.emit_metric(metrics.CLEANUP_FAILED, count=len(result.failed), backend=backend)
    return result


def _still_eligible(row: ReviewMedia, review: Review, now, expired_cutoff) -> bool:
    if row.status == MediaStatus.DELETING:
        return True
    if review.status == ReviewStatus.REMOVED:
        return row.status in (MediaStatus.READY, MediaStatus.PENDING)
    if row.status != MediaStatus.PENDING or row.upload_expires_at >= expired_cutoff:
        return False
    return row.completion_lease_until is None or row.completion_lease_until < now
