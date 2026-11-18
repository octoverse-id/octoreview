import pytest

from octoreview.core import errors
from octoreview.core.errors import ERROR_CODES, DomainError

pytestmark = pytest.mark.django_db

ENVELOPE_KEYS = {"code", "message", "details", "request_id", "doc_url", "retryable"}


def test_unknown_route_returns_the_envelope(api_client):
    response = api_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()["error"]
    assert set(body) == ENVELOPE_KEYS
    assert body["code"] == "not_found"
    assert body["doc_url"].endswith("docs/errors.md#not_found")
    assert body["retryable"] is False


def test_missing_bearer_is_authentication_required(api_client):
    response = api_client.get("/api/v1/audit-logs", HTTP_X_TENANT_ID="tenant-a")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


@pytest.mark.parametrize(
    "token",
    ["not-a-key", "ocr_deadbeef_unknownsecret", "ocr__x", "ocr_" + "a" * 300],
)
def test_unknown_or_malformed_key_is_invalid_api_key(api_client, token):
    response = api_client.get(
        "/api/v1/audit-logs", HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_TENANT_ID="tenant-a"
    )
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "invalid_api_key"
    assert token not in str(body)


def test_method_not_allowed_uses_the_envelope(api_client, make_key, headers):
    token = make_key({"scopes": ["audit:read"]})
    response = api_client.post("/api/v1/audit-logs", {}, format="json", **headers(token))
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_retryable_codes_set_the_flag():
    assert ERROR_CODES["storage_unavailable"].retryable is True
    assert ERROR_CODES["complete_in_progress"].retryable is True
    assert ERROR_CODES["validation_error"].retryable is False


def test_unregistered_code_is_refused():
    with pytest.raises(ValueError):
        DomainError("x", code="made_up_code")


def test_domain_error_subclasses_use_registered_codes():
    for cls in DomainError.__subclasses__():
        assert cls.code in ERROR_CODES, cls


def test_unexpected_exception_returns_internal_error_envelope(rf):
    request = rf.get("/api/v1/x")
    request.request_id = "req_test"
    response = errors.exception_handler(RuntimeError("boom"), {"request": request})
    assert response.status_code == 500
    assert response.data["error"]["code"] == "internal_error"
    assert response.data["error"]["request_id"] == "req_test"
    assert "boom" not in str(response.data)


def test_not_found_names_the_scope_it_searched(api_client, headers, make_key):
    token = make_key({"application_id": "app-1", "scopes": ["reviews:read"]})
    response = api_client.get(
        "/api/v1/reviews/00000000-0000-0000-0000-000000000000?application_id=app-1",
        **headers(token),
    )
    assert response.status_code == 404
    assert response.json()["error"]["details"] == {
        "scope": {
            "tenant_id": "tenant-a",
            "application_id": "app-1",
            "namespace_type": None,
            "namespace_id": None,
        }
    }
