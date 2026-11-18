"""Which reviews a request may read (docs/spec.md section 5.2)."""

from __future__ import annotations

from django.db.models import Q

from octoreview.core.auth import authorized_application_ids, has_permission, request_include_global
from octoreview.core.errors import DomainError, NotFoundError, PermissionDeniedError
from octoreview.core.namespaces import GLOBAL_SCOPE
from octoreview.reviews.models import Review, ReviewStatus

SORTS = {
    "newest": ("-created_at", "id"),
    "rating": ("-rating", "-created_at", "id"),
}
TRUE, FALSE = {"true", "1"}, {"false", "0"}


def scoped_reviews(request, *, for_write: bool = False):
    scope_context = getattr(request, "scope_context", GLOBAL_SCOPE)
    queryset = Review.objects.filter(tenant_id=request.tenant_id)
    global_rows = Q(namespace_type__isnull=True, namespace_id__isnull=True)
    if scope_context.is_global:
        queryset = queryset.filter(global_rows)
    else:
        partition = Q(
            namespace_type=scope_context.namespace_type, namespace_id=scope_context.namespace_id
        )
        if not for_write and request_include_global(request):
            partition |= global_rows
        queryset = queryset.filter(partition)
    applications = authorized_application_ids(request)
    if applications is not None:
        queryset = queryset.filter(application_id__in=applications)
    if getattr(request, "application_id", None):
        queryset = queryset.filter(application_id=request.application_id)
    return queryset.select_related("entity")


def get_review(request, review_id, *, for_write: bool = False) -> Review:
    review = scoped_reviews(request, for_write=for_write).filter(pk=review_id).first()
    if review is None:
        raise NotFoundError()
    return review


def _bool_param(request, name: str) -> bool | None:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    value = raw.lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise DomainError(details={"fields": {name: ["Must be true or false."]}})


def is_author_path(request) -> bool:
    return (
        bool(request.query_params.get("author_id"))
        and _bool_param(request, "include_unpublished") is True
    )


def list_reviews(request):
    """The list rules: published by default; other statuses need reviews:moderate, except the
    author path (`author_id` + `include_unpublished=true`)."""

    queryset = scoped_reviews(request)
    params = request.query_params
    moderator = has_permission(request, "reviews:moderate")
    author_path = is_author_path(request)
    status = params.get("status")
    # The media moderation queue filter joins this list with the media PR.
    gated = [name for name in ("has_pending_revision",) if name in params]
    if (status and status != ReviewStatus.PUBLISHED and not author_path) or gated:
        if not moderator:
            raise PermissionDeniedError(
                "Listing unpublished reviews or the moderation queue needs reviews:moderate; "
                "authors see their own reviews with author_id and include_unpublished=true.",
                {"reason": "scope_missing", "required_scope": "reviews:moderate"},
            )
    if status:
        if status not in ReviewStatus.values:
            raise DomainError(details={"fields": {"status": ["Unknown status."]}})
        queryset = queryset.filter(status=status)
    elif author_path:
        queryset = queryset.exclude(status=ReviewStatus.REMOVED)
    elif not (moderator and gated):
        queryset = queryset.filter(status=ReviewStatus.PUBLISHED)

    if params.get("entity_id"):
        queryset = queryset.filter(entity_id=params["entity_id"])
    if params.get("entity_type") and params.get("external_id"):
        queryset = queryset.filter(
            entity__entity_type=params["entity_type"], entity__external_id=params["external_id"]
        )
    if params.get("author_id"):
        queryset = queryset.filter(author_id=params["author_id"])
    if params.get("rating"):
        try:
            rating = int(params["rating"])
        except ValueError:
            rating = 0
        if not 1 <= rating <= 5:
            raise DomainError(details={"fields": {"rating": ["Must be 1-5."]}})
        queryset = queryset.filter(rating=rating)
    has_media = _bool_param(request, "has_media")
    if has_media is not None:
        queryset = (
            queryset.filter(ready_media_count__gt=0)
            if has_media
            else queryset.filter(ready_media_count=0)
        )
    verified = _bool_param(request, "is_verified_purchase")
    if verified is not None:
        queryset = queryset.filter(is_verified_purchase=verified)
    pending_revision = _bool_param(request, "has_pending_revision")
    if pending_revision is not None:
        queryset = queryset.filter(pending_revision__isnull=not pending_revision)

    sort = params.get("sort", "newest")
    if sort not in SORTS:
        raise DomainError(details={"fields": {"sort": [f"One of {sorted(SORTS)}."]}})
    return queryset.order_by(*SORTS[sort])
