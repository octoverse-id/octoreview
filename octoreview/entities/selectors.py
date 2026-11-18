"""Entity reads bounded to the caller's tenant, applications and namespace partition."""

from __future__ import annotations

from django.db.models import Q

from octoreview.core.auth import authorized_application_ids, request_include_global
from octoreview.core.errors import NotFoundError
from octoreview.core.namespaces import GLOBAL_SCOPE, ScopeContext
from octoreview.entities.models import Entity


def partition_filter(scope_context: ScopeContext, *, include_global: bool) -> Q:
    if scope_context.is_global:
        return Q(namespace_type__isnull=True, namespace_id__isnull=True)
    namespaced = Q(
        namespace_type=scope_context.namespace_type, namespace_id=scope_context.namespace_id
    )
    if include_global:
        return namespaced | Q(namespace_type__isnull=True, namespace_id__isnull=True)
    return namespaced


def visible_entities(request, *, for_write: bool = False):
    """Entities this request may see. Writes never reach across partitions: a namespaced write
    that names a global entity gets 404 (docs/spec.md section 1)."""

    scope_context = getattr(request, "scope_context", GLOBAL_SCOPE)
    include_global = (not for_write) and request_include_global(request)
    queryset = Entity.objects.filter(tenant_id=request.tenant_id).filter(
        partition_filter(scope_context, include_global=include_global)
    )
    applications = authorized_application_ids(request)
    if applications is not None:
        queryset = queryset.filter(application_id__in=applications)
    if getattr(request, "application_id", None):
        queryset = queryset.filter(application_id=request.application_id)
    return queryset


def get_visible_entity(request, entity_id, *, for_write: bool = False) -> Entity:
    entity = visible_entities(request, for_write=for_write).filter(pk=entity_id).first()
    if entity is None:
        raise NotFoundError()
    return entity


def prefer_namespaced(candidates: list[Entity]) -> Entity | None:
    """A namespaced entity wins over a global one with the same key (docs/spec.md 5.1)."""

    namespaced = [entity for entity in candidates if entity.namespace_type is not None]
    if namespaced:
        return namespaced[0]
    return candidates[0] if candidates else None


def lookup_entity(request, *, application_id: str, entity_type: str, external_id: str):
    candidates = list(
        visible_entities(request).filter(
            application_id=application_id, entity_type=entity_type, external_id=external_id
        )
    )
    return prefer_namespaced(candidates)


def batch_lookup(
    request, *, application_id: str, entity_type: str, external_ids: list[str]
) -> dict[str, Entity]:
    """One query for every requested id; duplicates are looked up once."""

    unique_ids = list(dict.fromkeys(external_ids))
    rows = visible_entities(request).filter(
        application_id=application_id, entity_type=entity_type, external_id__in=unique_ids
    )
    grouped: dict[str, list[Entity]] = {}
    for entity in rows:
        grouped.setdefault(entity.external_id, []).append(entity)
    return {
        external_id: prefer_namespaced(candidates) for external_id, candidates in grouped.items()
    }
