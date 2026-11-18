from __future__ import annotations

import warnings

from django.core.paginator import UnorderedObjectListWarning
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response

from octoreview.core.errors import DomainError

MAX_OFFSET = 10_000


def warn_if_unordered(queryset) -> None:
    """Re-arm Django's unordered-pagination warning on DRF's slicing path.

    DRF's LimitOffsetPagination slices the queryset without building a Paginator, so Django's
    own detector never fires. LIMIT/OFFSET with no ORDER BY repeats and skips rows; the
    pyproject filterwarnings entry turns this into a test failure (octonomy issue #162).
    """

    ordered = getattr(queryset, "ordered", None)
    if ordered is None or ordered:
        return
    warnings.warn(
        f"Pagination may yield inconsistent results with an unordered object_list: {queryset!r}.",
        UnorderedObjectListWarning,
        stacklevel=3,
    )


class OctoreviewLimitOffsetPagination(LimitOffsetPagination):
    """limit/offset with the `{data, pagination}` envelope (docs/spec.md section 5)."""

    default_limit = 20
    max_limit = 100

    def paginate_queryset(self, queryset, request, view=None):
        warn_if_unordered(queryset)
        offset = self.get_offset(request)
        if offset > MAX_OFFSET:
            raise DomainError(
                f"offset is capped at {MAX_OFFSET}; narrow the filters instead of paging deeper.",
                {"fields": {"offset": [f"Ensure this value is at most {MAX_OFFSET}."]}},
            )
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        return Response(
            {
                "data": data,
                "pagination": {"limit": self.limit, "offset": self.offset, "count": self.count},
            }
        )
