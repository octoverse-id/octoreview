from __future__ import annotations

from django.db import connection
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def live(request):
    return Response({"data": {"status": "ok"}})


@api_view(["GET"])
@permission_classes([AllowAny])
def ready(request):
    # Readiness checks the database only, never storage: a storage blip must not take every
    # pod out of rotation (docs/spec.md section 5).
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return Response({"data": {"status": "unavailable", "database": "down"}}, status=503)
    return Response({"data": {"status": "ok", "database": "ok"}})
