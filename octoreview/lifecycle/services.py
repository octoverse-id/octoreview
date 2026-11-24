"""Review writes: create, edit, moderation and revisions (docs/spec.md sections 4.1-4.4).

Every change runs in one transaction under the entity and review row locks, moves the rating
aggregates through `apply_delta`, and writes its audit rows and thin outbox events in the same
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from octoreview.audit import services as audit
from octoreview.core import metrics
from octoreview.core.audit_context import AuditContext
from octoreview.core.db import unique_violation
from octoreview.core.errors import DomainError
from octoreview.entities import ratings
from octoreview.entities.models import Entity, ModerationMode
from octoreview.entities.ratings import Contribution, apply_delta
from octoreview.entities.services import get_settings
from octoreview.events import services as events
from octoreview.lifecycle import transitions
from octoreview.lifecycle.locks import locked_entity, locked_review
from octoreview.reviews.models import (
    REVIEW_UNIQUE_CONSTRAINTS,
    Review,
    ReviewRevision,
    ReviewStatus,
)

CONTENT_FIELDS = ("rating", "title", "body")
IMMEDIATE_FIELDS = ("metadata", "author_metadata", "is_verified_purchase")
AUDITED_FIELDS = (
    "rating",
    "title",
    "body",
    "status",
    "status_reason",
    "metadata",
    "author_metadata",
    "is_verified_purchase",
    "source_ref",
    "version",
)


def contribution(review: Review) -> Contribution | None:
    if review.status != ReviewStatus.PUBLISHED:
        return None
    return Contribution(review.rating, review.is_verified_purchase)


def review_event_payload(review: Review) -> dict:
    """Thin: ids, status, rating and versions; never text or author data (spec section 10)."""

    return {
        "review_id": str(review.id),
        "entity_id": str(review.entity_id),
        "entity_type": review.entity.entity_type,
        "external_id": review.entity.external_id,
        "status": review.status,
        "rating": review.rating,
        "is_verified_purchase": review.is_verified_purchase,
        "version": review.version,
        "media_version": review.media_version,
    }


def _scope(review: Review) -> dict:
    return {
        "tenant_id": review.tenant_id,
        "application_id": review.application_id,
        "namespace_type": review.namespace_type,
        "namespace_id": review.namespace_id,
    }


def _emit(review: Review, event_type: str, ctx: AuditContext, **extra) -> None:
    events.enqueue(
        **_scope(review),
        event_type=event_type,
        resource_type="review",
        resource_id=review.id,
        payload={**review_event_payload(review), **extra},
        audit_context=ctx,
    )
    metric = _REVIEW_METRICS.get(event_type)
    if event_type == "review.status_changed" and review.status == ReviewStatus.REJECTED:
        metric = metrics.REVIEW_REJECTED
    if metric is not None:
        metrics.emit_on_commit(
            metric, tenant_id=review.tenant_id, application_id=review.application_id
        )


_REVIEW_METRICS = {
    "review.created": metrics.REVIEW_CREATED,
    "review.published": metrics.REVIEW_PUBLISHED,
}


def emit_rating_changed(entity: Entity, ctx: AuditContext) -> None:
    entity.refresh_from_db()
    summary = ratings.summary(entity)
    events.enqueue(
        tenant_id=entity.tenant_id,
        application_id=entity.application_id,
        namespace_type=entity.namespace_type,
        namespace_id=entity.namespace_id,
        event_type="entity.rating_changed",
        resource_type="entity",
        resource_id=entity.id,
        payload={
            "entity_id": str(entity.id),
            "entity_type": entity.entity_type,
            "external_id": entity.external_id,
            "rating_count": summary["rating_count"],
            "rating_average": summary["rating_average"],
            "histogram": summary["histogram"],
            "verified_review_count": summary["verified_review_count"],
        },
        audit_context=ctx,
    )


def _snapshot(review: Review) -> dict:
    return {field: getattr(review, field) for field in AUDITED_FIELDS}


def _audit(review: Review, action: str, ctx: AuditContext, changes: dict, **metadata) -> None:
    audit.record(
        **_scope(review),
        action=action,
        aggregate_type="review",
        aggregate_id=review.id,
        audit_context=ctx,
        changes=changes,
        metadata=metadata,
    )


def _move_aggregates(entity: Entity, before, after, ctx: AuditContext) -> None:
    if apply_delta(entity, remove=before, add=after):
        emit_rating_changed(entity, ctx)


def _review_exists(review: Review) -> DomainError:
    existing = (
        Review.objects.filter(
            entity_id=review.entity_id, author_id=review.author_id, source_ref=review.source_ref
        )
        .exclude(status=ReviewStatus.REMOVED)
        .values_list("id", flat=True)
        .first()
    )
    return DomainError(
        "This author already has a review for this entity (and order); update it with PATCH "
        "instead of creating another.",
        {"review_id": str(existing) if existing else None},
        code="review_exists",
    )


def entity_inactive(entity: Entity) -> DomainError:
    return DomainError(
        f"The entity is inactive; reactivate it with PATCH /entities/{entity.id} "
        '{"is_active": true}.',
        {"entity_id": str(entity.id)},
        code="entity_inactive",
    )


@dataclass(frozen=True)
class NewReview:
    author_id: str
    rating: int
    title: str | None = None
    body: str | None = None
    metadata: dict | None = None
    author_metadata: dict | None = None
    source_ref: str | None = None
    is_verified_purchase: bool = False


def create_review(entity: Entity, data: NewReview, ctx: AuditContext) -> Review:
    if not entity.is_active:
        raise entity_inactive(entity)
    settings = get_settings(entity.tenant_id, entity.application_id)
    mode = settings.effective_moderation_mode if settings else ModerationMode.AUTO_PUBLISH
    status = ReviewStatus.PUBLISHED if mode == ModerationMode.AUTO_PUBLISH else ReviewStatus.PENDING
    with transaction.atomic(), locked_entity(entity.id, tenant_id=entity.tenant_id) as locked:
        # The entity may have been deactivated since the caller resolved it; the locked row
        # decides (docs/spec.md section 4.2).
        if not locked.is_active:
            raise entity_inactive(locked)
        review = Review(
            tenant_id=locked.tenant_id,
            application_id=locked.application_id,
            namespace_type=locked.namespace_type,
            namespace_id=locked.namespace_id,
            entity=locked,
            author_id=data.author_id,
            rating=data.rating,
            title=data.title,
            body=data.body,
            metadata=data.metadata or {},
            author_metadata=data.author_metadata or {},
            source_ref=data.source_ref,
            is_verified_purchase=data.is_verified_purchase,
            status=status,
            published_at=timezone.now() if status == ReviewStatus.PUBLISHED else None,
        )
        try:
            with transaction.atomic():
                review.save(force_insert=True)
        except IntegrityError as exc:
            if unique_violation(exc) in REVIEW_UNIQUE_CONSTRAINTS:
                raise _review_exists(review) from exc
            raise
        _audit(review, "review.created", ctx, audit.diff({}, _snapshot(review)))
        if data.source_ref is not None or data.is_verified_purchase:
            _audit(
                review,
                "review.attested",
                ctx,
                {},
                source_ref=data.source_ref,
                is_verified_purchase=data.is_verified_purchase,
            )
        _emit(review, "review.created", ctx)
        if review.status == ReviewStatus.PUBLISHED:
            # Same shape as a published transition; null says it was published at creation.
            _emit(review, "review.published", ctx, previous_status=None)
        _move_aggregates(locked, None, contribution(review), ctx)
        return review


def update_review(review_id, values: dict, ctx: AuditContext, *, tenant_id: str) -> Review:
    """PATCH: immediate fields apply at once; content waits as a revision under pre-moderation."""

    with transaction.atomic(), locked_review(review_id, tenant_id=tenant_id) as (entity, review):
        if review.status not in transitions.EDITABLE_STATUSES:
            raise transitions.invalid_transition(review.status, transitions.EDIT)
        if not entity.is_active:
            raise entity_inactive(entity)
        before = _snapshot(review)
        old_contribution = contribution(review)
        content = {
            field: values[field]
            for field in CONTENT_FIELDS
            if field in values and values[field] != getattr(review, field)
        }
        settings = get_settings(review.tenant_id, review.application_id)
        pre_moderated = (
            settings is not None
            and settings.effective_moderation_mode == ModerationMode.PRE_MODERATION
        )
        revision_submitted = False
        if content and review.status == ReviewStatus.PUBLISHED and pre_moderated:
            proposed = {field: getattr(review, field) for field in CONTENT_FIELDS} | content
            # One pending revision per review; a newer edit replaces it with a new id.
            ReviewRevision.objects.filter(review=review).delete()
            ReviewRevision.objects.create(review=review, base_version=review.version, **proposed)
            revision_submitted = True
        elif content:
            for field, value in content.items():
                setattr(review, field, value)
            review.version += 1
        attested = (
            "is_verified_purchase" in values
            and values["is_verified_purchase"] != review.is_verified_purchase
        )
        for field in IMMEDIATE_FIELDS:
            if field in values:
                setattr(review, field, values[field])
        changes = audit.diff(before, _snapshot(review))
        if changes:
            review.save()
            _audit(review, "review.updated", ctx, changes)
            _emit(review, "review.updated", ctx)
        if attested:
            _audit(
                review,
                "review.attested",
                ctx,
                {},
                is_verified_purchase=review.is_verified_purchase,
            )
        if revision_submitted:
            _audit(review, "review.revision_submitted", ctx, {"fields": sorted(content)})
            _emit(review, "review.revision_submitted", ctx)
        _move_aggregates(entity, old_contribution, contribution(review), ctx)
        return review


def check_expected_versions(review: Review, expected_version, expected_media_version) -> None:
    if expected_version != review.version or expected_media_version != review.media_version:
        raise DomainError(
            "The review changed since you loaded it; reload it and review the new content.",
            {
                "expected_version": expected_version,
                "version": review.version,
                "expected_media_version": expected_media_version,
                "media_version": review.media_version,
            },
            code="review_changed",
        )


def transition_review(
    review_id,
    action: str,
    ctx: AuditContext,
    *,
    tenant_id: str,
    reason: str | None = None,
    expected_version: int | None = None,
    expected_media_version: int | None = None,
) -> Review:
    with transaction.atomic(), locked_review(review_id, tenant_id=tenant_id) as (entity, review):
        target = transitions.next_status(review.status, action)
        if action == transitions.PUBLISH:
            # Nothing the moderator has not seen may go public (docs/spec.md section 4.4).
            check_expected_versions(review, expected_version, expected_media_version)
        before = _snapshot(review)
        old_contribution = contribution(review)
        previous_status = review.status
        review.status = target
        review.status_reason = reason if action in (transitions.REJECT, transitions.HIDE) else None
        if target == ReviewStatus.PUBLISHED and review.published_at is None:
            review.published_at = timezone.now()
        if target == ReviewStatus.REMOVED:
            ReviewRevision.objects.filter(review=review).delete()
        review.save()
        _audit(review, f"review.{action}", ctx, audit.diff(before, _snapshot(review)))
        _emit(review, "review.status_changed", ctx, previous_status=previous_status)
        if target == ReviewStatus.PUBLISHED:
            _emit(review, "review.published", ctx, previous_status=previous_status)
        if target == ReviewStatus.REMOVED:
            _emit(review, "review.removed", ctx, previous_status=previous_status)
        _move_aggregates(entity, old_contribution, contribution(review), ctx)
        return review


def _current_revision(review: Review, revision_id) -> ReviewRevision:
    revision = ReviewRevision.objects.filter(review=review).first()
    if revision is None or str(revision.id) != str(revision_id):
        raise DomainError(
            "That revision is no longer the review's pending revision; fetch "
            "GET /reviews/{id}/revision and act on the current one.",
            {"revision_id": str(revision.id) if revision else None},
            code="revision_superseded",
        )
    return revision


def approve_revision(
    review_id,
    revision_id,
    ctx: AuditContext,
    *,
    tenant_id: str,
    expected_version,
    expected_media_version,
) -> Review:
    with transaction.atomic(), locked_review(review_id, tenant_id=tenant_id) as (entity, review):
        revision = _current_revision(review, revision_id)
        check_expected_versions(review, expected_version, expected_media_version)
        if revision.base_version != review.version:
            raise DomainError(
                "The revision was written against an older version of the review; ask the "
                "author to edit again.",
                {"base_version": revision.base_version, "version": review.version},
                code="revision_stale",
            )
        before = _snapshot(review)
        old_contribution = contribution(review)
        for field in CONTENT_FIELDS:
            setattr(review, field, getattr(revision, field))
        review.version += 1
        revision.delete()
        review.save()
        _audit(review, "review.revision_approved", ctx, audit.diff(before, _snapshot(review)))
        _emit(review, "review.updated", ctx)
        _move_aggregates(entity, old_contribution, contribution(review), ctx)
        return review


def reject_revision(
    review_id, revision_id, ctx: AuditContext, *, tenant_id: str, reason=None
) -> Review:
    with transaction.atomic(), locked_review(review_id, tenant_id=tenant_id) as (_entity, review):
        revision = _current_revision(review, revision_id)
        revision.delete()
        _audit(review, "review.revision_rejected", ctx, {}, reason=reason)
        return review
