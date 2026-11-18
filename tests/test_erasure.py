import io
import json

import pytest
from django.apps import apps
from django.core.management import call_command

from octoreview.events.models import OutboxEvent
from octoreview.lifecycle.erasure import erased_author_id
from octoreview.lifecycle.recompute import recompute_ratings
from octoreview.reviews.models import Review

pytestmark = pytest.mark.django_db

SENTINEL = "SENTINEL-PERSONAL-9f3a"
SCOPES = ["reviews:read", "reviews:write", "reviews:moderate", "reviews:attest", "privacy:erase"]


@pytest.fixture
def admin(make_key):
    return make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})


def make_reviews(api_client, headers, token):
    for index in range(3):
        response = api_client.post(
            "/api/v1/reviews",
            {
                "application_id": "app-1",
                "entity_type": "product",
                "external_id": f"sku-{index}",
                "author_id": SENTINEL,
                "rating": 4,
                "title": f"{SENTINEL} title",
                "body": f"{SENTINEL} body",
                "metadata": {"note": SENTINEL},
                "author_metadata": {"display_name": SENTINEL},
                "source_ref": f"{SENTINEL}-order-{index}",
                "is_verified_purchase": True,
            },
            format="json",
            **headers(token, **{"X-Actor-ID": SENTINEL}),
        )
        assert response.status_code == 201


def erase(api_client, headers, token, author=SENTINEL, ns=None):
    return api_client.post(
        "/api/v1/authors/erase",
        {"application_id": "app-1", "author_id": author},
        format="json",
        **headers(token, **(ns or {})),
    )


def sentinel_sweep():
    """No text or JSON column of any table still holds the author's data."""

    leaks = []
    for model in apps.get_models():
        for row in model.objects.all().values():
            for column, value in row.items():
                text = json.dumps(value, default=str) if not isinstance(value, str) else value
                if SENTINEL in text:
                    if model is OutboxEvent and row["event_type"] == "author.erased":
                        continue  # the one documented exception, deleted on delivery
                    leaks.append(f"{model._meta.db_table}.{column}")
    return leaks


def test_erasure_leaves_no_personal_data(api_client, headers, admin):
    make_reviews(api_client, headers, admin)
    response = erase(api_client, headers, admin)
    assert response.status_code == 200
    assert response.json()["data"] == {
        "reviews_redacted": 3,
        "pending_media_deletes": 0,
        "remaining": 0,
    }
    assert sentinel_sweep() == []
    erased = erased_author_id("tenant-a", "app-1", SENTINEL)
    assert set(Review.objects.values_list("author_id", flat=True)) == {erased}
    assert set(Review.objects.values_list("status", flat=True)) == {"removed"}
    assert recompute_ratings(repair=False) == []
    assert OutboxEvent.objects.filter(event_type="author.erased").count() == 1


def test_erasure_is_idempotent(api_client, headers, admin):
    make_reviews(api_client, headers, admin)
    erase(api_client, headers, admin)
    again = erase(api_client, headers, admin)
    assert again.json()["data"]["reviews_redacted"] == 0
    assert again.json()["data"]["remaining"] == 0


def test_erasure_needs_an_application_wide_grant(api_client, headers, make_key):
    token = make_key(
        {
            "application_id": "app-1",
            "namespace_type": "merchant",
            "namespace_id": "m-1",
            "scopes": ["privacy:erase"],
        }
    )
    ns = {"X-Namespace-Type": "merchant", "X-Namespace-ID": "m-1"}
    response = erase(api_client, headers, token, ns=ns)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "application_wide_grant_required"


def test_erase_author_command(api_client, headers, admin):
    make_reviews(api_client, headers, admin)
    out = io.StringIO()
    call_command(
        "erase_author",
        "--tenant",
        "tenant-a",
        "--application",
        "app-1",
        "--author",
        SENTINEL,
        "--dry-run",
        stdout=out,
    )
    assert json.loads(out.getvalue()) == {"reviews_to_redact": 3}
    out = io.StringIO()
    call_command(
        "erase_author",
        "--tenant",
        "tenant-a",
        "--application",
        "app-1",
        "--author",
        SENTINEL,
        "--yes",
        stdout=out,
    )
    assert json.loads(out.getvalue()) == {"reviews_redacted": 3, "remaining": 0}
    assert sentinel_sweep() == []


def test_recompute_ratings_check_and_repair(api_client, headers, admin):
    from octoreview.entities.models import Entity

    make_reviews(api_client, headers, admin)
    Entity.objects.update(rating_count=99)
    with pytest.raises(SystemExit) as exc:
        call_command("recompute_ratings", "--check", stdout=io.StringIO(), stderr=io.StringIO())
    assert exc.value.code == 1
    call_command("recompute_ratings", stdout=io.StringIO())
    assert recompute_ratings(repair=False) == []
