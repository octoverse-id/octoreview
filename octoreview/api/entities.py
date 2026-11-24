"""Entity, rating and application-settings endpoints (docs/spec.md sections 3.1, 3.2, 5.1)."""

from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view

from octoreview.core.audit_context import AuditContext
from octoreview.core.auth import require_scopes
from octoreview.core.errors import DomainError, NotFoundError
from octoreview.core.media_types import ALL_CONTENT_TYPES
from octoreview.core.pagination import OctoreviewLimitOffsetPagination
from octoreview.core.responses import data_response
from octoreview.core.validators import entity_type_error, external_id_error, json_object_error
from octoreview.entities import ratings, selectors, services
from octoreview.entities.models import POLICY_FIELDS, Entity, ModerationMode
from octoreview.entities.policy import ceiling_errors
from octoreview.media import selectors as media_selectors

# Summaries change only when a review moves; a short private cache is safe until ETags land
# after rc.1 (docs/spec.md section 5.3).
SUMMARY_CACHE_CONTROL = {"Cache-Control": "private, max-age=60"}
MAX_BATCH_IDS = 100
# 2^53 - 1: inside the bigint column, and exact in every SDK language (JS numbers are doubles).
MAX_STORAGE_BYTES = 2**53 - 1


def validation_error(fields: dict) -> DomainError:
    return DomainError(details={"fields": fields})


def require_application_id(request) -> str:
    if not request.application_id:
        raise validation_error({"application_id": ["This field is required."]})
    return request.application_id


def entity_representation(entity: Entity) -> dict:
    return {
        "id": str(entity.id),
        "tenant_id": entity.tenant_id,
        "application_id": entity.application_id,
        "namespace_type": entity.namespace_type,
        "namespace_id": entity.namespace_id,
        "entity_type": entity.entity_type,
        "external_id": entity.external_id,
        "metadata": entity.metadata,
        "is_active": entity.is_active,
        "media_policy": {field: getattr(entity, field) for field in POLICY_FIELDS},
        "rating": ratings.summary(entity),
        "created_at": entity.created_at.isoformat(),
        "updated_at": entity.updated_at.isoformat(),
    }


class PolicyOverridesMixin(serializers.Serializer):
    max_images = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    max_videos = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    max_image_bytes = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    max_video_bytes = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    metadata = serializers.JSONField(required=False)

    def validate_metadata(self, value):
        error = json_object_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate(self, attrs):
        errors = ceiling_errors(attrs)
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class EntityUpsertSerializer(PolicyOverridesMixin):
    application_id = serializers.CharField()
    entity_type = serializers.CharField()
    external_id = serializers.CharField(trim_whitespace=False)

    def validate_entity_type(self, value):
        error = entity_type_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate_external_id(self, value):
        error = external_id_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value


class EntityPatchSerializer(PolicyOverridesMixin):
    is_active = serializers.BooleanField(required=False)


def _validated(serializer_class, data):
    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        raise validation_error(serializer.errors)
    return serializer.validated_data


VALUE_FIELDS = (*POLICY_FIELDS, "metadata")


