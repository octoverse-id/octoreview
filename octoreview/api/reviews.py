"""Review, moderation, revision and erasure endpoints (docs/spec.md sections 4, 5.2 and 9)."""

from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view

from octoreview.core import etags
from octoreview.core.audit_context import AuditContext
from octoreview.core.auth import has_permission, require_permission, require_scopes
from octoreview.core.errors import DomainError, NotFoundError
from octoreview.core.pagination import OctoreviewLimitOffsetPagination
from octoreview.core.responses import data_response
from octoreview.core.validators import (
    entity_type_error,
    external_id_error,
    json_object_error,
    plain_text_error,
)
from octoreview.entities import selectors as entity_selectors
from octoreview.entities.services import get_or_create_entity
from octoreview.lifecycle import erasure, services, transitions
from octoreview.media import selectors as media_selectors
from octoreview.reviews import selectors
from octoreview.reviews.models import Review, ReviewRevision


def _field_error(errors) -> DomainError:
    return DomainError(details={"fields": errors})


def _validated(serializer_class, data, **kwargs):
    serializer = serializer_class(data=data, **kwargs)
    if not serializer.is_valid():
        raise _field_error(serializer.errors)
    return serializer.validated_data


class PlainTextField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        error = plain_text_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value


class JsonObjectField(serializers.JSONField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        error = json_object_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value


class AuthorMetadataField(JsonObjectField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        avatar = value.get("avatar_url")
        if avatar is not None and (
            not isinstance(avatar, str) or not avatar.startswith("https://")
        ):
            raise serializers.ValidationError("avatar_url must be an https URL.")
        return value


class ReviewContentSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    title = PlainTextField(max_length=200, allow_null=True, required=False, trim_whitespace=False)
    body = PlainTextField(max_length=10_000, allow_null=True, required=False, trim_whitespace=False)
    metadata = JsonObjectField(required=False)
    author_metadata = AuthorMetadataField(required=False)
    is_verified_purchase = serializers.BooleanField(required=False)


class ReviewCreateSerializer(ReviewContentSerializer):
    application_id = serializers.CharField()
    entity_id = serializers.UUIDField(required=False)
    entity_type = serializers.CharField(required=False)
    external_id = serializers.CharField(required=False, trim_whitespace=False)
    author_id = serializers.CharField(max_length=255, trim_whitespace=False)
    source_ref = serializers.CharField(max_length=255, required=False, trim_whitespace=False)

    def validate(self, attrs):
        by_id = "entity_id" in attrs
        by_key = "entity_type" in attrs or "external_id" in attrs
        if by_id == by_key:
            raise serializers.ValidationError(
                {"entity_id": ["Send entity_id, or entity_type with external_id, not both."]}
            )
        if by_key:
            errors = {}
            for name, check in (
                ("entity_type", entity_type_error),
                ("external_id", external_id_error),
            ):
                error = check(attrs.get(name))
                if error:
                    errors[name] = [error]
            if errors:
                raise serializers.ValidationError(errors)
        return attrs


class ReviewPatchSerializer(ReviewContentSerializer):
    rating = serializers.IntegerField(min_value=1, max_value=5, required=False)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_null=True)


class GuardedSerializer(ReasonSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    expected_media_version = serializers.IntegerField(min_value=1)


class RevisionActionSerializer(serializers.Serializer):
    revision_id = serializers.UUIDField()


class GuardedRevisionSerializer(RevisionActionSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    expected_media_version = serializers.IntegerField(min_value=1)


class EraseSerializer(serializers.Serializer):
    application_id = serializers.CharField()
    author_id = serializers.CharField(max_length=255, trim_whitespace=False)


def _represent_page(request, reviews, *, author_path: bool | None = None) -> list[dict]:
    """Reviews with their embedded media; one media query for the whole page."""

    reviews = list(reviews)
    if author_path is None:
        # A list on the author path is already filtered to that author's reviews.
        author_path = selectors.is_author_path(request)
    embedded = media_selectors.embedded_media(
        reviews, moderator=has_permission(request, "reviews:moderate"), author_path=author_path
    )
    return [review_representation(review, embedded[review.id]) for review in reviews]


def _represent(request, review: Review) -> dict:
    author_path = selectors.is_author_path_for(request, review)
    return _represent_page(request, [review], author_path=author_path)[0]


def _review_etag(request, reviews, *, author_path: bool, now, count: int | None = None) -> str:
    """The validator of a review list or detail, from the rows alone (docs/spec.md 5.3).

    `version` is the content version that guards a publish; status transitions, attestation
    and erasure change the body without bumping it, but every review write saves `updated_at`,
    and every media write bumps `media_version` (`touch_review`). The caller's view picks the
    media URLs; the epoch ages them.
    """

    return etags.etag(
        "reviews",
        etags.deployment(),
        etags.signing_epoch(now),
        has_permission(request, "reviews:moderate"),
        author_path,
        count,
        [
            (str(r.id), r.status, r.version, r.media_version, r.updated_at.isoformat())
            for r in reviews
        ],
    )


def review_representation(review: Review, media: dict | None = None) -> dict:
    media = media or {"media": [], "media_truncated": False}
    return {
        "id": str(review.id),
        "tenant_id": review.tenant_id,
        "application_id": review.application_id,
        "namespace_type": review.namespace_type,
        "namespace_id": review.namespace_id,
        "entity_id": str(review.entity_id),
        "entity_type": review.entity.entity_type,
        "external_id": review.entity.external_id,
        "author_id": review.author_id,
        "author_metadata": review.author_metadata,
        "rating": review.rating,
        "title": review.title,
        "body": review.body,
        "status": review.status,
        "status_reason": review.status_reason,
        "metadata": review.metadata,
        "source_ref": review.source_ref,
        "is_verified_purchase": review.is_verified_purchase,
        "version": review.version,
        "media_version": review.media_version,
        "media": media["media"],
        "media_truncated": media["media_truncated"],
        "published_at": review.published_at.isoformat() if review.published_at else None,
        "created_at": review.created_at.isoformat(),
        "updated_at": review.updated_at.isoformat(),
    }


def _attest_fields_need_scope(request, data) -> None:
    if data.get("source_ref") is not None or data.get("is_verified_purchase"):
        require_permission(request, "reviews:attest")


def _resolve_entity(request, data, ctx):
    if "entity_id" in data:
        return entity_selectors.get_visible_entity(request, data["entity_id"], for_write=True)
    entity, _ = get_or_create_entity(
        tenant_id=request.tenant_id,
        application_id=request.application_id,
        scope_context=request.scope_context,
        entity_type=data["entity_type"],
        external_id=data["external_id"],
        audit_context=ctx,
    )
    return entity


@require_scopes(get="reviews:read", post="reviews:write")
@api_view(["GET", "POST"])
def review_collection(request):
    if request.method == "GET":
        paginator = OctoreviewLimitOffsetPagination()
        page = list(paginator.paginate_queryset(selectors.list_reviews(request), request))
        now = timezone.now()
        author_path = selectors.is_author_path(request)
        tag = _review_etag(request, page, author_path=author_path, now=now, count=paginator.count)
        if etags.matches(request, tag):
            return etags.not_modified(tag)
        data = _represent_page(request, page, author_path=author_path)
        valid = etags.media_urls_outlive((m for review in data for m in review["media"]), now)
        return etags.with_validators(paginator.get_paginated_response(data), tag, valid=valid)

    data = _validated(ReviewCreateSerializer, request.data)
    _attest_fields_need_scope(request, data)
    ctx = AuditContext.from_request(request)
    entity = _resolve_entity(request, data, ctx)
    review = services.create_review(
        entity,
        services.NewReview(
            author_id=data["author_id"],
            rating=data["rating"],
            title=data.get("title"),
            body=data.get("body"),
            metadata=data.get("metadata"),
            author_metadata=data.get("author_metadata"),
            source_ref=data.get("source_ref"),
            is_verified_purchase=data.get("is_verified_purchase", False),
        ),
        ctx,
    )
    return data_response(_represent(request, review), status=status.HTTP_201_CREATED)


@require_scopes(get="reviews:read", patch="reviews:write", delete="reviews:write")
@api_view(["GET", "PATCH", "DELETE"])
def review_detail(request, review_id):
    if request.method == "GET":
        review = selectors.get_review(request, review_id)
        now = timezone.now()
        author_path = selectors.is_author_path_for(request, review)
        tag = _review_etag(request, [review], author_path=author_path, now=now)
        if etags.matches(request, tag):
            return etags.not_modified(tag)
        data = _represent_page(request, [review], author_path=author_path)[0]
        valid = etags.media_urls_outlive(data["media"], now)
        return etags.with_validators(data_response(data), tag, valid=valid)
    review = selectors.get_review(request, review_id, for_write=True)
    ctx = AuditContext.from_request(request)
    if request.method == "DELETE":
        review = services.transition_review(
            review.id, transitions.REMOVE, ctx, tenant_id=review.tenant_id
        )
        return data_response(_represent(request, review))
    if "source_ref" in request.data:
        raise DomainError(
            "source_ref is set only when the review is created; create a new review for "
            "another order.",
            code="source_ref_immutable",
        )
    data = _validated(ReviewPatchSerializer, request.data)
    if "is_verified_purchase" in data:
        require_permission(request, "reviews:attest")
    review = services.update_review(review.id, dict(data), ctx, tenant_id=review.tenant_id)
    return data_response(_represent(request, review))


def _moderate(request, review_id, action):
    review = selectors.get_review(request, review_id, for_write=True)
    ctx = AuditContext.from_request(request)
    if action == transitions.PUBLISH:
        data = _validated(GuardedSerializer, request.data)
    else:
        data = _validated(ReasonSerializer, request.data)
    review = services.transition_review(
        review.id,
        action,
        ctx,
        tenant_id=review.tenant_id,
        reason=data.get("reason"),
        expected_version=data.get("expected_version"),
        expected_media_version=data.get("expected_media_version"),
    )
    return data_response(_represent(request, review))


@require_scopes(post="reviews:moderate")
@api_view(["POST"])
def review_publish(request, review_id):
    return _moderate(request, review_id, transitions.PUBLISH)


@require_scopes(post="reviews:moderate")
@api_view(["POST"])
def review_reject(request, review_id):
    return _moderate(request, review_id, transitions.REJECT)


@require_scopes(post="reviews:moderate")
@api_view(["POST"])
def review_hide(request, review_id):
    return _moderate(request, review_id, transitions.HIDE)


def revision_representation(revision: ReviewRevision) -> dict:
    return {
        "id": str(revision.id),
        "review_id": str(revision.review_id),
        "rating": revision.rating,
        "title": revision.title,
        "body": revision.body,
        "base_version": revision.base_version,
        "created_at": revision.created_at.isoformat(),
    }


@require_scopes(get="reviews:moderate")
@api_view(["GET"])
def review_revision(request, review_id):
    review = selectors.get_review(request, review_id)
    revision = ReviewRevision.objects.filter(review=review).first()
    if revision is None:
        raise NotFoundError("This review has no pending revision.")
    return data_response(revision_representation(revision))


@require_scopes(post="reviews:moderate")
@api_view(["POST"])
def revision_approve(request, review_id):
    review = selectors.get_review(request, review_id, for_write=True)
    data = _validated(GuardedRevisionSerializer, request.data)
    review = services.approve_revision(
        review.id,
        data["revision_id"],
        AuditContext.from_request(request),
        tenant_id=review.tenant_id,
        expected_version=data["expected_version"],
        expected_media_version=data["expected_media_version"],
    )
    return data_response(_represent(request, review))


@require_scopes(post="reviews:moderate")
@api_view(["POST"])
def revision_reject(request, review_id):
    review = selectors.get_review(request, review_id, for_write=True)
    data = _validated(RevisionActionSerializer, request.data)
    review = services.reject_revision(
        review.id,
        data["revision_id"],
        AuditContext.from_request(request),
        tenant_id=review.tenant_id,
    )
    return data_response(_represent(request, review))


@require_scopes(post="privacy:erase")
@api_view(["POST"])
def authors_erase(request):
    data = _validated(EraseSerializer, request.data)
    result = erasure.erase_author(
        tenant_id=request.tenant_id,
        application_id=request.application_id,
        author_id=data["author_id"],
        audit_context=AuditContext.from_request(request),
    )
    return data_response(result.as_dict())


# Erasure acts on the whole application, so an exact-namespace grant cannot run it (spec 2).
authors_erase.cls.requires_application_wide_grant = True
