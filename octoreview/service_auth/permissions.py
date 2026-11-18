"""The DRF permission class that authenticates service keys (docs/spec.md section 2).

It lives here, above `octoreview.core`, because it needs the ServiceClient lookup; the grant
predicates it applies stay in `octoreview.core.auth`, so every domain module can reuse them.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.permissions import BasePermission

from octoreview.core.auth import (
    authorized_scope_contexts,
    canonical_application_id,
    grant_authorizes,
    grant_is_application_wide,
    granted_scopes,
    is_read_request,
    tenant_grants,
)
from octoreview.core.errors import DomainError, PermissionDeniedError
from octoreview.core.models import SCOPE_ID_PATTERN
from octoreview.core.namespaces import GLOBAL_SCOPE, include_global_from_query, scope_from_headers
from octoreview.service_auth.services import authenticate_service_token

LAST_USED_UPDATE_INTERVAL = timedelta(seconds=60)


class BearerTokenPermission(BasePermission):
    """Authenticates the service key and authorizes the view's declared scope."""

    def has_permission(self, request, view) -> bool:
        if getattr(view, "allow_unauthenticated", False):
            return True
        if request.method not in getattr(view, "allowed_methods", ()):
            # Let DRF answer 405 for a method the view does not serve, instead of a 403 about a
            # scope the method can never have.
            return True

        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise exceptions.NotAuthenticated()
        client = authenticate_service_token(token.strip())
        if client is None:
            raise exceptions.AuthenticationFailed()
        request.service_client = client

        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id:
            raise DomainError(
                "X-Tenant-ID is required; send the tenant id header.",
                {"fields": {"X-Tenant-ID": ["This header is required."]}},
            )
        if not SCOPE_ID_PATTERN.fullmatch(tenant_id):
            raise DomainError(
                "X-Tenant-ID must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$; fix the header.",
                {"fields": {"X-Tenant-ID": ["Invalid tenant id."]}},
            )

        scope = (getattr(view, "required_scopes", {}) or {}).get(request.method)
        if not scope:
            raise PermissionDeniedError(
                "This endpoint declares no scope for this method; report it as a bug.",
                {"reason": "endpoint_has_no_scope"},
            )
        request.required_scope = scope

        scope_context = scope_from_headers(request)
        request.scope_context = scope_context
        is_read = is_read_request(request, view)
        request.include_global = include_global_from_query(request, is_read=is_read)
        application_id = canonical_application_id(request, view)
        request.application_id = application_id

        grants = tenant_grants(request, client, tenant_id)
        if not grants:
            raise PermissionDeniedError(
                "This service key has no grant for this tenant; use a key granted for it.",
                {"reason": "tenant_not_granted", "required_scope": scope, "grant_scope": []},
            )
        scoped = [grant for grant in grants if grant.has_scope(scope)]
        if not scoped:
            raise PermissionDeniedError(
                f"This service key lacks the {scope} scope; use a key whose grant includes it.",
                {
                    "reason": "scope_missing",
                    "required_scope": scope,
                    "grant_scope": granted_scopes(request),
                },
            )

        requested = (scope_context,)
        if request.include_global and not scope_context.is_global:
            requested = (scope_context, GLOBAL_SCOPE)
        request.authorized_scope_contexts = authorized_scope_contexts(
            scoped,
            tenant_id=tenant_id,
            application_id=application_id,
            requested=requested,
            required_scope=scope,
        )

        if scope_context not in request.authorized_scope_contexts:
            reason = "namespace_not_granted"
            if not scope_context.is_global and application_id is None:
                reason = "application_required_for_namespace"
            elif not any(grant.application_id in (None, application_id) for grant in scoped):
                reason = "application_not_granted"
            raise PermissionDeniedError(
                "This service key is not granted this application or namespace; use a key whose "
                "grant covers it.",
                {"reason": reason, "required_scope": scope, "grant_scope": granted_scopes(request)},
            )

        if getattr(view, "requires_application_wide_grant", False) and not any(
            grant_is_application_wide(grant)
            and grant_authorizes(
                grant,
                tenant_id=tenant_id,
                application_id=application_id,
                scope_context=GLOBAL_SCOPE,
                required_scope=scope,
            )
            for grant in scoped
        ):
            raise PermissionDeniedError(
                "This action needs a global-only or namespace-wildcard grant for the application; "
                "an exact-namespace grant cannot perform it.",
                {
                    "reason": "application_wide_grant_required",
                    "required_scope": scope,
                    "grant_scope": granted_scopes(request),
                },
            )

        self.mark_client_used(client)
        return True

    @staticmethod
    def mark_client_used(client) -> None:
        now = timezone.now()
        # Throttled so busy service traffic does not turn every request into a write.
        if client.last_used_at and now - client.last_used_at <= LAST_USED_UPDATE_INTERVAL:
            return
        client.last_used_at = now
        client.save(update_fields=["last_used_at"])
