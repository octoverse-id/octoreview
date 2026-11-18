import pytest

from octoreview.audit import services
from octoreview.audit.models import AuditLog
from octoreview.core.audit_context import AuditContext

pytestmark = pytest.mark.django_db

URL = "/api/v1/audit-logs"


def make_row(**overrides):
    values = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "action": "review.created",
        "aggregate_type": "review",
        "aggregate_id": "r1",
        "audit_context": AuditContext(actor_id="actor-1", request_id="req_1"),
    }
    values.update(overrides)
    return services.record(**values)


def test_lists_only_the_callers_tenant_application_and_partition(api_client, make_key, headers):
    mine = make_row()
    make_row(tenant_id="tenant-b")
    make_row(application_id="app-2")
    make_row(namespace_type="merchant", namespace_id="m-1")
    token = make_key({"application_id": "app-1", "scopes": ["audit:read"]})
    response = api_client.get(URL + "?application_id=app-1", **headers(token))
    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body["data"]] == [str(mine.id)]
    assert body["pagination"] == {"limit": 20, "offset": 0, "count": 1}


def test_exact_namespace_grant_sees_only_its_namespace(api_client, make_key, headers):
    make_row()
    ns_row = make_row(namespace_type="merchant", namespace_id="m-1")
    make_row(namespace_type="merchant", namespace_id="m-2")
    token = make_key(
        {
            "application_id": "app-1",
            "namespace_type": "merchant",
            "namespace_id": "m-1",
            "scopes": ["audit:read"],
        }
    )
    ns = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}
    response = api_client.get(
        URL + "?application_id=app-1&include_global=true", **headers(token, **ns)
    )
    # include_global is silently ignored: the exact grant does not cover the global partition.
    assert [row["id"] for row in response.json()["data"]] == [str(ns_row.id)]


def test_filters(api_client, make_key, headers):
    make_row(action="review.created")
    wanted = make_row(action="review.published", audit_context=AuditContext())
    token = make_key({"scopes": ["audit:read"]})
    response = api_client.get(
        URL + "?application_id=app-1&action=review.published", **headers(token)
    )
    assert [row["id"] for row in response.json()["data"]] == [str(wanted.id)]


def test_pagination_bounds(api_client, make_key, headers):
    token = make_key({"scopes": ["audit:read"]})
    capped = api_client.get(URL + "?limit=1000", **headers(token))
    assert capped.json()["pagination"]["limit"] == 100
    too_deep = api_client.get(URL + "?offset=10001", **headers(token))
    assert too_deep.status_code == 400
    assert "offset" in too_deep.json()["error"]["details"]["fields"]


def test_diff_lists_only_changed_fields():
    assert services.diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == {
        "b": [2, 3],
        "c": [None, 4],
    }


def test_audit_row_records_the_request_context():
    row = make_row()
    row.refresh_from_db()
    assert row.actor_id == "actor-1"
    assert row.request_id == "req_1"
    assert AuditLog.objects.count() == 1
