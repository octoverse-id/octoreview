"""Author erasure (docs/spec.md section 9): UU PDP (UU 27/2022) and GDPR requests.

Each review is redacted in its own short transaction, so a large erasure never holds locks for
long and a crash leaves completed reviews completed. Re-runs match both the raw author id and its
erased form, so an interrupted erasure resumes where it stopped. Media deletion joins this module
with the media PR.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, replace

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from octoreview.audit import services as audit
from octoreview.audit.models import AuditLog
from octoreview.core import metrics
from octoreview.core.audit_context import AuditContext
from octoreview.entities.ratings import apply_delta
from octoreview.events import services as events
from octoreview.lifecycle.locks import locked_review
from octoreview.lifecycle.services import contribution, emit_rating_changed
from octoreview.media.models import MediaStatus, ReviewMedia
from octoreview.reviews.models import Review, ReviewRevision, ReviewStatus

BATCH_SIZE = 100
ERASED = {"erased": True}
# Personal fields that may appear in an audit row's changes or metadata.
PERSONAL_FIELDS = ("title", "body", "author_metadata", "metadata", "source_ref", "author_id")


def erased_author_id(tenant_id: str, application_id: str, author_id: str) -> str:
    """Pseudonymous, stable replacement for an author id (the docs call it pseudonymisation)."""

    message = f"{tenant_id}\x1f{application_id}\x1f{author_id}".encode()
    digest = hmac.new(settings.ERASURE_HMAC_KEY.encode(), message, hashlib.sha256).hexdigest()
    return f"erased:{digest[:32]}"


@dataclass(frozen=True)
class ErasureResult:
    reviews_redacted: int
    pending_media_deletes: int
    remaining: int

    def as_dict(self) -> dict:
        return {
            "reviews_redacted": self.reviews_redacted,
            "pending_media_deletes": self.pending_media_deletes,
            "remaining": self.remaining,
        }


def pending_reviews(tenant_id: str, application_id: str, author_id: str):
    erased = erased_author_id(tenant_id, application_id, author_id)
    unfinished = (
        Q(author_id=author_id)
        | ~Q(status=ReviewStatus.REMOVED)
        | Q(title__isnull=False)
        | Q(body__isnull=False)
        | Q(source_ref__isnull=False)
    )
    return (
        Review.objects.filter(tenant_id=tenant_id, application_id=application_id)
        .filter(Q(author_id=author_id) | Q(author_id=erased))
        .filter(unfinished)
        .order_by("id")
    )


def _redact_value(value):
    if isinstance(value, dict):
        return {
            key: (ERASED if key in PERSONAL_FIELDS else _redact_value(item))
            for key, item in value.items()
        }
    return value


def _redact_review_audit(review: Review) -> None:
    rows = AuditLog.objects.filter(
        tenant_id=review.tenant_id,
        application_id=review.application_id,
        aggregate_type="review",
        aggregate_id=str(review.id),
    )
    for row in rows:
        row.changes = _redact_value(row.changes)
        row.metadata = _redact_value(row.metadata)
        row.save(update_fields=["changes", "metadata"])


def _redact_audit_actor(tenant_id: str, application_id: str, author_id: str, erased: str) -> None:
    # Erasure is per application (docs/spec.md section 9): the same author id in another
    # application of the tenant is another person's record, and this key's grant does not reach it.
    AuditLog.objects.filter(
        tenant_id=tenant_id, application_id=application_id, actor_id=author_id
    ).update(actor_id=erased)


def _erase_one(review_id, erased: str, ctx: AuditContext, *, tenant_id: str) -> int:
    with transaction.atomic(), locked_review(review_id, tenant_id=tenant_id) as (entity, review):
        old_contribution = contribution(review)
        review.title = None
        review.body = None
        review.author_metadata = dict(ERASED)
        review.metadata = dict(ERASED)
        review.source_ref = None
        review.author_id = erased
        review.status = ReviewStatus.REMOVED
        review.save()
        ReviewRevision.objects.filter(review=review).delete()
        # Objects are deleted outside the lock by cleanup_media; the rows show `deleting` and
        # `url: null` at once (docs/spec.md section 9).
        media_rows = ReviewMedia.objects.select_for_update().filter(
            review=review, status__in=[MediaStatus.PENDING, MediaStatus.READY]
        )
        pending_deletes = media_rows.update(status=MediaStatus.DELETING)
        if pending_deletes:
            from octoreview.lifecycle.media import touch_review

            touch_review(review)
        _redact_review_audit(review)
        if apply_delta(entity, remove=old_contribution):
            emit_rating_changed(entity, ctx)
        return pending_deletes


def erase_author(
    *, tenant_id: str, application_id: str, author_id: str, audit_context: AuditContext
) -> ErasureResult:
    """Redact up to BATCH_SIZE of the author's reviews; `remaining` says whether to call again."""

    erased = erased_author_id(tenant_id, application_id, author_id)
    if audit_context.actor_id == author_id:
        # A self-erasure: every row this call writes carries the pseudonym, never the raw id.
        audit_context = replace(audit_context, actor_id=erased)
    batch = list(
        pending_reviews(tenant_id, application_id, author_id).values_list("id", flat=True)[
            :BATCH_SIZE
        ]
    )
    pending_media_deletes = 0
    for review_id in batch:
        pending_media_deletes += _erase_one(review_id, erased, audit_context, tenant_id=tenant_id)
    # Every call, so an author who acted in this application without reviews is covered too.
    with transaction.atomic():
        _redact_audit_actor(tenant_id, application_id, author_id, erased)
    remaining = pending_reviews(tenant_id, application_id, author_id).count()
    result = ErasureResult(
        reviews_redacted=len(batch),
        pending_media_deletes=pending_media_deletes,
        remaining=remaining,
    )
    if remaining == 0:
        with transaction.atomic():
            # Counts only: the author id is exactly what erasure removes.
            metrics.emit_on_commit(
                metrics.ERASE_COMPLETED,
                tenant_id=tenant_id,
                application_id=application_id,
                reviews_redacted=len(batch),
                pending_media_deletes=pending_media_deletes,
            )
            audit.record(
                tenant_id=tenant_id,
                application_id=application_id,
                action="author.erased",
                aggregate_type="author",
                aggregate_id=erased,
                audit_context=audit_context,
                metadata={"reviews_redacted": len(batch)},
            )
            # The one event that carries a raw author id, so consumers can purge their copies;
            # its outbox row is deleted as soon as it is delivered.
            events.enqueue(
                tenant_id=tenant_id,
                application_id=application_id,
                event_type=events.AUTHOR_ERASED,
                resource_type="author",
                resource_id=erased,
                payload={
                    "author_id": author_id,
                    "erased_at": timezone.now().isoformat(),
                    "reviews_redacted": len(batch),
                },
                audit_context=audit_context,
            )
    return result
