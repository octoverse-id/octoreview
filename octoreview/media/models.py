from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from octoreview.core.models import NamespaceScopedModel, namespace_scope_constraint
from octoreview.reviews.models import Review


class MediaStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    READY = "ready", "Ready"
    INVALID = "invalid", "Invalid"
    DELETING = "deleting", "Deleting"
    DELETED = "deleted", "Deleted"


class ModerationStatus(models.TextChoices):
    NONE = "none", "None"
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


VISIBLE_MODERATION = (ModerationStatus.NONE, ModerationStatus.APPROVED)


class StorageLocation(models.Model):
    """Every storage location this deployment has written to (docs/spec.md section 3.6)."""

    id = models.CharField(max_length=100, primary_key=True)
    backend = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "storage_locations"


class ReviewMedia(NamespaceScopedModel):
    """One image or video of a review (docs/spec.md sections 3.5 and 4.3).

    `status`, `moderation_status`, the lease and the cleanup claim change only through
    `octoreview.lifecycle.media`, which also keeps the review's `media_version` and
    `ready_media_count` in step.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=100)
    application_id = models.CharField(max_length=100)
    review = models.ForeignKey(Review, related_name="media", on_delete=models.PROTECT)
    kind = models.CharField(max_length=10)
    status = models.CharField(max_length=20, choices=MediaStatus.choices)
    moderation_status = models.CharField(
        max_length=20, choices=ModerationStatus.choices, default=ModerationStatus.NONE
    )
    moderation_reason = models.CharField(max_length=500, null=True, blank=True)
    invalid_reason = models.CharField(max_length=40, null=True, blank=True)
    storage_location = models.ForeignKey(StorageLocation, on_delete=models.PROTECT)
    staging_key = models.CharField(max_length=500)
    storage_key = models.CharField(max_length=500, null=True, blank=True)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)
    client_upload_id = models.CharField(max_length=100, null=True, blank=True)
    upload_expires_at = models.DateTimeField()
    completion_attempt_id = models.UUIDField(null=True, blank=True)
    completion_lease_until = models.DateTimeField(null=True, blank=True)
    cleanup_claimed_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "review_media"
        constraints = [
            namespace_scope_constraint(),
            models.CheckConstraint(condition=Q(size_bytes__gte=1), name="media_size_positive"),
            models.UniqueConstraint(
                fields=["review", "client_upload_id"],
                condition=Q(client_upload_id__isnull=False) & ~Q(status="deleted"),
                name="uniq_media_client_upload",
            ),
        ]
        indexes = [
            models.Index(fields=["review", "kind", "status"], name="media_review_kind_idx"),
            models.Index(fields=["status", "upload_expires_at"], name="media_expiry_idx"),
            models.Index(
                fields=["tenant_id", "application_id", "status", "upload_expires_at"],
                name="media_reservation_idx",
            ),
            models.Index(
                fields=["tenant_id", "application_id", "status"],
                name="media_stored_bytes_idx",
                include=["size_bytes"],
            ),
        ]
