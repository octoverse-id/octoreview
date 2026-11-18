from rest_framework import serializers

from octoreview.audit.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "tenant_id",
            "application_id",
            "namespace_type",
            "namespace_id",
            "action",
            "aggregate_type",
            "aggregate_id",
            "actor_id",
            "request_id",
            "operation_id",
            "changes",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields
