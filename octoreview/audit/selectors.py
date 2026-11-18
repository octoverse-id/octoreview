from __future__ import annotations

from django.db.models import Q

from octoreview.audit.models import AuditLog
from octoreview.core.namespaces import GLOBAL_SCOPE

FILTERS = ("action", "aggregate_type", "aggregate_id", "actor_id")


def audit_logs_for_request(request):
    """Audit rows the caller may read: its tenant, application and namespace partition.

    An exact-namespace grant sees only its namespace's rows (docs/spec.md section 2).
    """

    queryset = AuditLog.objects.filter(tenant_id=request.tenant_id)
    if request.application_id:
        queryset = queryset.filter(application_id=request.application_id)
    scope_context = getattr(request, "scope_context", GLOBAL_SCOPE)
    if scope_context.is_global:
        queryset = queryset.filter(namespace_type__isnull=True)
    else:
        partition = Q(
            namespace_type=scope_context.namespace_type, namespace_id=scope_context.namespace_id
        )
        if GLOBAL_SCOPE in getattr(request, "authorized_scope_contexts", frozenset()) and getattr(
            request, "include_global", False
        ):
            partition |= Q(namespace_type__isnull=True)
        queryset = queryset.filter(partition)
    for name in FILTERS:
        value = request.query_params.get(name)
        if value:
            queryset = queryset.filter(**{name: value})
    return queryset.order_by("-created_at", "id")
