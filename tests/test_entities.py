import pytest
from django.db import IntegrityError, transaction

from octoreview.audit.models import AuditLog
from octoreview.entities.models import Entity

pytestmark = pytest.mark.django_db

URL = "/api/v1/entities"
NS = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}
ALL = ["entities:read", "entities:write"]


@pytest.fixture
def key(make_key):
    return make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": ALL})


def upsert(api_client, headers, token, ns=None, **body):
    payload = {"application_id": "app-1", "entity_type": "product", "external_id": "sku-1"}
    payload.update(body)
    return api_client.put(URL, payload, format="json", **headers(token, **(ns or {})))


def test_upsert_creates_then_updates(api_client, headers, key):
    created = upsert(api_client, headers, key, metadata={"color": "red"}, max_images=3)
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["entity_type"] == "product"
    assert data["media_policy"]["max_images"] == 3
    assert data["rating"]["rating_count"] == 0
    updated = upsert(api_client, headers, key, metadata={"color": "blue"})
    assert updated.status_code == 200
    assert updated.json()["data"]["id"] == data["id"]
    assert updated.json()["data"]["metadata"] == {"color": "blue"}
    assert Entity.objects.count() == 1
    assert list(AuditLog.objects.order_by("created_at").values_list("action", flat=True)) == [
        "entity.created",
        "entity.updated",
    ]


def test_upsert_never_reactivates(api_client, headers, key):
    entity_id = upsert(api_client, headers, key).json()["data"]["id"]
    api_client.delete(f"{URL}/{entity_id}?application_id=app-1", **headers(key))
    again = upsert(api_client, headers, key, metadata={"x": 1})
    assert again.json()["data"]["is_active"] is False


@pytest.mark.parametrize(
    "field, value",
    [
        ("entity_type", "Product"),
        ("entity_type", "_x"),
        ("external_id", ""),
        ("external_id", "a\nb"),
        ("external_id", "x" * 256),
        ("max_images", 21),
        ("max_video_bytes", 524_288_001),
        ("metadata", ["not", "an", "object"]),
        ("metadata", {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}),
        ("metadata", {"blob": "x" * 17_000}),
    ],
)
def test_upsert_validation(api_client, headers, key, field, value):
    response = upsert(api_client, headers, key, **{field: value})
    assert response.status_code == 400
    assert field in response.json()["error"]["details"]["fields"]


def test_namespaced_and_global_entities_are_separate(api_client, headers, key):
    global_id = upsert(api_client, headers, key).json()["data"]["id"]
    ns_id = upsert(api_client, headers, key, ns=NS).json()["data"]["id"]
    assert global_id != ns_id
    assert Entity.objects.count() == 2


def test_namespaced_read_excludes_global_unless_asked(api_client, headers, key):
    global_id = upsert(api_client, headers, key).json()["data"]["id"]
    ns_id = upsert(api_client, headers, key, ns=NS, external_id="sku-2").json()["data"]["id"]
    plain = api_client.get(URL + "?application_id=app-1", **headers(key, **NS))
    assert [row["id"] for row in plain.json()["data"]] == [ns_id]
    both = api_client.get(URL + "?application_id=app-1&include_global=true", **headers(key, **NS))
    assert {row["id"] for row in both.json()["data"]} == {global_id, ns_id}


def test_lookup_prefers_the_namespaced_entity(api_client, headers, key):
    upsert(api_client, headers, key)
    ns_id = upsert(api_client, headers, key, ns=NS).json()["data"]["id"]
    query = "/lookup?application_id=app-1&entity_type=product&external_id=sku-1&include_global=true"
    response = api_client.get(URL + query, **headers(key, **NS))
    assert response.json()["data"]["id"] == ns_id
    missing = api_client.get(
        URL + "/lookup?application_id=app-1&entity_type=product&external_id=nope", **headers(key)
    )
    assert missing.status_code == 404


def test_namespaced_write_cannot_touch_a_global_entity(api_client, headers, key):
    global_id = upsert(api_client, headers, key).json()["data"]["id"]
    response = api_client.patch(
        f"{URL}/{global_id}?application_id=app-1",
        {"metadata": {}},
        format="json",
        **headers(key, **NS),
    )
    assert response.status_code == 404


def test_patch_replaces_metadata_and_null_clears_override(api_client, headers, key):
    entity_id = upsert(api_client, headers, key, metadata={"a": 1, "b": 2}, max_images=4).json()[
        "data"
    ]["id"]
    response = api_client.patch(
        f"{URL}/{entity_id}?application_id=app-1",
        {"metadata": {"c": 3}, "max_images": None},
        format="json",
        **headers(key),
    )
    data = response.json()["data"]
    assert data["metadata"] == {"c": 3}
    assert data["media_policy"]["max_images"] is None


def test_deactivate_and_reactivate(api_client, headers, key):
    entity_id = upsert(api_client, headers, key).json()["data"]["id"]
    deleted = api_client.delete(f"{URL}/{entity_id}?application_id=app-1", **headers(key))
    assert deleted.json()["data"]["is_active"] is False
    reactivated = api_client.patch(
        f"{URL}/{entity_id}?application_id=app-1",
        {"is_active": True},
        format="json",
        **headers(key),
    )
    assert reactivated.json()["data"]["is_active"] is True
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"entity.deactivated", "entity.reactivated"} <= actions


def test_application_scoped_grant_cannot_read_another_applications_entity(
    api_client, headers, make_key
):
    writer = make_key({"scopes": ALL}, name="tenant-wide")
    other = upsert(api_client, headers, writer, application_id="app-2").json()["data"]["id"]
    reader = make_key({"application_id": "app-1", "scopes": ALL}, name="app-1-only")
    assert api_client.get(f"{URL}/{other}", **headers(reader)).status_code == 403
    assert (
        api_client.get(f"{URL}/{other}?application_id=app-1", **headers(reader)).status_code == 404
    )


def test_split_unique_constraints():
    Entity.objects.create(tenant_id="t", application_id="a", entity_type="product", external_id="1")
    with pytest.raises(IntegrityError), transaction.atomic():
        Entity.objects.create(
            tenant_id="t", application_id="a", entity_type="product", external_id="1"
        )
    Entity.objects.create(
        tenant_id="t",
        application_id="a",
        namespace_type="merchant",
        namespace_id="m",
        entity_type="product",
        external_id="1",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Entity.objects.create(
            tenant_id="t",
            application_id="a",
            namespace_type="merchant",
            namespace_id="m",
            entity_type="product",
            external_id="1",
        )


def test_namespace_check_constraint_rejects_half_set():
    with pytest.raises(IntegrityError), transaction.atomic():
        Entity.objects.create(
            tenant_id="t",
            application_id="a",
            namespace_type="merchant",
            entity_type="product",
            external_id="1",
        )
