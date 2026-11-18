"""The error envelope and the error-code registry (docs/spec.md section 11).

Every API error, including an unexpected 500, leaves through `error_response`, so clients can
always read `error.code`, `message`, `details`, `request_id`, `doc_url` and `retryable`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.http import Http404, JsonResponse
from rest_framework import exceptions, status
from rest_framework.response import Response

logger = logging.getLogger("octoreview.errors")


@dataclass(frozen=True)
class ErrorCode:
    code: str
    status: int
    retryable: bool = False


# Every code the API can return. docs/errors.md has one anchor per entry, and a test keeps the
# two in step. Add a code here before raising it.
ERROR_CODES: dict[str, ErrorCode] = {
    entry.code: entry
    for entry in (
        ErrorCode("validation_error", 400),
        ErrorCode("invalid_namespace", 400),
        ErrorCode("application_mismatch", 400),
        ErrorCode("media_too_large", 400),
        ErrorCode("content_type_not_allowed", 400),
        ErrorCode("authentication_required", 401),
        ErrorCode("invalid_api_key", 401),
        ErrorCode("permission_denied", 403),
        ErrorCode("upload_token_invalid", 403),
        ErrorCode("media_token_invalid", 403),
        ErrorCode("not_found", 404),
        ErrorCode("method_not_allowed", 405),
        ErrorCode("not_acceptable", 406),
        ErrorCode("review_exists", 409),
        ErrorCode("entity_inactive", 409),
        ErrorCode("invalid_transition", 409),
        ErrorCode("review_changed", 409),
        ErrorCode("revision_superseded", 409),
        ErrorCode("revision_stale", 409),
        ErrorCode("source_ref_immutable", 409),
        ErrorCode("review_not_accepting_media", 409),
        ErrorCode("media_limit_exceeded", 409),
        ErrorCode("storage_quota_exceeded", 409),
        ErrorCode("upload_expired", 409),
        ErrorCode("upload_not_found", 409),
        ErrorCode("upload_changed", 409, retryable=True),
        ErrorCode("complete_in_progress", 409, retryable=True),
        ErrorCode("upload_already_completed", 409),
        ErrorCode("upload_token_expired", 410),
        ErrorCode("media_token_expired", 410),
        ErrorCode("content_length_required", 411),
        ErrorCode("upload_too_large", 413),
        ErrorCode("unsupported_media_type", 415),
        ErrorCode("range_not_satisfiable", 416),
        ErrorCode("media_rejected", 422),
        ErrorCode("internal_error", 500),
        ErrorCode("storage_unavailable", 503, retryable=True),
        ErrorCode("storage_credentials_expiring", 503, retryable=True),
        ErrorCode("too_many_completions", 503, retryable=True),
    )
}


class DomainError(Exception):
    """A product error with a registered code. Subclasses set `code` and a default message.

    `message` should end with the fix the caller can apply (docs/spec.md section 11).
    """

    code = "validation_error"
    message = "Request validation failed; correct the fields listed in details and retry."
    retry_after: int | None = None

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        *,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        if self.code not in ERROR_CODES:
            raise ValueError(f"Unregistered error code {self.code!r}; add it to ERROR_CODES.")
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details or {}
        if retry_after is not None:
            self.retry_after = retry_after

    @property
    def status_code(self) -> int:
        return ERROR_CODES[self.code].status


class InvalidNamespaceError(DomainError):
    code = "invalid_namespace"
    message = (
        "X-Namespace-Type and X-Namespace-ID must be sent together, non-blank, at most 100 "
        "characters, and the type cannot be 'global'; send both or neither."
    )


class ApplicationMismatchError(DomainError):
    code = "application_mismatch"
    message = "The path, body and query name different application_id values; send one value."


class PermissionDeniedError(DomainError):
    code = "permission_denied"
    message = "This service key is not granted this action; use a key whose grant covers it."


class NotFoundError(DomainError):
    code = "not_found"
    message = (
        "Resource not found in this tenant, application and namespace; check the id and scope."
    )


def _doc_url(code: str) -> str:
    return f"{settings.ERROR_DOC_BASE_URL}#{code}"


def error_payload(code: str, message: str, details: Any, request_id: str | None) -> dict:
    entry = ERROR_CODES.get(code) or ERROR_CODES["internal_error"]
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details if details is not None else {},
            "request_id": request_id,
            "doc_url": _doc_url(code),
            "retryable": entry.retryable,
        }
    }


def _stamp_error_code(request, code: str) -> None:
    # The request log reports the failure reason; every API error passes through here, so this
    # is the one place that sees the resolved code.
    if request is not None:
        underlying = getattr(request, "_request", request)
        underlying.error_code = code


def error_response(
    code: str,
    message: str,
    details: Any,
    request,
    http_status: int,
    *,
    retry_after: int | None = None,
) -> Response:
    _stamp_error_code(request, code)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return Response(
        error_payload(code, message, details, getattr(request, "request_id", None)),
        status=http_status,
        headers=headers,
    )


def exception_handler(exc, context):
    request = context.get("request")

    if isinstance(exc, DomainError):
        return error_response(
            exc.code,
            exc.message,
            exc.details,
            request,
            exc.status_code,
            retry_after=exc.retry_after,
        )

    if isinstance(exc, Http404 | exceptions.NotFound):
        return error_response(
            "not_found", NotFoundError.message, {}, request, status.HTTP_404_NOT_FOUND
        )

    if isinstance(exc, exceptions.NotAuthenticated):
        return error_response(
            "authentication_required",
            "Send Authorization: Bearer <service key>; the header was missing or malformed.",
            {},
            request,
            status.HTTP_401_UNAUTHORIZED,
        )
    if isinstance(exc, exceptions.AuthenticationFailed):
        return error_response(
            "invalid_api_key",
            "The service key is unknown, revoked or expired; use an active key.",
            {},
            request,
            status.HTTP_401_UNAUTHORIZED,
        )
    if isinstance(exc, exceptions.PermissionDenied):
        return error_response(
            "permission_denied",
            PermissionDeniedError.message,
            {"reason": "permission_denied"},
            request,
            status.HTTP_403_FORBIDDEN,
        )
    if isinstance(exc, exceptions.MethodNotAllowed):
        return error_response(
            "method_not_allowed",
            f"{request.method if request else 'This method'} is not allowed here; "
            "see docs/openapi.yaml for the allowed methods.",
            {},
            request,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
    if isinstance(exc, exceptions.NotAcceptable):
        return error_response(
            "not_acceptable",
            "The API only renders application/json; send Accept: application/json.",
            {},
            request,
            status.HTTP_406_NOT_ACCEPTABLE,
        )
    if isinstance(exc, exceptions.UnsupportedMediaType):
        return error_response(
            "unsupported_media_type",
            "Send the body as application/json.",
            {},
            request,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    if isinstance(exc, exceptions.ParseError):
        return error_response(
            "validation_error",
            "The request body is not valid JSON; send a JSON object.",
            {"fields": {"body": [str(exc.detail)]}},
            request,
            status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, exceptions.ValidationError):
        return error_response(
            "validation_error",
            DomainError.message,
            {"fields": exc.detail},
            request,
            status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, exceptions.APIException):
        return error_response(
            "validation_error",
            str(exc.detail),
            {},
            request,
            exc.status_code,
        )

    # Anything else is a bug. Log it with the request id and still answer in the envelope, so
    # clients never parse an HTML error page.
    logger.exception(
        "unhandled_exception", extra={"request_id": getattr(request, "request_id", None)}
    )
    return error_response(
        "internal_error",
        "Unexpected server error; retry later and report the request_id if it persists.",
        {},
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def json_error(request, code: str, message: str, http_status: int, details=None) -> JsonResponse:
    """The envelope for plain Django views (local token endpoints, 404/500 handlers)."""

    _stamp_error_code(request, code)
    return JsonResponse(
        error_payload(code, message, details, getattr(request, "request_id", None)),
        status=http_status,
    )


def handler404(request, exception=None):
    return json_error(request, "not_found", NotFoundError.message, 404)


def handler500(request):
    return json_error(
        request,
        "internal_error",
        "Unexpected server error; retry later and report the request_id if it persists.",
        500,
    )
