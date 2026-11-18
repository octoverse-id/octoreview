import pytest

from octoreview.audit.models import AuditLog
from octoreview.entities.models import ApplicationSettings, Entity
from octoreview.entities.policy import effective_policy

pytestmark = pytest.mark.django_db

URL = "/api/v1/applications/app-1/settings"
SCOPES = ["settings:read", "settings:write"]
FULL = {
    "max_images": 2,
    "max_videos": None,
    "max_image_bytes": None,
    "max_video_bytes": None,
    "allowed_content_types": ["image/png", "image/jpeg", "image/png"],
    "moderation_mode": "pre_moderation",
    "max_storage_bytes": None,
}


@pytest.fixture
def key(make_key):
    return make_key({"application_id": "app-1", "scopes": SCOPES})


def test_get_without_a_row_returns_nulls(api_client, headers, key):
    response = api_client.get(URL, **headers(key))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["application_id"] == "app-1"
    assert data["moderation_mode"] is None


def test_put_requires_every_field(api_client, headers, key):
    response = api_client.put(URL, {"max_images": 2}, format="json", **headers(key))
    assert response.status_code == 400
    assert "moderation_mode" in response.json()["error"]["details"]["fields"]


def test_put_stores_and_audits(api_client, headers, key):
    response = api_client.put(URL, FULL, format="json", **headers(key))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["allowed_content_types"] == ["image/jpeg", "image/png"]
    assert data["max_images"] == 2
    assert AuditLog.objects.filter(action="application_settings.updated").count() == 1
    again = api_client.put(URL, FULL, format="json", **headers(key))
    assert again.status_code == 200
    assert AuditLog.objects.filter(action="application_settings.updated").count() == 1


@pytest.mark.parametrize(
    "field, value",
    [
        ("max_images", 21),
        ("allowed_content_types", ["image/gif"]),
        ("allowed_content_types", []),
        ("moderation_mode", "sometimes"),
    ],
)
def test_put_validation(api_client, headers, key, field, value):
    response = api_client.put(URL, {**FULL, field: value}, format="json", **headers(key))
    assert response.status_code == 400
    assert field in response.json()["error"]["details"]["fields"]


def test_exact_namespace_grant_gets_403(api_client, headers, make_key):
    token = make_key(
        {
            "application_id": "app-1",
            "namespace_type": "merchant",
            "namespace_id": "m-1",
            "scopes": SCOPES,
        }
    )
    ns = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}
    response = api_client.get(URL, **headers(token, **ns))
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "application_wide_grant_required"


def test_wildcard_grant_may_manage_settings(api_client, headers, make_key):
    token = make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})
    assert api_client.get(URL, **headers(token)).status_code == 200


def test_path_and_query_application_must_agree(api_client, headers, key):
    response = api_client.get(URL + "?application_id=app-2", **headers(key))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "application_mismatch"


def test_effective_policy_resolves_each_field(settings):
    entity = Entity(tenant_id="t", application_id="a", entity_type="p", external_id="1")
    entity.max_videos = 2
    app = ApplicationSettings(tenant_id="t", application_id="a", max_images=3)
    policy = effective_policy(entity, app)
    assert policy.max_images == 3 and policy.sources["max_images"] == "application"
    assert policy.max_videos == 2 and policy.sources["max_videos"] == "entity"
    assert policy.max_image_bytes == settings.MEDIA_POLICY_DEFAULTS["max_image_bytes"]
    assert policy.sources["max_image_bytes"] == "server"
    assert len(policy.allowed_content_types) == 6
    assert effective_policy(entity, None).max_images == settings.MEDIA_POLICY_DEFAULTS["max_images"]
