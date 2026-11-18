"""Media endpoints (docs/spec.md sections 5, 7.3-7.5)."""

from __future__ import annotations

import re

from django.conf import settings
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers, status
from rest_framework.decorators import api_view

from octoreview.core.audit_context import AuditContext
from octoreview.core.auth import has_permission, require_scopes
from octoreview.core.errors import DomainError, json_error
from octoreview.core.media_types import ALL_CONTENT_TYPES, KINDS
from octoreview.core.responses import data_response
from octoreview.lifecycle import media as media_lifecycle
from octoreview.media import files, tokens
from octoreview.media import selectors as media_selectors
from octoreview.media.models import MediaStatus, ReviewMedia
from octoreview.reviews import selectors as review_selectors

UPLOAD_CHUNK = 1024 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _validated(serializer_class, data):
    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        raise DomainError(details={"fields": serializer.errors})
    return serializer.validated_data


class IntentSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=KINDS)
    content_type = serializers.ChoiceField(choices=ALL_CONTENT_TYPES)
    size_bytes = serializers.IntegerField(min_value=1)
    client_upload_id = serializers.CharField(max_length=100, required=False)
    position = serializers.IntegerField(min_value=0, required=False, default=0)
    width = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    height = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    duration_seconds = serializers.FloatField(min_value=0, required=False, allow_null=True)

    def to_internal_value(self, data):
        # An unknown content type is a policy answer (content_type_not_allowed with the allowed
        # list), not a generic field error.
        if isinstance(data, dict) and data.get("content_type") not in ALL_CONTENT_TYPES:
            if "content_type" in data:
                raise DomainError(
                    "This content type is not allowed; convert the file to one of the allowed "
                    "types.",
                    {"allowed_content_types": list(ALL_CONTENT_TYPES)},
                    code="content_type_not_allowed",
                )
        return super().to_internal_value(data)


class ModerationSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_null=True)


