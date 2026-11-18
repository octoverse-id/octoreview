from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from octoreview.core.models import NamespaceScopedModel, namespace_scope_constraint
from octoreview.entities.models import Entity

# Named so the IntegrityError classifier can map them to 409 review_exists on PostgreSQL; the
# SQLite column lists below are the same constraints as SQLite reports them.
UNIQUE_AUTHOR = "uniq_review_author"
UNIQUE_AUTHOR_SOURCE = "uniq_review_author_source"
SQLITE_UNIQUE_AUTHOR = "columns:reviews.entity_id,reviews.author_id"
SQLITE_UNIQUE_AUTHOR_SOURCE = "columns:reviews.entity_id,reviews.author_id,reviews.source_ref"
REVIEW_UNIQUE_CONSTRAINTS = frozenset(
    {UNIQUE_AUTHOR, UNIQUE_AUTHOR_SOURCE, SQLITE_UNIQUE_AUTHOR, SQLITE_UNIQUE_AUTHOR_SOURCE}
)


class ReviewStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PUBLISHED = "published", "Published"
    REJECTED = "rejected", "Rejected"
    HIDDEN = "hidden", "Hidden"
    REMOVED = "removed", "Removed"


class Review(NamespaceScopedModel):
    """One author's review of one entity (docs/spec.md section 3.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=100)
    application_id = models.CharField(max_length=100)
    entity = models.ForeignKey(Entity, related_name="reviews", on_delete=models.PROTECT)
    author_id = models.CharField(max_length=255)
    author_metadata = models.JSONField(default=dict)
    rating = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=200, null=True, blank=True)
    body = models.TextField(max_length=10_000, null=True, blank=True)
    status = models.CharField(max_length=20, choices=ReviewStatus.choices)
    status_reason = models.CharField(max_length=500, null=True, blank=True)
    metadata = models.JSONField(default=dict)
    source_ref = models.CharField(max_length=255, null=True, blank=True)
    is_verified_purchase = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    media_version = models.PositiveIntegerField(default=1)
    ready_media_count = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reviews"
        constraints = [
            namespace_scope_constraint(),
            models.CheckConstraint(
                condition=Q(rating__gte=1, rating__lte=5), name="review_rating_1_5"
            ),
            # One non-removed review per author and entity, or per author, entity and order when
            # source_ref is set (docs/spec.md section 3.3).
            models.UniqueConstraint(
                fields=["entity", "author_id"],
                condition=Q(source_ref__isnull=True) & ~Q(status="removed"),
                name=UNIQUE_AUTHOR,
            ),
            models.UniqueConstraint(
                fields=["entity", "author_id", "source_ref"],
                condition=Q(source_ref__isnull=False) & ~Q(status="removed"),
                name=UNIQUE_AUTHOR_SOURCE,
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant_id", "application_id", "entity", "status", "-created_at", "id"],
                name="review_entity_newest_idx",
            ),
            models.Index(
                fields=[
                    "tenant_id",
                    "application_id",
                    "entity",
                    "status",
                    "-rating",
                    "-created_at",
                    "id",
                ],
                name="review_entity_rating_idx",
            ),
            models.Index(
                fields=["tenant_id", "application_id", "author_id"], name="review_author_idx"
            ),
            models.Index(
                fields=["tenant_id", "application_id", "status", "-created_at", "id"],
                name="review_queue_idx",
            ),
            models.Index(
                fields=[
                    "tenant_id",
                    "application_id",
                    "namespace_type",
                    "namespace_id",
                    "status",
                    "-created_at",
                    "id",
                ],
                name="review_queue_ns_idx",
            ),
        ]


class ReviewRevision(models.Model):
    """The single pending content change of a published review (docs/spec.md section 3.4)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    review = models.OneToOneField(Review, related_name="pending_revision", on_delete=models.CASCADE)
    rating = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=200, null=True, blank=True)
    body = models.TextField(max_length=10_000, null=True, blank=True)
    base_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "review_revisions"
