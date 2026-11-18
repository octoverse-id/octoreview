import pytest

from octoreview.audit import services as audit_services
from octoreview.core.audit_context import AuditContext
from tests.conftest import postgres_only

pytestmark = pytest.mark.django_db


def test_valid_inbound_request_id_is_kept(api_client):
    response = api_client.get("/api/v1/health/live", HTTP_X_REQUEST_ID="client-req_1.2:3")
    assert response["X-Request-ID"] == "client-req_1.2:3"


@pytest.mark.parametrize("bad", ["has space", "a/b", "x" * 101, ""])
def test_invalid_inbound_request_id_is_replaced(api_client, bad):
    response = api_client.get("/api/v1/health/live", HTTP_X_REQUEST_ID=bad)
    assert response["X-Request-ID"].startswith("req_")
    assert len(response["X-Request-ID"]) == 36


def test_hundred_character_request_id_is_kept(api_client):
    value = "r" * 100
    response = api_client.get("/api/v1/health/live", HTTP_X_REQUEST_ID=value)
    assert response["X-Request-ID"] == value


def test_errors_carry_the_request_id(api_client):
    response = api_client.get("/api/v1/audit-logs", HTTP_X_REQUEST_ID="trace-me")
    assert response.status_code == 401
    assert response["X-Request-ID"] == "trace-me"
    assert response.json()["error"]["request_id"] == "trace-me"


@postgres_only
def test_hundred_character_request_id_fits_the_audit_column():
    # The 100-character limit exists because PostgreSQL enforces varchar(100); 101 would 500.
    entry = audit_services.record(
        tenant_id="tenant-a",
        action="test.recorded",
        aggregate_type="test",
        aggregate_id="1",
        audit_context=AuditContext(request_id="r" * 100),
    )
    entry.refresh_from_db()
    assert len(entry.request_id) == 100
