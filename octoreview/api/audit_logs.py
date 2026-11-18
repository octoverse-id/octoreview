from __future__ import annotations

from rest_framework.decorators import api_view

from octoreview.audit.selectors import audit_logs_for_request
from octoreview.audit.serializers import AuditLogSerializer
from octoreview.core.auth import require_scopes
from octoreview.core.pagination import OctoreviewLimitOffsetPagination


@require_scopes(get="audit:read")
@api_view(["GET"])
def audit_log_list(request):
    paginator = OctoreviewLimitOffsetPagination()
    page = paginator.paginate_queryset(audit_logs_for_request(request), request)
    return paginator.get_paginated_response(AuditLogSerializer(page, many=True).data)
