import pytest

pytestmark = pytest.mark.django_db


def test_live_needs_no_key(api_client):
    response = api_client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}}


def test_ready_checks_the_database(api_client):
    response = api_client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["data"]["database"] == "ok"
