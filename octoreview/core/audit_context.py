from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuditContext:
    """Who did something and under which request; every audit row and outbox event carries it."""

    actor_id: str | None = None
    request_id: str | None = None
    operation_id: uuid.UUID = field(default_factory=uuid.uuid4)

    @classmethod
    def from_request(cls, request) -> AuditContext:
        actor_id = request.headers.get("X-Actor-ID") or None
        if actor_id is not None:
            actor_id = actor_id[:255]
        return cls(actor_id=actor_id, request_id=getattr(request, "request_id", None))
