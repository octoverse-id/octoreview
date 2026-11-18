from __future__ import annotations

import logging
import re
import time
import uuid

from django.utils.cache import patch_vary_headers

logger = logging.getLogger("octoreview.requests")

# Inbound request ids are kept only when they fit the 100-character audit and outbox columns
# (docs/spec.md section 1); anything else would 500 on PostgreSQL, so we generate our own.
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")

# Cacheable reads vary by caller identity and namespace partition, so a shared cache never serves
# one partition's rows to another.
VARY_HEADERS = ("Authorization", "X-Tenant-ID", "X-Namespace-Type", "X-Namespace-ID")
CACHEABLE_METHODS = frozenset({"GET", "HEAD"})


def request_id_for(request) -> str:
    inbound = request.headers.get("X-Request-ID")
    if inbound and REQUEST_ID_PATTERN.fullmatch(inbound):
        return inbound
    return f"req_{uuid.uuid4().hex}"


def route_template(request) -> str:
    # Log the resolved route, never request.path: local media URLs carry signed tokens in the
    # path (docs/spec.md section 16).
    match = getattr(request, "resolver_match", None)
    if match is None or not getattr(match, "route", None):
        return "<unresolved>"
    return match.route


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = request_id_for(request)
        request.tenant_id = request.headers.get("X-Tenant-ID")

        started_at = time.monotonic()
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        if request.method in CACHEABLE_METHODS:
            patch_vary_headers(response, VARY_HEADERS)

        scope_context = getattr(request, "scope_context", None)
        logger.info(
            "request_completed",
            extra={
                "request_id": request.request_id,
                "tenant_id": request.tenant_id,
                "namespace_type": getattr(scope_context, "namespace_type", None),
                "method": request.method,
                "route": route_template(request),
                "status_code": response.status_code,
                "error_code": getattr(request, "error_code", None),
                "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
            },
        )
        return response
