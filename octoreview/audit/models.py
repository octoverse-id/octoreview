from __future__ import annotations

import uuid

from django.db import models

from octoreview.core.models import NamespaceScopedModel, namespace_scope_constraint


class AuditLog(NamespaceScopedModel):
    """Append-only record of every mutation (docs/spec.md section 3.8).

    `aggregate_type`/`aggregate_id` are octonomy's `entity_type`/`entity_id`, renamed so they
    cannot be confused with the domain Entity. Rows change only through erasure redaction.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=100)
    application_id = models.CharField(max_length=100, null=True, blank=True)
    action = models.CharField(max_length=100)
    aggregate_type = models.CharField(max_length=100)
    aggregate_id = models.CharField(max_length=255)
    actor_id = models.CharField(max_length=255, null=True, blank=True)
    request_id = models.CharField(max_length=100, null=True, blank=True)
    operation_id = models.UUIDField(default=uuid.uuid4)
    changes = models.JSONField(default=dict)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_logs"
        ordering = ["-created_at", "id"]
        constraints = [namespace_scope_constraint()]
        indexes = [
            models.Index(fields=["tenant_id", "-created_at"], name="audit_tenant_created_idx"),
            models.Index(
                fields=["tenant_id", "application_id", "-created_at"],
                name="audit_app_created_idx",
            ),
            models.Index(
                fields=[
                    "tenant_id",
                    "application_id",
                    "namespace_type",
                    "namespace_id",
                    "-created_at",
                ],
                name="audit_scope_created_idx",
            ),
            models.Index(
                fields=["tenant_id", "aggregate_type", "aggregate_id", "-created_at"],
                name="audit_aggregate_idx",
            ),
            models.Index(fields=["tenant_id", "actor_id"], name="audit_actor_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}/{self.action}/{self.aggregate_type}/{self.aggregate_id}"
