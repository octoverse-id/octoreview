"""The request's namespace partition (docs/spec.md section 1)."""

from __future__ import annotations

from dataclasses import dataclass

from octoreview.core.errors import DomainError, InvalidNamespaceError
from octoreview.core.models import NAMESPACE_FIELD_MAX_LENGTH, RESERVED_NAMESPACE_TYPE_GLOBAL


@dataclass(frozen=True)
class ScopeContext:
    """One namespace partition: both fields null (global) or both set."""

    namespace_type: str | None = None
    namespace_id: str | None = None

    def __post_init__(self) -> None:
        if (self.namespace_type is None) != (self.namespace_id is None):
            raise ValueError("namespace_type and namespace_id must both be set or both be null.")

    @property
    def is_global(self) -> bool:
        return self.namespace_type is None


GLOBAL_SCOPE = ScopeContext()


def _valid_part(value: str) -> bool:
    # Values are case-sensitive and never trimmed; blank or over-long values are refused rather
    # than silently normalised, so two callers can never land in the same partition by accident.
    return bool(value.strip()) and len(value) <= NAMESPACE_FIELD_MAX_LENGTH


def scope_from_headers(request) -> ScopeContext:
    namespace_type = request.headers.get("X-Namespace-Type")
    namespace_id = request.headers.get("X-Namespace-ID")
    if namespace_type is None and namespace_id is None:
        return GLOBAL_SCOPE
    if namespace_type is None or namespace_id is None:
        raise InvalidNamespaceError()
    if not _valid_part(namespace_type) or not _valid_part(namespace_id):
        raise InvalidNamespaceError()
    if namespace_type == RESERVED_NAMESPACE_TYPE_GLOBAL:
        raise InvalidNamespaceError()
    return ScopeContext(namespace_type=namespace_type, namespace_id=namespace_id)


def include_global_from_query(request, *, is_read: bool) -> bool:
    raw = request.query_params.get("include_global")
    if raw is None:
        return False
    if not is_read:
        raise DomainError(
            "include_global applies to reads only; drop it from this request.",
            {"fields": {"include_global": ["Not allowed on writes."]}},
        )
    value = raw.strip().lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise DomainError(
        "include_global must be true or false.",
        {"fields": {"include_global": ["Must be true or false."]}},
    )
