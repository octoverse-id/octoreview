from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from octoreview.core.auth import has_permission, require_permission, require_scopes
from octoreview.core.errors import DomainError
from octoreview.service_auth.models import ServiceClient

pytestmark = pytest.mark.django_db

URL = "/api/v1/audit-logs"


def error(response):
    return response.json()["error"]


def test_missing_tenant_header_is_a_validation_error(api_client, make_key, headers):
    token = make_key({"scopes": ["audit:read"]})
    response = api_client.get(URL, **headers(token, tenant=None))
    assert response.status_code == 400
    assert "X-Tenant-ID" in error(response)["details"]["fields"]


def test_malformed_tenant_header_is_refused(api_client, make_key, headers):
    token = make_key({"scopes": ["audit:read"]})
    response = api_client.get(URL, **headers(token, tenant="_staging"))
    assert response.status_code == 400


def test_tenant_not_granted(api_client, make_key, headers):
    token = make_key({"tenant_id": "tenant-b", "scopes": ["audit:read"]})
    response = api_client.get(URL, **headers(token))
    assert response.status_code == 403
    assert error(response)["details"]["reason"] == "tenant_not_granted"


def test_scope_missing_names_required_and_granted_scopes(api_client, make_key, headers):
    token = make_key({"scopes": ["reviews:read"]})
    response = api_client.get(URL, **headers(token))
    assert response.status_code == 403
    details = error(response)["details"]
    assert details == {
        "reason": "scope_missing",
        "required_scope": "audit:read",
        "grant_scope": ["reviews:read"],
    }


def test_application_scoped_grant(api_client, make_key, headers):
    token = make_key({"application_id": "app-1", "scopes": ["audit:read"]})
    assert api_client.get(URL + "?application_id=app-1", **headers(token)).status_code == 200
    denied = api_client.get(URL + "?application_id=app-2", **headers(token))
    assert denied.status_code == 403
    assert error(denied)["details"]["reason"] == "application_not_granted"
    # An application-scoped grant cannot read tenant-wide.
    assert api_client.get(URL, **headers(token)).status_code == 403


def test_exact_namespace_grant(api_client, make_key, headers):
    token = make_key(
        {
            "application_id": "app-1",
            "namespace_type": "merchant",
            "namespace_id": "m-1",
            "scopes": ["audit:read"],
        }
    )
    ns = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}
    ok = api_client.get(URL + "?application_id=app-1", **headers(token, **ns))
    assert ok.status_code == 200
    other = api_client.get(
        URL + "?application_id=app-1",
        **headers(token, **{"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-2"}),
    )
    assert other.status_code == 403
    assert error(other)["details"]["reason"] == "namespace_not_granted"
    # The same key cannot read the global partition.
    assert api_client.get(URL + "?application_id=app-1", **headers(token)).status_code == 403


def test_global_only_grant_cannot_reach_a_namespace(api_client, make_key, headers):
    token = make_key({"application_id": "app-1", "scopes": ["audit:read"]})
    ns = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}
    response = api_client.get(URL + "?application_id=app-1", **headers(token, **ns))
    assert response.status_code == 403


def test_wildcard_grant_reaches_any_namespace(api_client, make_key, headers):
    token = make_key(
        {"application_id": "app-1", "namespace_wildcard": True, "scopes": ["audit:read"]}
    )
    for namespace_id in ("m-1", "m-2"):
        ns = {"X-Namespace-Type": "merchant", "X-Namespace-ID": namespace_id}
        assert (
            api_client.get(URL + "?application_id=app-1", **headers(token, **ns)).status_code == 200
        )


def test_namespaced_request_needs_an_application(api_client, make_key, headers):
    token = make_key({"namespace_wildcard": True, "scopes": ["audit:read"]})
    ns = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}
    response = api_client.get(URL, **headers(token, **ns))
    assert response.status_code == 403
    assert error(response)["details"]["reason"] == "application_required_for_namespace"


@pytest.mark.parametrize(
    "ns",
    [
        {"X-Namespace-Type": "merchant"},
        {"X-Namespace-ID": "m-1"},
        {"X-Namespace-Type": "global", "X-Namespace-ID": "m-1"},
        {"X-Namespace-Type": " ", "X-Namespace-ID": "m-1"},
        {"X-Namespace-Type": "merchant", "X-Namespace-ID": "x" * 101},
    ],
)
def test_invalid_namespace_headers(api_client, make_key, headers, ns):
    token = make_key(
        {"application_id": "app-1", "namespace_wildcard": True, "scopes": ["audit:read"]}
    )
    response = api_client.get(URL + "?application_id=app-1", **headers(token, **ns))
    assert response.status_code == 400
    assert error(response)["code"] == "invalid_namespace"


def test_inactive_and_expired_keys_are_invalid_api_key(api_client, make_key, headers):
    inactive = make_key({"scopes": ["audit:read"]}, name="inactive")
    ServiceClient.objects.filter(name="inactive").update(is_active=False)
    expired = make_key(
        {"scopes": ["audit:read"]}, name="expired", expires_at=timezone.now() - timedelta(minutes=1)
    )
    for token in (inactive, expired):
        response = api_client.get(URL, **headers(token))
        assert response.status_code == 401
        assert error(response)["code"] == "invalid_api_key"


# Secondary permissions and application canonicalisation, through a throwaway view.


@require_scopes(post="reviews:write")
@api_view(["POST"])
def probe_view(request):
    require_permission(request, "reviews:attest")
    return Response({"application_id": request.application_id})


def call_probe(token, *, query="", body=None, tenant="tenant-a"):
    factory = APIRequestFactory()
    request = factory.post(
        f"/probe{query}",
        body or {},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_TENANT_ID=tenant,
    )
    request.request_id = "req_probe"
    request.tenant_id = tenant
    return probe_view(request)


def test_secondary_permission_uses_the_full_predicate(make_key):
    # Write in app A, attest only in app B: attestation in A is refused.
    token = make_key(
        {"application_id": "app-a", "scopes": ["reviews:write"]},
        {"application_id": "app-b", "scopes": ["reviews:attest"]},
    )
    refused = call_probe(token, body={"application_id": "app-a"})
    assert refused.status_code == 403
    assert refused.data["error"]["details"]["required_scope"] == "reviews:attest"

    token_both = make_key(
        {"application_id": "app-a", "scopes": ["reviews:write", "reviews:attest"]}, name="both"
    )
    allowed = call_probe(token_both, body={"application_id": "app-a"})
    assert allowed.status_code == 200


def test_application_mismatch_between_query_and_body(make_key):
    token = make_key({"scopes": ["reviews:write", "reviews:attest"]})
    response = call_probe(token, query="?application_id=app-a", body={"application_id": "app-b"})
    assert response.status_code == 400
    assert response.data["error"]["code"] == "application_mismatch"


def test_include_global_on_a_write_is_refused(make_key):
    token = make_key({"scopes": ["reviews:write", "reviews:attest"]})
    response = call_probe(token, query="?include_global=true")
    assert response.status_code == 400
    assert "include_global" in response.data["error"]["details"]["fields"]


def test_has_permission_without_a_client_is_false(rf):
    request = rf.get("/")
    assert has_permission(request, "reviews:read") is False


def test_unknown_scope_cannot_be_declared():
    with pytest.raises(ValueError):
        require_scopes(get="reviews:everything")


def test_domain_error_default_is_validation_error():
    assert DomainError().code == "validation_error"
