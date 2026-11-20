"""Who reaches the console, and that the REST API does not change because it exists."""

from __future__ import annotations

import pytest
from django.core.checks import run_checks
from rest_framework.test import APIClient

from tests.console.conftest import admin_enabled
from tests.test_media import SCOPES, create_review

pytestmark = pytest.mark.django_db


def test_the_console_is_absent_by_default(client):
    response = client.get("/admin/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_anonymous_visitors_are_sent_to_the_login_page(console, client):
    response = client.get("/admin/")
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]
    assert client.get("/admin/login/").status_code == 200


def test_staff_without_superuser_cannot_enter(console, client, staff_user):
    client.force_login(staff_user)
    assert client.get("/admin/").status_code == 302
    assert client.get("/admin/reviews/review/").status_code == 302


def test_an_inactive_superuser_cannot_enter(console, client, django_user_model):
    ghost = django_user_model.objects.create_superuser(
        username="ghost", email="ghost@example.com", password="pw-ghost-123456", is_active=False
    )
    client.force_login(ghost)
    assert client.get("/admin/").status_code == 302


def test_a_superuser_sees_every_registered_model(console, root_client):
    page = root_client.get("/admin/").content.decode()
    for path in (
        "/admin/reviews/review/",
        "/admin/media/reviewmedia/",
        "/admin/entities/entity/",
        "/admin/service_auth/serviceclient/",
        "/admin/audit/auditlog/",
        "/admin/events/outboxevent/",
    ):
        assert path in page, path


def test_the_api_ignores_sessions_and_csrf(console, root_client, make_key, headers):
    """Bearer keys stay the only credential: a console session does not open the API, and API
    writes need no CSRF token even with CsrfViewMiddleware installed."""

    unauthenticated = root_client.get("/api/v1/reviews?application_id=app-1", HTTP_X_TENANT_ID="t")
    assert unauthenticated.status_code == 401

    token = make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})
    api = APIClient(enforce_csrf_checks=True)
    review = create_review(api, headers, token)
    assert review["id"]
    response = api.get(f"/api/v1/reviews/{review['id']}?application_id=app-1", **headers(token))
    assert response.status_code == 200
    assert "sessionid" not in response.cookies


def test_the_deploy_check_warns_only_when_enabled_in_production(settings):
    def warned():
        return any(m.id == "octoreview.W001" for m in run_checks(include_deployment_checks=True))

    settings.DEBUG = False
    settings.ADMIN_ENABLED = False
    assert not warned()
    settings.ADMIN_ENABLED = True
    assert warned()
    settings.DEBUG = True
    assert not warned()


def test_reloading_restores_the_absent_route(client):
    with admin_enabled(True):
        assert client.get("/admin/login/").status_code == 200
    assert client.get("/admin/login/").status_code == 404
