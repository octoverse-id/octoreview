from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from octoreview.core.models import NamespaceScopedModel, namespace_scope_constraint

POLICY_FIELDS = ("max_images", "max_videos", "max_image_bytes", "max_video_bytes")


class ModerationMode(models.TextChoices):
    AUTO_PUBLISH = "auto_publish", "Auto publish"
    PRE_MODERATION = "pre_moderation", "Pre-moderation"


class ApplicationSettings(models.Model):
    """Per-application defaults (docs/spec.md section 3.1).

    Every policy field is nullable and null means "inherit the server default", so a missing row
    and an all-null row behave the same. The row is created lazily.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=100)
    application_id = models.CharField(max_length=100)
    max_images = models.PositiveIntegerField(null=True, blank=True)
    max_videos = models.PositiveIntegerField(null=True, blank=True)
    max_image_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    max_video_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    allowed_content_types = models.JSONField(null=True, blank=True)
    moderation_mode = models.CharField(
        max_length=20, choices=ModerationMode.choices, null=True, blank=True
    )
    max_storage_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "application_settings"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_id", "application_id"], name="uniq_application_settings"
            )
        ]

    @property
    def effective_moderation_mode(self) -> str:
        return self.moderation_mode or ModerationMode.AUTO_PUBLISH


class Entity(NamespaceScopedModel):
    """An external resource that can be reviewed (docs/spec.md section 3.2).

    Aggregate columns count published reviews only and are written exclusively by
    `octoreview.entities.ratings.apply_delta` (a grep gate in `make check` enforces it).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=100)
    application_id = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=100)
    external_id = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    max_images = models.PositiveIntegerField(null=True, blank=True)
    max_videos = models.PositiveIntegerField(null=True, blank=True)
    max_image_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    max_video_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    rating_count = models.PositiveIntegerField(default=0)
    rating_sum = models.PositiveBigIntegerField(default=0)
    rating_1_count = models.PositiveIntegerField(default=0)
    rating_2_count = models.PositiveIntegerField(default=0)
    rating_3_count = models.PositiveIntegerField(default=0)
    rating_4_count = models.PositiveIntegerField(default=0)
    rating_5_count = models.PositiveIntegerField(default=0)
    verified_review_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "entities"
        constraints = [
            namespace_scope_constraint(),
            # Split partial unique constraints (octonomy's pattern): NULL namespaces would never
            # collide in one composite constraint, and SQLite has no NULLS NOT DISTINCT.
            models.UniqueConstraint(
                fields=["tenant_id", "application_id", "entity_type", "external_id"],
                condition=Q(namespace_type__isnull=True, namespace_id__isnull=True),
                name="uniq_entity_global",
            ),
            models.UniqueConstraint(
                fields=[
                    "tenant_id",
                    "application_id",
                    "namespace_type",
                    "namespace_id",
                    "entity_type",
                    "external_id",
                ],
                condition=Q(namespace_type__isnull=False, namespace_id__isnull=False),
                name="uniq_entity_namespaced",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant_id", "application_id", "entity_type", "external_id"],
                name="entity_lookup_idx",
            ),
            models.Index(
                fields=["tenant_id", "application_id", "namespace_type", "namespace_id", "id"],
                name="entity_scope_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entity_type}/{self.external_id}"