@require_scopes(get="entities:read", put="entities:write")
@api_view(["GET", "PUT"])
def entity_collection(request):
    if request.method == "PUT":
        data = _validated(EntityUpsertSerializer, request.data)
        entity, created = services.upsert_entity(
            tenant_id=request.tenant_id,
            application_id=require_application_id(request),
            scope_context=request.scope_context,
            entity_type=data["entity_type"],
            external_id=data["external_id"],
            values={field: data[field] for field in VALUE_FIELDS if field in data},
            audit_context=AuditContext.from_request(request),
        )
        return data_response(
            entity_representation(entity),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    queryset = selectors.visible_entities(request)
    for name in ("entity_type", "external_id"):
        value = request.query_params.get(name)
        if value:
            queryset = queryset.filter(**{name: value})
    is_active = request.query_params.get("is_active")
    if is_active in {"true", "false"}:
        queryset = queryset.filter(is_active=is_active == "true")
    paginator = OctoreviewLimitOffsetPagination()
    page = paginator.paginate_queryset(queryset.order_by("-created_at", "id"), request)
    return paginator.get_paginated_response([entity_representation(e) for e in page])


@require_scopes(get="entities:read", patch="entities:write", delete="entities:write")
@api_view(["GET", "PATCH", "DELETE"])
def entity_detail(request, entity_id):
    ctx = AuditContext.from_request(request)
    if request.method == "GET":
        return data_response(
            entity_representation(selectors.get_visible_entity(request, entity_id))
        )
    entity = selectors.get_visible_entity(request, entity_id, for_write=True)
    if request.method == "DELETE":
        return data_response(entity_representation(services.deactivate_entity(entity, ctx)))
    data = _validated(EntityPatchSerializer, request.data)
    return data_response(entity_representation(services.update_entity(entity, dict(data), ctx)))


@require_scopes(get="entities:read")
@api_view(["GET"])
def entity_lookup(request):
    application_id = require_application_id(request)
    entity_type = request.query_params.get("entity_type")
    external_id = request.query_params.get("external_id")
    missing = {
        name: ["This query parameter is required."]
        for name, value in (("entity_type", entity_type), ("external_id", external_id))
        if not value
    }
    if missing:
        raise validation_error(missing)
    entity = selectors.lookup_entity(
        request, application_id=application_id, entity_type=entity_type, external_id=external_id
    )
    if entity is None:
        raise NotFoundError()
    return data_response(entity_representation(entity))


@require_scopes(get="entities:read")
@api_view(["GET"])
def entity_rating(request, entity_id):
    entity = selectors.get_visible_entity(request, entity_id)
    return data_response(
        {"entity_id": str(entity.id), **ratings.summary(entity)}, headers=SUMMARY_CACHE_CONTROL
    )


class RatingsQuerySerializer(serializers.Serializer):
    application_id = serializers.CharField()
    entity_type = serializers.CharField()
    external_ids = serializers.ListField(
        child=serializers.CharField(trim_whitespace=False),
        min_length=1,
        max_length=MAX_BATCH_IDS,
    )

    def validate_entity_type(self, value):
        error = entity_type_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value


@require_scopes(post="entities:read")
@api_view(["POST"])
def ratings_query(request):
    data = _validated(RatingsQuerySerializer, request.data)
    found = selectors.batch_lookup(
        request,
        application_id=require_application_id(request),
        entity_type=data["entity_type"],
        external_ids=data["external_ids"],
    )
    results = []
    for external_id in data["external_ids"]:
        entity = found.get(external_id)
        results.append(
            {
                "external_id": external_id,
                "entity_id": str(entity.id) if entity is not None else None,
                "namespace_type": entity.namespace_type if entity is not None else None,
                "namespace_id": entity.namespace_id if entity is not None else None,
                **ratings.summary(entity),
            }
        )
    return data_response(results, headers=SUMMARY_CACHE_CONTROL)


# The batch query is a read sent as POST (100 ids exceed gunicorn's request line); reads accept
# include_global and take application_id from the body (docs/spec.md section 1).
ratings_query.cls.read_only_post = True


class SettingsSerializer(serializers.Serializer):
    max_images = serializers.IntegerField(min_value=0, allow_null=True)
    max_videos = serializers.IntegerField(min_value=0, allow_null=True)
    max_image_bytes = serializers.IntegerField(min_value=1, allow_null=True)
    max_video_bytes = serializers.IntegerField(min_value=1, allow_null=True)
    allowed_content_types = serializers.ListField(
        child=serializers.ChoiceField(choices=ALL_CONTENT_TYPES), allow_null=True, allow_empty=False
    )
    moderation_mode = serializers.ChoiceField(choices=ModerationMode.choices, allow_null=True)
    max_storage_bytes = serializers.IntegerField(
        min_value=0, max_value=MAX_STORAGE_BYTES, allow_null=True
    )

    def validate_allowed_content_types(self, value):
        if value is None:
            return None
        return sorted(set(value), key=ALL_CONTENT_TYPES.index)

    def validate(self, attrs):
        errors = ceiling_errors(attrs)
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


def settings_representation(tenant_id: str, application_id: str, row) -> dict:
    values = {field: getattr(row, field, None) for field in services.SETTINGS_FIELDS}
    # Computed SUMs, not counters (docs/spec.md 7.6): nothing to keep in step or recompute.
    usage = media_selectors.storage_usage(tenant_id, application_id, now=timezone.now())
    return {"tenant_id": tenant_id, "application_id": application_id, **values, **usage}


@require_scopes(get="settings:read", put="settings:write")
@api_view(["GET", "PUT"])
def application_settings(request, application_id):
    if request.method == "PUT":
        missing = [field for field in services.SETTINGS_FIELDS if field not in request.data]
        if missing:
            # PUT replaces every field; explicit null means "inherit" (docs/spec.md 3.1).
            raise DomainError(
                "PUT needs every settings field; send null to inherit the server default.",
                {"fields": {field: ["This field is required."] for field in missing}},
            )
        data = _validated(SettingsSerializer, request.data)
        row = services.put_settings(
            tenant_id=request.tenant_id,
            application_id=application_id,
            values=dict(data),
            audit_context=AuditContext.from_request(request),
        )
        return data_response(settings_representation(request.tenant_id, application_id, row))
    row = services.get_settings(request.tenant_id, application_id)
    return data_response(settings_representation(request.tenant_id, application_id, row))


application_settings.cls.requires_application_wide_grant = True
