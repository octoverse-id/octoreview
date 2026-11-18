"""Service-key authentication and grant authorization (docs/spec.md section 2).

Ported from octonomy `core/auth.py`, without the v2 surface and the namespace rollout switches.
Every decision, primary scope or secondary permission, goes through `grant_authorizes`: tenant,
application, namespace and capability are evaluated as one predicate.
"""

from __future__ import annotations

from collections.abc import Iterable

from octoreview.core.errors import (
    ApplicationMismatchError,
    DomainError,
    PermissionDeniedError,
)
from octoreview.core.models import SCOPE_ID_PATTERN
from octoreview.core.namespaces import (
    GLOBAL_SCOPE,
    ScopeContext,
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SCOPES = frozenset(
    {
        "entities:read",
        "entities:write",
        "reviews:read",
        "reviews:write",
        "reviews:moderate",
        "reviews:attest",
        "media:write",
        "settings:read",
        "settings:write",
        "audit:read",
        "privacy:erase",
    }
)


def require_scopes(**method_scopes: str):
    """Declare the scope each HTTP method of a DRF view needs, e.g. `get="reviews:read"`."""

    normalized = {method.upper(): scope for method, scope in method_scopes.items()}
    unknown = set(normalized.values()) - SCOPES
    if unknown:
        raise ValueError(f"Unknown scopes {sorted(unknown)}; add them to SCOPES first.")

    def decorator(view_func):
        target = view_func.cls if hasattr(view_func, "cls") else view_func
        target.required_scopes = normalized
        return view_func

    return decorator


def is_read_request(request, view) -> bool:
    # "Write" means a mutating operation, not an HTTP method: POST /entities/ratings/query is a
    # read and declares itself with `read_only_post = True` (docs/spec.md section 1).
    return request.method in SAFE_METHODS or bool(getattr(view, "read_only_post", False))


def grant_matches_namespace(grant, scope_context: ScopeContext) -> bool:
    if grant.namespace_wildcard:
        return True
    if scope_context.is_global:
        return grant.namespace_type is None and grant.namespace_id is None
    return (
        grant.namespace_type == scope_context.namespace_type
        and grant.namespace_id == scope_context.namespace_id
    )


def grant_is_application_wide(grant) -> bool:
    """A global-only or wildcard grant: it reaches every namespace of its application."""

    return grant.namespace_wildcard or (grant.namespace_type is None and grant.namespace_id is None)


def grant_authorizes(
    grant,
    *,
    tenant_id: str,
    application_id: str | None,
    scope_context: ScopeContext,
    required_scope: str,
) -> bool:
    """Tenant, application, namespace and API scope as one predicate."""

    if grant.tenant_id != tenant_id or not grant.has_scope(required_scope):
        return False
    if grant.application_id is not None and grant.application_id != application_id:
        return False
    # A namespaced request must name its application even when the grant is tenant-wide:
    # namespace isolation sits below the application.
    if not scope_context.is_global and application_id is None:
        return False
    return grant_matches_namespace(grant, scope_context)


def tenant_grants(request, client, tenant_id: str) -> list:
    """The client's grants for one tenant, cached on the request under (client, tenant)."""

    key = (client.pk, tenant_id)
    cached = getattr(request, "_tenant_grants_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    grants = list(client.grants.filter(tenant_id=tenant_id))
    request._tenant_grants_cache = (key, grants)
    return grants


def canonical_application_id(request, view) -> str | None:
    """The one application the request names, from path, query or (for writes) body."""

    values: dict[str, str] = {}
    path_value = getattr(view, "kwargs", {}).get("application_id")
    if path_value:
        values["path"] = str(path_value)
    query_value = request.query_params.get("application_id")
    if query_value:
        values["query"] = query_value
    if not is_read_request(request, view) or getattr(view, "read_only_post", False):
        data = request.data if hasattr(request, "data") else {}
        if isinstance(data, dict) and data.get("application_id"):
            values["body"] = str(data["application_id"])
    distinct = set(values.values())
    if len(distinct) > 1:
        raise ApplicationMismatchError(details=values)
    if not distinct:
        return None
    application_id = distinct.pop()
    if not SCOPE_ID_PATTERN.fullmatch(application_id):
        raise DomainError(
            "application_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$; fix the value.",
            {"fields": {"application_id": ["Invalid application_id."]}},
        )
    return application_id


def has_permission(request, scope: str, *, application_id: str | None = None) -> bool:
    """Whether the authenticated caller holds `scope` for this request's tenant, application
    and namespace. Used for secondary permissions such as `reviews:attest` (docs/spec.md
    section 2): the same predicate, never a bare scope-name check."""

    client = getattr(request, "service_client", None)
    tenant_id = getattr(request, "tenant_id", None)
    if client is None or not tenant_id:
        return False
    if application_id is None:
        application_id = getattr(request, "application_id", None)
    scope_context = getattr(request, "scope_context", GLOBAL_SCOPE)
    return any(
        grant_authorizes(
            grant,
            tenant_id=tenant_id,
            application_id=application_id,
            scope_context=scope_context,
            required_scope=scope,
        )
        for grant in tenant_grants(request, client, tenant_id)
    )


def require_permission(request, scope: str, *, application_id: str | None = None) -> None:
    if not has_permission(request, scope, application_id=application_id):
        raise PermissionDeniedError(
            f"This action also needs the {scope} scope for this application and namespace; "
            "use a key whose grant includes it.",
            {
                "reason": "scope_missing",
                "required_scope": scope,
                "grant_scope": granted_scopes(request),
            },
        )


def granted_scopes(request) -> list[str]:
    client = getattr(request, "service_client", None)
    tenant_id = getattr(request, "tenant_id", None)
    if client is None or not tenant_id:
        return []
    scopes: set[str] = set()
    for grant in tenant_grants(request, client, tenant_id):
        scopes.update(grant.scopes)
    return sorted(scopes)


def authorized_application_ids(request) -> set[str] | None:
    """Applications the caller may reach for its required scope in this namespace.

    `None` means unrestricted (a tenant-wide grant). Object-by-id lookups bound the row to this
    set, so a grant can never reach a row in an application it does not cover.
    """

    client = getattr(request, "service_client", None)
    tenant_id = getattr(request, "tenant_id", None)
    if client is None or not tenant_id:
        return set()
    scope_context = getattr(request, "scope_context", GLOBAL_SCOPE)
    required_scope = getattr(request, "required_scope", None)
    application_ids: set[str] = set()
    for grant in tenant_grants(request, client, tenant_id):
        if required_scope and not grant.has_scope(required_scope):
            continue
        if not grant_matches_namespace(grant, scope_context):
            continue
        if grant.application_id is None:
            return None
        application_ids.add(grant.application_id)
    return application_ids


def authorized_scope_contexts(
    grants: Iterable,
    *,
    tenant_id: str,
    application_id: str | None,
    requested: Iterable[ScopeContext],
    required_scope: str,
) -> frozenset[ScopeContext]:
    grant_list = tuple(grants)
    return frozenset(
        scope_context
        for scope_context in requested
        if any(
            grant_authorizes(
                grant,
                tenant_id=tenant_id,
                application_id=application_id,
                scope_context=scope_context,
                required_scope=required_scope,
            )
            for grant in grant_list
        )
    )


def request_include_global(request) -> bool:
    """Whether a namespaced read may also see global rows.

    Only when the caller asked (`include_global=true`) *and* its grants cover the global
    partition; otherwise global rows are silently excluded (docs/spec.md section 1).
    """

    scope_context = getattr(request, "scope_context", GLOBAL_SCOPE)
    if scope_context.is_global:
        return True
    authorized = getattr(request, "authorized_scope_contexts", frozenset())
    return bool(getattr(request, "include_global", False)) and GLOBAL_SCOPE in authorized
