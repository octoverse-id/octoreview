from __future__ import annotations

from typing import Any

from octoreview.audit.models import AuditLog
from octoreview.core.audit_context import AuditContext


def build_audit_log(
    *,
    tenant_id: str,
    action: str,
    aggregate_type: str,
    aggregate_id: Any,
    audit_context: AuditContext,
    application_id: str | None = None,
    namespace_type: str | None = None,
    namespace_id: str | None = None,
    changes: dict | None = None,
    metadata: dict | None = None,
) -> AuditLog:
    # An audit row is not optional: callers always pass a context, so a missing one is a bug
    # that fails loudly instead of silently skipping the record (octonomy logged and skipped).
    return AuditLog(
        tenant_id=tenant_id,
        application_id=application_id,
        namespace_type=namespace_type,
        namespace_id=namespace_id,
        action=action,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        actor_id=audit_context.actor_id,
        request_id=audit_context.request_id,
        operation_id=audit_context.operation_id,
        changes=changes or {},
        metadata=metadata or {},
    )


def record(**kwargs) -> AuditLog:
    entry = build_audit_log(**kwargs)
    entry.save()
    return entry


def diff(before: dict, after: dict) -> dict:
    """`{field: [before, after]}` for every field whose value changed."""

    return {
        key: [before.get(key), after.get(key)]
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
