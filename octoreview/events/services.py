from __future__ import annotations

from typing import Any

from octoreview.core.audit_context import AuditContext
from octoreview.events.models import OutboxEvent

# Fields no event payload may carry (docs/spec.md section 10). `author.erased` is the one
# documented exception for `author_id`.
FORBIDDEN_PAYLOAD_FIELDS = frozenset(
    {"title", "body", "author_metadata", "author_id", "source_ref", "metadata"}
)
AUTHOR_ERASED = "author.erased"


def build_outbox_event(
    *,
    tenant_id: str,
    event_type: str,
    resource_type: str,
    resource_id: Any,
    payload: dict,
    audit_context: AuditContext,
    application_id: str | None = None,
    namespace_type: str | None = None,
    namespace_id: str | None = None,
    metadata: dict | None = None,
) -> OutboxEvent:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object.")
    forbidden = FORBIDDEN_PAYLOAD_FIELDS & set(payload)
    if event_type == AUTHOR_ERASED:
        forbidden -= {"author_id"}
    if forbidden:
        # Thin events keep personal data out of logs and consumer copies; erasure relies on it.
        raise ValueError(f"Event payloads cannot carry {sorted(forbidden)} (docs/spec.md 10).")
    return OutboxEvent(
        tenant_id=tenant_id,
        application_id=application_id,
        namespace_type=namespace_type,
        namespace_id=namespace_id,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=str(resource_id),
        payload=payload,
        metadata=metadata or {},
        operation_id=audit_context.operation_id,
        request_id=audit_context.request_id,
    )


def enqueue(**kwargs) -> OutboxEvent:
    event = build_outbox_event(**kwargs)
    event.save()
    return event
