import pytest
from django.test.utils import CaptureQueriesContext

from octoreview.entities.models import Entity
from octoreview.entities.ratings import Contribution, apply_delta, summary

pytestmark = pytest.mark.django_db

NS = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}


def make_entity(external_id="sku-1", **kwargs):
    values = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "entity_type": "product",
        "external_id": external_id,
    }
    values.update(kwargs)
    return Entity.objects.create(**values)


def test_apply_delta_adds_moves_and_removes():
    entity = make_entity()
    assert apply_delta(entity.id, add=Contribution(5, verified=True)) is True
    assert apply_delta(entity.id, add=Contribution(3, verified=False)) is True
    apply_delta(entity.id, remove=Contribution(3, False), add=Contribution(4, False))
    entity.refresh_from_db()
    assert (entity.rating_count, entity.rating_sum) == (2, 9)
    assert (entity.rating_3_count, entity.rating_4_count, entity.rating_5_count) == (0, 1, 1)
    assert entity.verified_review_count == 1
    apply_delta(entity.id, remove=Contribution(5, verified=True))
    entity.refresh_from_db()
    assert (entity.rating_count, entity.rating_sum, entity.verified_review_count) == (1, 4, 0)


def test_apply_delta_noop_when_nothing_changes():
    entity = make_entity()
    assert (
        apply_delta(entity.id, remove=Contribution(4, False), add=Contribution(4, False)) is False
    )
    with pytest.raises(ValueError):
        apply_delta(entity.id, add=Contribution(6, False))


def test_summary_shape():
    entity = make_entity()
    for rating in (5, 4, 4):
        apply_delta(entity.id, add=Contribution(rating, rating == 5))
    entity.refresh_from_db()
    assert summary(entity) == {
        "rating_count": 3,
        "rating_average": 4.33,
        "histogram": {"1": 0, "2": 0, "3": 0, "4": 2, "5": 1},
        "histogram_percent": {"1": 0.0, "2": 0.0, "3": 0.0, "4": 66.7, "5": 33.3},
        "verified_review_count": 1,
    }


def test_summary_of_nothing_is_zeroed():
    data = summary(None)
    assert data["rating_count"] == 0
    assert data["rating_average"] is None
    assert set(data["histogram_percent"].values()) == {0.0}


def test_rating_endpoint_returns_a_json_number(api_client, make_key, headers):
    entity = make_entity()
    apply_delta(entity.id, add=Contribution(5, False))
    apply_delta(entity.id, add=Contribution(4, False))
    token = make_key({"application_id": "app-1", "scopes": ["entities:read"]})
    response = api_client.get(
        f"/api/v1/entities/{entity.id}/rating?application_id=app-1", **headers(token)
    )
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, max-age=60"
    assert b'"rating_average":4.5' in response.content.replace(b" ", b"")


def batch(api_client, headers, token, external_ids, ns=None, query=""):
    return api_client.post(
        "/api/v1/entities/ratings/query" + query,
        {"application_id": "app-1", "entity_type": "product", "external_ids": external_ids},
        format="json",
        **headers(token, **(ns or {})),
    )


@pytest.fixture
def reader(make_key):
    return make_key(
        {"application_id": "app-1", "namespace_wildcard": True, "scopes": ["entities:read"]}
    )


def test_batch_answers_in_request_order_with_duplicates_and_unknowns(api_client, headers, reader):
    known = make_entity("a")
    apply_delta(known.id, add=Contribution(5, False))
    response = batch(api_client, headers, reader, ["a", "missing", "a"])
    data = response.json()["data"]
    assert [row["external_id"] for row in data] == ["a", "missing", "a"]
    assert data[0]["entity_id"] == str(known.id)
    assert data[0]["rating_count"] == 1
    assert data[1] == {
        "external_id": "missing",
        "entity_id": None,
        "namespace_type": None,
        "namespace_id": None,
        **summary(None),
    }
    assert response["Cache-Control"] == "private, max-age=60"


def test_batch_limits(api_client, headers, reader):
    assert batch(api_client, headers, reader, [str(i) for i in range(100)]).status_code == 200
    too_many = batch(api_client, headers, reader, [str(i) for i in range(101)])
    assert too_many.status_code == 400
    assert batch(api_client, headers, reader, []).status_code == 400


def test_batch_query_count_is_constant(api_client, headers, reader):
    from django.db import connection

    for i in range(30):
        make_entity(str(i))
    # Warm up: the first request also records the key's last_used_at.
    batch(api_client, headers, reader, ["1"])
    with CaptureQueriesContext(connection) as small:
        batch(api_client, headers, reader, ["1"])
    with CaptureQueriesContext(connection) as large:
        batch(api_client, headers, reader, [str(i) for i in range(30)])
    assert len(large.captured_queries) == len(small.captured_queries)


def test_batch_prefers_namespaced_and_names_the_match(api_client, headers, reader):
    make_entity("a")
    namespaced = make_entity("a", namespace_type="merchant", namespace_id="m-1")
    response = batch(api_client, headers, reader, ["a"], ns=NS, query="?include_global=true")
    row = response.json()["data"][0]
    assert row["entity_id"] == str(namespaced.id)
    assert (row["namespace_type"], row["namespace_id"]) == ("merchant", "m-1")