class GuardedModerationSerializer(ModerationSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    expected_media_version = serializers.IntegerField(min_value=1)


def _caller_audience(request, review, media):
    return media_selectors.audience_for(
        media,
        review,
        moderator=has_permission(request, "reviews:moderate"),
        author_path=review_selectors.is_author_path_for(request, review),
    )


def _represent(request, review, media) -> dict:
    return media_selectors.media_representation(
        media, review, audience=_caller_audience(request, review, media)
    )


@require_scopes(get="reviews:read", post="media:write")
@api_view(["GET", "POST"])
def media_collection(request, review_id):
    if request.method == "GET":
        review = review_selectors.get_review(request, review_id)
        rows = ReviewMedia.objects.filter(review=review).exclude(status=MediaStatus.DELETED)
        rows = rows.order_by("position", "created_at", "id")
        return data_response([_represent(request, review, row) for row in rows])

    review = review_selectors.get_review(request, review_id, for_write=True)
    data = _validated(IntentSerializer, request.data)
    result = media_lifecycle.create_intent(
        review.id, media_lifecycle.IntentRequest(**data), AuditContext.from_request(request)
    )
    upload = None
    if result.target is not None:
        upload = {
            "method": result.target.method,
            "url": result.target.url,
            "headers": result.target.headers,
            "expires_at": result.target.expires_at.isoformat(),
        }
    body = {"media": _represent(request, review, result.media), "upload": upload}
    return data_response(
        body, status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    )


@require_scopes(post="media:write")
@api_view(["POST"])
def media_complete(request, review_id, media_id):
    review = review_selectors.get_review(request, review_id, for_write=True)
    media = media_lifecycle.complete(review.id, media_id, AuditContext.from_request(request))
    review.refresh_from_db()
    return data_response(_represent(request, review, media))


@require_scopes(delete="media:write")
@api_view(["DELETE"])
def media_detail(request, review_id, media_id):
    review = review_selectors.get_review(request, review_id, for_write=True)
    media = media_lifecycle.delete_media(review.id, media_id, AuditContext.from_request(request))
    return data_response(_represent(request, review, media))


def _moderate(request, review_id, media_id, action):
    review = review_selectors.get_review(request, review_id, for_write=True)
    serializer = GuardedModerationSerializer if action == "approve" else ModerationSerializer
    data = _validated(serializer, request.data)
    media = media_lifecycle.moderate_media(
        review.id,
        media_id,
        action,
        AuditContext.from_request(request),
        reason=data.get("reason"),
        expected_version=data.get("expected_version"),
        expected_media_version=data.get("expected_media_version"),
    )
    review.refresh_from_db()
    return data_response(_represent(request, review, media))


@require_scopes(post="reviews:moderate")
@api_view(["POST"])
def media_approve(request, review_id, media_id):
    return _moderate(request, review_id, media_id, "approve")


@require_scopes(post="reviews:moderate")
@api_view(["POST"])
def media_reject(request, review_id, media_id):
    return _moderate(request, review_id, media_id, "reject")


# --- local backend token endpoints (plain Django views; no API key) -----------------------------


def _security_headers(response, content_type: str):
    response["Content-Type"] = content_type
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cross-Origin-Resource-Policy"] = settings.MEDIA_CORP
    return response


def _token_error(request, exc, purpose: str):
    prefix = "upload" if purpose == tokens.UPLOAD else "media"
    if isinstance(exc, tokens.TokenExpired):
        return json_error(
            request,
            f"{prefix}_token_expired",
            "This URL expired; request a fresh one.",
            410,
        )
    return json_error(
        request, f"{prefix}_token_invalid", "This URL is not valid; request a fresh one.", 403
    )


@csrf_exempt
def media_upload(request, token):
    """PUT the bytes of a pending media item (docs/spec.md section 7.5)."""

    if request.method != "PUT":
        return json_error(request, "method_not_allowed", "Use PUT to upload.", 405)
    try:
        claims = tokens.verify(token, tokens.UPLOAD, now=timezone.now())
    except (tokens.TokenInvalid, tokens.TokenExpired) as exc:
        return _token_error(request, exc, tokens.UPLOAD)
    media = ReviewMedia.objects.filter(pk=claims.media_id, staging_key=claims.key).first()
    if media is None:
        return json_error(request, "upload_token_invalid", "This URL is not valid.", 403)
    if media.status != MediaStatus.PENDING:
        return json_error(
            request,
            "upload_already_completed",
            "This upload already completed; create a new intent to upload another file.",
            409,
        )
    raw_length = request.META.get("CONTENT_LENGTH")
    if not raw_length:
        return json_error(
            request,
            "content_length_required",
            "Send Content-Length with the upload; chunked uploads are not accepted.",
            411,
        )
    try:
        length = int(raw_length)
    except ValueError:
        return json_error(
            request, "content_length_required", "Content-Length must be a number.", 411
        )
    if length > media.size_bytes:
        return json_error(
            request,
            "upload_too_large",
            f"The body is larger than the declared {media.size_bytes} bytes.",
            413,
            {"max_bytes": media.size_bytes},
        )

    def chunks():
        while True:
            chunk = request.read(UPLOAD_CHUNK)
            if not chunk:
                return
            yield chunk

    try:
        files.write_stream(media.staging_key, chunks(), limit=media.size_bytes)
    except ValueError:
        return json_error(
            request,
            "upload_too_large",
            f"The body is larger than the declared {media.size_bytes} bytes.",
            413,
            {"max_bytes": media.size_bytes},
        )
    except files.StorageError:
        return json_error(
            request, "storage_unavailable", "The media volume failed; retry the upload.", 503
        )
    return HttpResponse(status=204)


# Plain Django views name the methods they serve; `make openapi-check` reads these to match the
# routes against docs/openapi.yaml (DRF views expose theirs on `.cls`).
media_upload.http_methods = ("PUT",)


def _parse_range(header: str | None, size: int):
    """Return (start, end) inclusive, None for a full response, or "invalid" for 416."""

    if not header:
        return None
    if "," in header:
        return None  # multi-range: serve the whole body with 200
    match = _RANGE.match(header.strip())
    if not match:
        return "invalid"
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return "invalid"
    if not start_text:
        suffix = int(end_text)
        if suffix == 0:
            return "invalid"
        return max(0, size - suffix), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        return "invalid"
    return start, min(end, size - 1)


def media_file(request, token):
    """Serve a local media file to the token's audience (docs/spec.md section 7.5)."""

    if request.method not in ("GET", "HEAD"):
        return json_error(request, "method_not_allowed", "Use GET to download.", 405)
    try:
        claims = tokens.verify(token, tokens.DOWNLOAD, now=timezone.now())
    except (tokens.TokenInvalid, tokens.TokenExpired) as exc:
        return _token_error(request, exc, tokens.DOWNLOAD)
    media = (
        ReviewMedia.objects.select_related("review")
        .filter(pk=claims.media_id, storage_key=claims.key)
        .first()
    )
    revoked = media is None or media.status != MediaStatus.READY
    if not revoked and claims.audience == media_selectors.PUBLIC:
        revoked = not media_selectors.is_publicly_visible(media, media.review)
    if revoked:
        return json_error(request, "not_found", "This media is not available.", 404)
    try:
        handle = files.open_file(media.storage_key)
    except (OSError, files.StorageError):
        return json_error(request, "not_found", "This media is not available.", 404)
    size = media.size_bytes
    byte_range = _parse_range(request.headers.get("Range"), size)
    if byte_range == "invalid":
        handle.close()
        response = json_error(
            request, "range_not_satisfiable", "The range is not satisfiable.", 416
        )
        response["Content-Range"] = f"bytes */{size}"
        return response
    if byte_range is None:
        response = FileResponse(handle, content_type=media.content_type)
        response["Content-Length"] = str(size)
    else:
        start, end = byte_range
        handle.seek(start)
        length = end - start + 1

        def body():
            remaining = length
            try:
                while remaining > 0:
                    chunk = handle.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        return
                    remaining -= len(chunk)
                    yield chunk
            finally:
                handle.close()

        response = StreamingHttpResponse(body(), status=206, content_type=media.content_type)
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, max-age=60"
    return _security_headers(response, media.content_type)


media_file.http_methods = ("GET",)
