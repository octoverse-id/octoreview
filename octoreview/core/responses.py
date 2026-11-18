from __future__ import annotations

from rest_framework.response import Response


def data_response(data, status=None, headers=None) -> Response:
    """The single-resource envelope `{"data": ...}` (docs/spec.md section 5)."""

    return Response({"data": data}, status=status, headers=headers)
