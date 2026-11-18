"""Every operation in `docs/openapi.yaml` answers with exactly the documented shape.

One scenario walks the whole API, validating each response; the last assertion proves no
operation was skipped, so a new endpoint cannot land without a conformance check.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from rest_framework.test import APIClient

from tests.contract import Contract, load_contract
from tests.test_media import PNG

pytestmark = pytest.mark.django_db

ALL_SCOPES = [
    "entities:read",
    "entities:write",
    "reviews:read",
    "reviews:write",
    "reviews:moderate",
    "reviews:attest",
    "media:write",
    "settings:read",
    "settings:write",
    "audit:read",
    "privacy:erase",
]
APP = "application_id=app-1"
MISSING = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
def contract():
    return Contract(load_contract())


@pytest.fixture
def admin(make_key):
    return make_key(
        {"application_id": "app-1", "namespace_wildcard": True, "scopes": ALL_SCOPES},
        name="contract",
    )


def test_every_operation_matches_the_contract(api_client, headers, admin, contract):
    h = headers(admin)
    check = contract.check
    anonymous = APIClient()

    # --- health ---------------------------------------------------------------------------------
    check(anonymous.get("/api/v1/health/live"), "getHealthLive")
    check(anonymous.get("/api/v1/health/ready"), "getHealthReady")

    # --- entities and ratings -------------------------------------------------------------------
    body = {"application_id": "app-1", "entity_type": "product", "external_id": "sku-1"}
    created = check(api_client.put("/api/v1/entities", body, format="json", **h), "upsertEntity")
    assert created.status_code == 201
    entity_id = created.json()["data"]["id"]
    again = api_client.put(
        "/api/v1/entities", {**body, "metadata": {"tier": "gold"}}, format="json", **h
    )
    assert check(again, "upsertEntity").status_code == 200
    check(api_client.get(f"/api/v1/entities?{APP}", **h), "listEntities")
    lookup = f"/api/v1/entities/lookup?{APP}&entity_type=product&external_id=sku-1"
    check(api_client.get(lookup, **h), "lookupEntity")
    check(api_client.get(f"/api/v1/entities/{entity_id}?{APP}", **h), "getEntity")
    patched = api_client.patch(
        f"/api/v1/entities/{entity_id}?{APP}", {"max_images": 3}, format="json", **h
    )
    check(patched, "updateEntity")

    # --- settings -------------------------------------------------------------------------------
    settings_url = "/api/v1/applications/app-1/settings"
    check(api_client.get(settings_url, **h), "getApplicationSettings")
    settings_body = {
        "max_images": None,
        "max_videos": None,
        "max_image_bytes": None,
        "max_video_bytes": None,
        "allowed_content_types": None,
        "moderation_mode": "auto_publish",
        "max_storage_bytes": 10_000_000,
    }
    put = api_client.put(settings_url, settings_body, format="json", **h)
    check(put, "putApplicationSettings")

    # --- reviews --------------------------------------------------------------------------------
    review_body = {
        "application_id": "app-1",
        "entity_id": entity_id,
        "author_id": "u1",
        "rating": 4,
        "title": "Good",
        "body": "Works well",
        "source_ref": "order-1",
        "is_verified_purchase": True,
        "author_metadata": {"display_name": "U"},
    }
    review = check(
        api_client.post("/api/v1/reviews", review_body, format="json", **h), "createReview"
    )
    review = review.json()["data"]
    rid = review["id"]
    duplicate = api_client.post("/api/v1/reviews", review_body, format="json", **h)
    assert check(duplicate, "createReview").json()["error"]["code"] == "review_exists"
    check(api_client.get(f"/api/v1/reviews?{APP}", **h), "listReviews")
    author_path = f"{APP}&author_id=u1&include_unpublished=true"
    check(api_client.get(f"/api/v1/reviews/{rid}?{author_path}", **h), "getReview")
    missing = api_client.get(f"/api/v1/reviews/{MISSING}?{APP}", **h)
    assert check(missing, "getReview").status_code == 404
    edit = api_client.patch(f"/api/v1/reviews/{rid}?{APP}", {"rating": 5}, format="json", **h)
    check(edit, "updateReview")
    check(api_client.get(f"/api/v1/entities/{entity_id}/rating?{APP}", **h), "getEntityRating")
    query = {"application_id": "app-1", "entity_type": "product", "external_ids": ["sku-1", "x"]}
    ratings = api_client.post("/api/v1/entities/ratings/query", query, format="json", **h)
    assert check(ratings, "queryRatings").json()["data"][1]["entity_id"] is None

    # --- media ----------------------------------------------------------------------------------
    media_url = f"/api/v1/reviews/{rid}/media?{APP}"
    intent_body = {
        "kind": "image",
        "content_type": "image/png",
        "size_bytes": len(PNG),
        "client_upload_id": "c-1",
        "width": 10,
        "height": 10,
    }
    intent = check(api_client.post(media_url, intent_body, format="json", **h), "createMediaUpload")
    replay = api_client.post(media_url, intent_body, format="json", **h)
    assert check(replay, "createMediaUpload").status_code == 200
    data = intent.json()["data"]
    mid = data["media"]["id"]
    target = urlsplit(data["upload"]["url"]).path
    uploaded = anonymous.generic("PUT", target, PNG, content_type="image/png")
    assert check(uploaded, "uploadMediaBytes").status_code == 204
    expired = anonymous.generic("PUT", target + "x", PNG, content_type="image/png")
    check(expired, "uploadMediaBytes")
    done = api_client.post(
        f"/api/v1/reviews/{rid}/media/{mid}/complete?{APP}", {}, format="json", **h
    )
    media = check(done, "completeMediaUpload").json()["data"]
    check(api_client.get(media_url, **h), "listReviewMedia")
    file_path = urlsplit(media["url"]).path
    check(anonymous.get(file_path), "downloadMedia")
    assert (
        check(anonymous.get(file_path, HTTP_RANGE="bytes=0-3"), "downloadMedia").status_code == 206
    )
    assert (
        check(anonymous.get(file_path, HTTP_RANGE="bytes=-0"), "downloadMedia").status_code == 416
    )
    current = api_client.get(f"/api/v1/reviews/{rid}?{APP}", **h).json()["data"]
    assert len(current["media"]) == 1
    approve = api_client.post(
        f"/api/v1/reviews/{rid}/media/{mid}/approve?{APP}",
        {
            "expected_version": current["version"],
            "expected_media_version": current["media_version"],
        },
        format="json",
        **h,
    )
    check(approve, "approveMedia")
    reject = api_client.post(
        f"/api/v1/reviews/{rid}/media/{mid}/reject?{APP}", {"reason": "blurry"}, format="json", **h
    )
    check(reject, "rejectMedia")
    deleted = api_client.delete(f"/api/v1/reviews/{rid}/media/{mid}?{APP}", **h)
    check(deleted, "deleteMedia")

    # --- moderation and revisions ---------------------------------------------------------------
    put = api_client.put(
        settings_url, {**settings_body, "moderation_mode": "pre_moderation"}, format="json", **h
    )
    check(put, "putApplicationSettings")
    edit = api_client.patch(f"/api/v1/reviews/{rid}?{APP}", {"rating": 2}, format="json", **h)
    check(edit, "updateReview")
    revision = api_client.get(f"/api/v1/reviews/{rid}/revision?{APP}", **h)
    revision_id = check(revision, "getReviewRevision").json()["data"]["id"]
    current = api_client.get(f"/api/v1/reviews/{rid}?{APP}", **h).json()["data"]
    approved = api_client.post(
        f"/api/v1/reviews/{rid}/revision/approve?{APP}",
        {
            "revision_id": revision_id,
            "expected_version": current["version"],
            "expected_media_version": current["media_version"],
        },
        format="json",
        **h,
    )
    check(approved, "approveReviewRevision")
    api_client.patch(f"/api/v1/reviews/{rid}?{APP}", {"rating": 1}, format="json", **h)
    revision_id = api_client.get(f"/api/v1/reviews/{rid}/revision?{APP}", **h).json()["data"]["id"]
    rejected = api_client.post(
        f"/api/v1/reviews/{rid}/revision/reject?{APP}",
        {"revision_id": revision_id},
        format="json",
        **h,
    )
    check(rejected, "rejectReviewRevision")
    hidden = api_client.post(
        f"/api/v1/reviews/{rid}/hide?{APP}", {"reason": "spam"}, format="json", **h
    )
    hidden = check(hidden, "hideReview").json()["data"]
    published = api_client.post(
        f"/api/v1/reviews/{rid}/publish?{APP}",
        {"expected_version": hidden["version"], "expected_media_version": hidden["media_version"]},
        format="json",
        **h,
    )
    check(published, "publishReview")
    stale = api_client.post(
        f"/api/v1/reviews/{rid}/publish?{APP}",
        {"expected_version": 1, "expected_media_version": 1},
        format="json",
        **h,
    )
    assert check(stale, "publishReview").json()["error"]["code"] in {
        "review_changed",
        "invalid_transition",
    }
    pending = api_client.post(
        "/api/v1/reviews",
        {**review_body, "author_id": "u2", "source_ref": "order-2"},
        format="json",
        **h,
    ).json()["data"]
    assert pending["status"] == "pending"
    check(
        api_client.post(f"/api/v1/reviews/{pending['id']}/reject?{APP}", {}, format="json", **h),
        "rejectReview",
    )
    check(api_client.delete(f"/api/v1/reviews/{pending['id']}?{APP}", **h), "removeReview")

    # --- privacy, audit, deactivation -----------------------------------------------------------
    erased = api_client.post(
        "/api/v1/authors/erase", {"application_id": "app-1", "author_id": "u1"}, format="json", **h
    )
    check(erased, "eraseAuthor")
    check(api_client.get(f"/api/v1/audit-logs?{APP}", **h), "listAuditLogs")
    check(api_client.delete(f"/api/v1/entities/{entity_id}?{APP}", **h), "deactivateEntity")
    unauthenticated = APIClient().get(f"/api/v1/reviews?{APP}", HTTP_X_TENANT_ID="tenant-a")
    assert check(unauthenticated, "listReviews").status_code == 401

    assert set(contract.operations) - contract.exercised == set()


def _broken(mutate) -> Contract:
    document = load_contract()
    mutate(document["components"]["schemas"])
    return Contract(document)


def test_an_undocumented_response_field_fails(api_client):
    contract = _broken(lambda schemas: schemas["HealthLive"]["properties"].pop("status"))
    with pytest.raises(AssertionError, match="Additional properties"):
        contract.check(api_client.get("/api/v1/health/live"), "getHealthLive")


def test_a_missing_required_response_field_fails(api_client):
    def require_extra(schemas):
        schemas["HealthLive"]["required"].append("uptime")
        schemas["HealthLive"]["properties"]["uptime"] = {"type": "integer"}

    with pytest.raises(AssertionError, match="'uptime' is a required property"):
        _broken(require_extra).check(api_client.get("/api/v1/health/live"), "getHealthLive")


def test_an_undocumented_success_status_fails(api_client):
    contract = Contract(load_contract())
    contract.operations["getHealthLive"][2]["responses"].pop("200")
    with pytest.raises(AssertionError, match="not in the contract"):
        contract.check(api_client.get("/api/v1/health/live"), "getHealthLive")


def test_operations_marked_idempotent_answer_the_same_twice(api_client, headers, admin):
    """The SDKs auto-retry `idempotent` operations; a repeat after a lost response succeeds."""

    from octoreview.openapi.contract import SURFACE, load_yaml

    h = headers(admin)
    body = {"application_id": "app-1", "entity_type": "product", "external_id": "sku-1"}
    entity_id = api_client.put("/api/v1/entities", body, format="json", **h).json()["data"]["id"]
    review = api_client.post(
        "/api/v1/reviews",
        {"application_id": "app-1", "entity_id": entity_id, "author_id": "u1", "rating": 5},
        format="json",
        **h,
    ).json()["data"]
    rid = review["id"]
    media_base = f"/api/v1/reviews/{rid}/media"
    intent = api_client.post(
        f"{media_base}?{APP}",
        {"kind": "image", "content_type": "image/png", "size_bytes": len(PNG)},
        format="json",
        **h,
    ).json()["data"]
    mid = intent["media"]["id"]
    APIClient().generic(
        "PUT", urlsplit(intent["upload"]["url"]).path, PNG, content_type="image/png"
    )
    settings_body = {
        "max_images": None,
        "max_videos": None,
        "max_image_bytes": None,
        "max_video_bytes": None,
        "allowed_content_types": None,
        "moderation_mode": None,
        "max_storage_bytes": None,
    }
    calls = {
        "upsertEntity": lambda: api_client.put("/api/v1/entities", body, format="json", **h),
        "updateEntity": lambda: api_client.patch(
            f"/api/v1/entities/{entity_id}?{APP}", {"max_images": 2}, format="json", **h
        ),
        "putApplicationSettings": lambda: api_client.put(
            "/api/v1/applications/app-1/settings", settings_body, format="json", **h
        ),
        "completeMediaUpload": lambda: api_client.post(
            f"{media_base}/{mid}/complete?{APP}", {}, format="json", **h
        ),
        "rejectMedia": lambda: api_client.post(
            f"{media_base}/{mid}/reject?{APP}", {}, format="json", **h
        ),
        "deleteMedia": lambda: api_client.delete(f"{media_base}/{mid}?{APP}", **h),
        "eraseAuthor": lambda: api_client.post(
            "/api/v1/authors/erase",
            {"application_id": "app-1", "author_id": "u1"},
            format="json",
            **h,
        ),
        "deactivateEntity": lambda: api_client.delete(f"/api/v1/entities/{entity_id}?{APP}", **h),
    }
    surface = load_yaml(SURFACE)
    marked = {
        entry["operation"] for entry in surface["operations"] if entry.get("retry") == "idempotent"
    }
    assert marked == set(calls), "every idempotent operation needs a repeat check here"
    for operation_id, call in calls.items():
        first, second = call(), call()
        assert first.status_code in (200, 201), (operation_id, first.json())
        assert second.status_code == 200, (operation_id, second.json())
