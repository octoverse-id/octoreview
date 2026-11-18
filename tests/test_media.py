"""Review media on the local backend (docs/spec.md sections 5.2, 6, 7)."""

from __future__ import annotations

import io
import json
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from octoreview.core.db import holding_locks
from octoreview.entities.models import ApplicationSettings, Entity
from octoreview.events.models import OutboxEvent
from octoreview.lifecycle.media import cleanup_media
from octoreview.media import files, signatures, tokens
from octoreview.media.models import MediaStatus, ReviewMedia
from octoreview.media.storage import StorageError
from octoreview.media.storage.local import LocalBackend
from octoreview.reviews.models import Review

pytestmark = pytest.mark.django_db

SCOPES = [
    "entities:read",
    "entities:write",
    "reviews:read",
    "reviews:write",
    "reviews:moderate",
    "media:write",
    "settings:read",
    "settings:write",
    "privacy:erase",
]
APP = "application_id=app-1"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(56))
JPEG = b"\xff\xd8\xff\xe0" + bytes(60)


@pytest.fixture
def admin(make_key):
    return make_key(
        {"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES}, name="admin"
    )


@pytest.fixture
def uploader(make_key):
    return make_key(
        {"application_id": "app-1", "scopes": ["reviews:read", "reviews:write", "media:write"]},
        name="uploader",
    )


@pytest.fixture
def reader(make_key):
    return make_key({"application_id": "app-1", "scopes": ["reviews:read"]}, name="reader")


def create_review(api_client, headers, token, **body):
    payload = {
        "application_id": "app-1",
        "entity_type": "product",
        "external_id": "sku-1",
        "author_id": "u1",
        "rating": 5,
    }
    payload.update(body)
    response = api_client.post("/api/v1/reviews", payload, format="json", **headers(token))
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def intent(api_client, headers, token, review_id, **body):
    payload = {"kind": "image", "content_type": "image/png", "size_bytes": len(PNG)}
    payload.update(body)
    return api_client.post(
        f"/api/v1/reviews/{review_id}/media?{APP}", payload, format="json", **headers(token)
    )


def put(client, url, body: bytes, **extra):
    return client.generic("PUT", urlsplit(url).path, body, content_type="image/png", **extra)


def complete(api_client, headers, token, review_id, media_id):
    return api_client.post(
        f"/api/v1/reviews/{review_id}/media/{media_id}/complete?{APP}",
        {},
        format="json",
        **headers(token),
    )


def upload(api_client, headers, token, review_id, body=PNG, **intent_body):
    """Intent, PUT and complete; returns the complete response."""

    created = intent(api_client, headers, token, review_id, **intent_body)
    assert created.status_code == 201, created.json()
    data = created.json()["data"]
    assert put(APIClient(), data["upload"]["url"], body).status_code == 204
    return complete(api_client, headers, token, review_id, data["media"]["id"])


def get_review(api_client, headers, token, review_id, query=""):
    response = api_client.get(f"/api/v1/reviews/{review_id}?{APP}{query}", **headers(token))
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def path_of(key: str) -> Path:
    return Path(settings.MEDIA_ROOT) / key


def event_types():
    return list(
        OutboxEvent.objects.order_by("created_at", "id").values_list("event_type", flat=True)
    )


def set_settings(**values):
    ApplicationSettings.objects.update_or_create(
        tenant_id="tenant-a", application_id="app-1", defaults=values
    )


# --- intent -------------------------------------------------------------------------------------


def test_upload_happy_path(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    created = intent(api_client, headers, admin, review["id"])
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["media"]["status"] == "pending"
    assert data["media"]["url"] is None
    assert data["upload"]["method"] == "PUT"
    assert "/api/v1/media-uploads/" in data["upload"]["url"]
    assert data["upload"]["headers"] == {"Content-Type": "image/png"}

    assert put(APIClient(), data["upload"]["url"], PNG).status_code == 204
    done = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert done.status_code == 200, done.json()
    media = done.json()["data"]
    assert media["status"] == "ready"
    assert media["moderation_status"] == "none"
    assert "/api/v1/media-files/" in media["url"]

    row = ReviewMedia.objects.get(pk=media["id"])
    assert path_of(row.storage_key).read_bytes() == PNG
    assert not path_of(row.staging_key).exists()
    assert row.storage_key.endswith(f"{row.id}.png")

    detail = get_review(api_client, headers, admin, review["id"])
    assert detail["media_version"] > review["media_version"]
    assert [item["id"] for item in detail["media"]] == [media["id"]]
    assert Review.objects.get(pk=review["id"]).ready_media_count == 1
    assert "review_media.ready" in event_types()

    download = APIClient().get(urlsplit(media["url"]).path)
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == PNG
    assert download["Content-Type"] == "image/png"
    assert download["X-Content-Type-Options"] == "nosniff"
    assert download["Content-Security-Policy"] == "sandbox; default-src 'none'"
    assert download["Cross-Origin-Resource-Policy"] == "cross-origin"


def test_complete_is_idempotent(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    first = upload(api_client, headers, admin, review["id"])
    media_id = first.json()["data"]["id"]
    again = complete(api_client, headers, admin, review["id"], media_id)
    assert again.status_code == 200
    assert again.json()["data"]["status"] == "ready"
    assert event_types().count("review_media.ready") == 1


def test_entity_limit_allows_two_images_and_rejects_the_third(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    Entity.objects.filter(external_id="sku-1").update(max_images=2)
    for _ in range(2):
        assert intent(api_client, headers, admin, review["id"]).status_code == 201
    third = intent(api_client, headers, admin, review["id"])
    assert third.status_code == 409
    error = third.json()["error"]
    assert error["code"] == "media_limit_exceeded"
    assert error["details"]["limit"] == 2
    assert error["details"]["pending_count"] == 2
    assert error["details"]["policy_source"] == "entity"
    assert error["details"]["next_slot_frees_at"]


def test_videos_have_their_own_limit(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    Entity.objects.filter(external_id="sku-1").update(max_images=0)
    assert intent(api_client, headers, admin, review["id"]).status_code == 409
    video = intent(api_client, headers, admin, review["id"], kind="video", content_type="video/mp4")
    assert video.status_code == 201


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"content_type": "image/gif"}, 400, "content_type_not_allowed"),
        ({"kind": "video"}, 400, "validation_error"),
        ({"size_bytes": 10_485_761}, 400, "media_too_large"),
        ({"size_bytes": 0}, 400, "validation_error"),
    ],
)
def test_intent_validation(api_client, headers, admin, body, status, code):
    review = create_review(api_client, headers, admin)
    response = intent(api_client, headers, admin, review["id"], **body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_application_allow_list_narrows_types(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    set_settings(allowed_content_types=["image/jpeg"])
    response = intent(api_client, headers, admin, review["id"])
    assert response.status_code == 400
    assert response.json()["error"]["details"]["allowed_content_types"] == ["image/jpeg"]


def test_intent_needs_media_write(api_client, headers, admin, reader):
    review = create_review(api_client, headers, admin)
    response = intent(api_client, headers, reader, review["id"])
    assert response.status_code == 403


def test_intent_on_removed_review(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    api_client.delete(f"/api/v1/reviews/{review['id']}?{APP}", **headers(admin))
    response = intent(api_client, headers, admin, review["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "review_not_accepting_media"


def test_intent_retry_replays_the_row_with_a_fresh_url(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    first = intent(api_client, headers, admin, review["id"], client_upload_id="up-1")
    retry = intent(api_client, headers, admin, review["id"], client_upload_id="up-1")
    assert first.status_code == 201
    assert retry.status_code == 200
    first_data, retry_data = first.json()["data"], retry.json()["data"]
    assert retry_data["media"]["id"] == first_data["media"]["id"]
    # The lost first response carried the only URL; the replay must be usable.
    assert put(APIClient(), retry_data["upload"]["url"], PNG).status_code == 204
    done = complete(api_client, headers, admin, review["id"], retry_data["media"]["id"])
    assert done.status_code == 200
    after = intent(api_client, headers, admin, review["id"], client_upload_id="up-1")
    assert after.status_code == 200
    assert after.json()["data"]["upload"] is None
    assert ReviewMedia.objects.count() == 1


# --- complete -----------------------------------------------------------------------------------


def test_type_mismatch_marks_the_row_invalid(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    response = upload(api_client, headers, admin, review["id"], body=JPEG)
    assert response.status_code == 422
    error = response.json()["error"]
    assert (error["code"], error["details"]["reason"]) == ("media_rejected", "type_mismatch")
    row = ReviewMedia.objects.get()
    assert (row.status, row.invalid_reason) == (MediaStatus.INVALID, "type_mismatch")
    assert not path_of(row.staging_key).exists()
    assert "review_media.invalid" in event_types()
    # Invalid rows free their slot.
    Entity.objects.filter(external_id="sku-1").update(max_images=1)
    assert intent(api_client, headers, admin, review["id"]).status_code == 201


def test_size_mismatch_is_rejected(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    response = upload(api_client, headers, admin, review["id"], body=PNG[:-1])
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "size_mismatch"


def test_complete_without_put_releases_the_lease(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    media_id = intent(api_client, headers, admin, review["id"]).json()["data"]["media"]["id"]
    for _ in range(2):
        response = complete(api_client, headers, admin, review["id"], media_id)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "upload_not_found"
    assert ReviewMedia.objects.get().completion_lease_until is None


def test_complete_after_the_grace_is_expired(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    data = intent(api_client, headers, admin, review["id"]).json()["data"]
    put(APIClient(), data["upload"]["url"], PNG)
    past = timezone.now() - timedelta(seconds=settings.UPLOAD_GRACE_SECONDS + 1)
    ReviewMedia.objects.update(upload_expires_at=past)
    response = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "upload_expired"


def test_complete_within_the_grace_succeeds(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    data = intent(api_client, headers, admin, review["id"]).json()["data"]
    put(APIClient(), data["upload"]["url"], PNG)
    ReviewMedia.objects.update(upload_expires_at=timezone.now() - timedelta(seconds=60))
    response = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert response.status_code == 200


def test_live_lease_answers_complete_in_progress(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    data = intent(api_client, headers, admin, review["id"]).json()["data"]
    ReviewMedia.objects.update(completion_lease_until=timezone.now() + timedelta(seconds=30))
    response = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "complete_in_progress"
    assert int(response["Retry-After"]) >= 1


def test_ready_limit_is_rechecked_at_complete(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    first = intent(api_client, headers, admin, review["id"]).json()["data"]
    second = intent(api_client, headers, admin, review["id"]).json()["data"]
    for data in (first, second):
        put(APIClient(), data["upload"]["url"], PNG)
    Entity.objects.filter(external_id="sku-1").update(max_images=1)
    ok = complete(api_client, headers, admin, review["id"], first["media"]["id"])
    over = complete(api_client, headers, admin, review["id"], second["media"]["id"])
    assert ok.status_code == 200
    assert over.status_code == 422
    assert over.json()["error"]["details"]["reason"] == "over_limit"


# --- local upload endpoint ----------------------------------------------------------------------


def test_upload_token_errors(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    data = intent(api_client, headers, admin, review["id"]).json()["data"]
    url = data["upload"]["url"]
    client = APIClient(enforce_csrf_checks=True)

    tampered = url[:-2] + ("AA" if not url.endswith("AA") else "BB")
    response = put(client, tampered, PNG)
    assert (response.status_code, response.json()["error"]["code"]) == (
        403,
        "upload_token_invalid",
    )

    row = ReviewMedia.objects.get()
    expired = tokens.upload_token(
        media_id=row.id,
        key=row.staging_key,
        size_bytes=row.size_bytes,
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    response = client.generic("PUT", f"/api/v1/media-uploads/{expired}", PNG)
    assert (response.status_code, response.json()["error"]["code"]) == (
        410,
        "upload_token_expired",
    )

    download = tokens.download_token(
        media_id=row.id,
        key=row.staging_key,
        audience="moderator",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    response = client.generic("PUT", f"/api/v1/media-uploads/{download}", PNG)
    assert response.status_code == 403  # a download token never authorizes an upload

    response = put(client, url, PNG, CONTENT_LENGTH="")
    assert (response.status_code, response.json()["error"]["code"]) == (
        411,
        "content_length_required",
    )

    response = put(client, url, PNG + b"x")
    assert (response.status_code, response.json()["error"]["code"]) == (413, "upload_too_large")
    assert not path_of(row.staging_key).exists()

    assert client.get(urlsplit(url).path).status_code == 405

    # CSRF never applies: the token is the credential.
    assert put(client, url, PNG).status_code == 204
    complete(api_client, headers, admin, review["id"], row.id)
    response = put(client, url, PNG)
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "upload_already_completed",
    )


def test_re_put_replaces_the_staged_file(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    data = intent(api_client, headers, admin, review["id"]).json()["data"]
    assert put(APIClient(), data["upload"]["url"], JPEG).status_code == 204
    assert put(APIClient(), data["upload"]["url"], PNG).status_code == 204
    response = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert response.status_code == 200


def test_cors_preflight_on_token_paths_only(api_client):
    preflight = {
        "HTTP_ORIGIN": "https://shop.example",
        "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "PUT",
    }
    response = api_client.options("/api/v1/media-uploads/x.y.z", **preflight)
    assert response["Access-Control-Allow-Origin"] == "https://shop.example"
    response = api_client.options("/api/v1/reviews", **preflight)
    assert "Access-Control-Allow-Origin" not in response


# --- download endpoint --------------------------------------------------------------------------


@pytest.fixture
def ready_media(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    media = upload(api_client, headers, admin, review["id"]).json()["data"]
    return review, media


@pytest.mark.parametrize(
    ("header", "status", "body", "content_range"),
    [
        ("bytes=0-3", 206, PNG[0:4], f"bytes 0-3/{len(PNG)}"),
        ("bytes=60-", 206, PNG[60:], f"bytes 60-63/{len(PNG)}"),
        ("bytes=-4", 206, PNG[-4:], f"bytes 60-63/{len(PNG)}"),
        ("bytes=10-1000", 206, PNG[10:], f"bytes 10-63/{len(PNG)}"),
        ("bytes=0-1,4-5", 200, PNG, None),
        ("bytes=-0", 416, None, f"bytes */{len(PNG)}"),
        ("bytes=5-2", 416, None, f"bytes */{len(PNG)}"),
        ("bytes=64-", 416, None, f"bytes */{len(PNG)}"),
        ("items=0-1", 416, None, f"bytes */{len(PNG)}"),
    ],
)
def test_download_ranges(ready_media, header, status, body, content_range):
    _review, media = ready_media
    response = APIClient().get(urlsplit(media["url"]).path, HTTP_RANGE=header)
    assert response.status_code == status
    if body is not None:
        assert b"".join(response.streaming_content) == body
        assert response["Content-Length"] == str(len(body))
    if content_range:
        assert response["Content-Range"] == content_range
    if status == 416:
        assert response.json()["error"]["code"] == "range_not_satisfiable"


def test_download_token_errors(ready_media):
    _review, media = ready_media
    path = urlsplit(media["url"]).path
    response = APIClient().get(path[:-2] + "zz")
    assert (response.status_code, response.json()["error"]["code"]) == (
        403,
        "media_token_invalid",
    )
    row = ReviewMedia.objects.get()
    expired = tokens.download_token(
        media_id=row.id,
        key=row.storage_key,
        audience="public",
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    response = APIClient().get(f"/api/v1/media-files/{expired}")
    assert (response.status_code, response.json()["error"]["code"]) == (
        410,
        "media_token_expired",
    )


def test_token_signed_with_the_previous_secret_still_verifies(settings):
    now = timezone.now()
    token = tokens.download_token(
        media_id="m", key="k", audience="public", expires_at=now + timedelta(minutes=1)
    )
    settings.MEDIA_TOKEN_SECRET_PREVIOUS = settings.MEDIA_TOKEN_SECRET
    settings.MEDIA_TOKEN_SECRET = "rotated-secret"
    rotated = token.replace("c.", "p.", 1)
    assert tokens.verify(rotated, tokens.DOWNLOAD, now=now).key == "k"
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(token, tokens.DOWNLOAD, now=now)
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(rotated, tokens.UPLOAD, now=now)


def test_public_url_is_revoked_when_the_review_is_hidden(api_client, headers, admin, reader):
    review, _media = ready_media_for(api_client, headers, admin)
    public = get_review(api_client, headers, reader, review["id"])["media"][0]["url"]
    moderator = get_review(api_client, headers, admin, review["id"])["media"][0]["url"]
    assert APIClient().get(urlsplit(public).path).status_code == 200
    hidden = api_client.post(
        f"/api/v1/reviews/{review['id']}/hide?{APP}", {}, format="json", **headers(admin)
    )
    assert hidden.status_code == 200
    response = APIClient().get(urlsplit(public).path)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")
    assert APIClient().get(urlsplit(moderator).path).status_code == 200
    assert get_review(api_client, headers, reader, review["id"])["media"][0]["url"] is None


def ready_media_for(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    media = upload(api_client, headers, admin, review["id"]).json()["data"]
    return review, media


# --- visibility and moderation ------------------------------------------------------------------


def test_pre_moderated_media_visibility(api_client, headers, admin, reader):
    review = create_review(api_client, headers, admin)
    set_settings(moderation_mode="pre_moderation")
    media = upload(api_client, headers, admin, review["id"]).json()["data"]
    assert media["moderation_status"] == "pending"
    author = "&author_id=u1&include_unpublished=true"

    assert get_review(api_client, headers, reader, review["id"])["media"][0]["url"] is None
    assert get_review(api_client, headers, reader, review["id"], author)["media"][0]["url"]
    other = "&author_id=u2&include_unpublished=true"
    assert get_review(api_client, headers, reader, review["id"], other)["media"][0]["url"] is None
    # Pending media is not counted as publicly visible.
    assert Review.objects.get().ready_media_count == 0

    queue = api_client.get(
        f"/api/v1/reviews?{APP}&media_moderation_status=pending", **headers(reader)
    )
    assert queue.status_code == 403
    queue = api_client.get(
        f"/api/v1/reviews?{APP}&media_moderation_status=pending", **headers(admin)
    )
    assert [item["id"] for item in queue.json()["data"]] == [review["id"]]

    current = get_review(api_client, headers, admin, review["id"])
    missing = api_client.post(
        f"/api/v1/reviews/{review['id']}/media/{media['id']}/approve?{APP}",
        {},
        format="json",
        **headers(admin),
    )
    assert missing.status_code == 400
    stale = api_client.post(
        f"/api/v1/reviews/{review['id']}/media/{media['id']}/approve?{APP}",
        {"expected_version": current["version"], "expected_media_version": 1},
        format="json",
        **headers(admin),
    )
    assert stale.json()["error"]["code"] == "review_changed"
    approved = api_client.post(
        f"/api/v1/reviews/{review['id']}/media/{media['id']}/approve?{APP}",
        {
            "expected_version": current["version"],
            "expected_media_version": current["media_version"],
        },
        format="json",
        **headers(admin),
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["moderation_status"] == "approved"
    assert get_review(api_client, headers, reader, review["id"])["media"][0]["url"]
    assert Review.objects.get().ready_media_count == 1


def test_rejected_media_is_hidden_from_the_author(api_client, headers, admin, reader):
    review, media = ready_media_for(api_client, headers, admin)
    rejected = api_client.post(
        f"/api/v1/reviews/{review['id']}/media/{media['id']}/reject?{APP}",
        {"reason": "blurry"},
        format="json",
        **headers(admin),
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["moderation_status"] == "rejected"
    author = "&author_id=u1&include_unpublished=true"
    assert get_review(api_client, headers, reader, review["id"], author)["media"][0]["url"] is None
    assert get_review(api_client, headers, admin, review["id"])["media"][0]["url"]
    assert "review_media.moderated" in event_types()


def test_moderation_needs_the_moderate_scope(api_client, headers, admin, uploader):
    review, media = ready_media_for(api_client, headers, admin)
    response = api_client.post(
        f"/api/v1/reviews/{review['id']}/media/{media['id']}/reject?{APP}",
        {},
        format="json",
        **headers(uploader),
    )
    assert response.status_code == 403


def test_embedded_media_is_capped(api_client, headers, admin, settings):
    settings.LIST_MEDIA_LIMIT = 1
    review = create_review(api_client, headers, admin)
    for _ in range(2):
        assert upload(api_client, headers, admin, review["id"]).status_code == 200
    detail = get_review(api_client, headers, admin, review["id"])
    assert (len(detail["media"]), detail["media_truncated"]) == (1, True)
    listed = api_client.get(f"/api/v1/reviews?{APP}", **headers(admin)).json()["data"][0]
    assert (len(listed["media"]), listed["media_truncated"]) == (1, True)
    full = api_client.get(f"/api/v1/reviews/{review['id']}/media?{APP}", **headers(admin))
    assert len(full.json()["data"]) == 2


# --- delete -------------------------------------------------------------------------------------


def test_delete_media(api_client, headers, admin):
    review, media = ready_media_for(api_client, headers, admin)
    row = ReviewMedia.objects.get()
    url = f"/api/v1/reviews/{review['id']}/media/{media['id']}?{APP}"
    response = api_client.delete(url, **headers(admin))
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "deleted"
    assert response.json()["data"]["url"] is None
    assert not path_of(row.storage_key).exists()
    assert Review.objects.get().ready_media_count == 0
    assert "review_media.deleted" in event_types()
    assert api_client.delete(url, **headers(admin)).json()["data"]["status"] == "deleted"
    listed = api_client.get(f"/api/v1/reviews/{review['id']}/media?{APP}", **headers(admin))
    assert listed.json()["data"] == []


def test_delete_on_a_removed_review_is_refused(api_client, headers, admin):
    review, media = ready_media_for(api_client, headers, admin)
    api_client.delete(f"/api/v1/reviews/{review['id']}?{APP}", **headers(admin))
    response = api_client.delete(
        f"/api/v1/reviews/{review['id']}/media/{media['id']}?{APP}", **headers(admin)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"


def test_unknown_media_is_404(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    response = complete(
        api_client, headers, admin, review["id"], "00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404


# --- cleanup and erasure ------------------------------------------------------------------------


def test_cleanup_media(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    abandoned = intent(api_client, headers, admin, review["id"]).json()["data"]
    put(APIClient(), abandoned["upload"]["url"], PNG)
    leased = intent(api_client, headers, admin, review["id"]).json()["data"]
    long_ago = timezone.now() - timedelta(
        seconds=settings.UPLOAD_GRACE_SECONDS + settings.CLEANUP_DELAY_SECONDS + 1
    )
    ReviewMedia.objects.update(upload_expires_at=long_ago)
    ReviewMedia.objects.filter(pk=leased["media"]["id"]).update(
        completion_lease_until=timezone.now() + timedelta(minutes=5)
    )
    orphan = path_of("_staging/tenant-a/app-1/orphan.part-1")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x")
    old = (timezone.now() - timedelta(days=2)).timestamp()
    os.utime(orphan, (old, old))

    out = io.StringIO()
    call_command("cleanup_media", stdout=out)
    summary = json.loads(out.getvalue())
    assert summary["deleted"] == 1
    assert summary["swept"] >= 1
    assert ReviewMedia.objects.get(pk=abandoned["media"]["id"]).status == MediaStatus.DELETED
    assert ReviewMedia.objects.get(pk=leased["media"]["id"]).status == MediaStatus.PENDING
    assert not orphan.exists()
    assert not path_of(ReviewMedia.objects.get(pk=abandoned["media"]["id"]).staging_key).exists()


def test_cleanup_deletes_media_of_removed_reviews(api_client, headers, admin):
    review, _media = ready_media_for(api_client, headers, admin)
    row = ReviewMedia.objects.get()
    api_client.delete(f"/api/v1/reviews/{review['id']}?{APP}", **headers(admin))
    result = cleanup_media()
    assert result.deleted == 1
    assert ReviewMedia.objects.get().status == MediaStatus.DELETED
    assert not path_of(row.storage_key).exists()


def test_cleanup_keeps_hidden_review_media(api_client, headers, admin):
    review, _media = ready_media_for(api_client, headers, admin)
    api_client.post(
        f"/api/v1/reviews/{review['id']}/hide?{APP}", {}, format="json", **headers(admin)
    )
    assert cleanup_media().deleted == 0
    assert ReviewMedia.objects.get().status == MediaStatus.READY


def test_erasure_deletes_media(api_client, headers, admin):
    review, _media = ready_media_for(api_client, headers, admin)
    row = ReviewMedia.objects.get()
    response = api_client.post(
        "/api/v1/authors/erase",
        {"application_id": "app-1", "author_id": "u1"},
        format="json",
        **headers(admin),
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["pending_media_deletes"] == 1
    assert ReviewMedia.objects.get().status == MediaStatus.DELETING
    assert get_review(api_client, headers, admin, review["id"])["media"] == []
    assert cleanup_media().deleted == 1
    assert not path_of(row.storage_key).exists()


# --- quota --------------------------------------------------------------------------------------


def test_storage_quota(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    set_settings(max_storage_bytes=len(PNG) + 10)
    first = intent(api_client, headers, admin, review["id"]).json()["data"]
    usage = api_client.get("/api/v1/applications/app-1/settings", **headers(admin)).json()["data"]
    assert (usage["stored_bytes"], usage["reserved_bytes"]) == (0, len(PNG))

    second = intent(api_client, headers, admin, review["id"])
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "storage_quota_exceeded"
    assert error["details"]["reserved_bytes"] == len(PNG)
    assert error["details"]["requested_bytes"] == len(PNG)

    put(APIClient(), first["upload"]["url"], PNG)
    complete(api_client, headers, admin, review["id"], first["media"]["id"])
    usage = api_client.get("/api/v1/applications/app-1/settings", **headers(admin)).json()["data"]
    assert (usage["stored_bytes"], usage["reserved_bytes"]) == (len(PNG), 0)


# --- storage adapter ----------------------------------------------------------------------------


def test_storage_calls_refuse_to_run_under_a_row_lock():
    with holding_locks(), pytest.raises(RuntimeError):
        files.stat("_staging/anything")


def test_local_backend_refuses_keys_outside_the_root():
    with pytest.raises(StorageError):
        LocalBackend().path_for("../escape")


def test_finalize_refuses_a_changed_file():
    backend = LocalBackend()
    backend.write_stream("_staging/t/a/probe", [PNG], limit=len(PNG))
    version = backend.stat("_staging/t/a/probe").version
    backend.write_stream("_staging/t/a/probe", [JPEG], limit=len(JPEG))
    with pytest.raises(files.PreconditionFailed):
        backend.finalize(
            "_staging/t/a/probe", "media/t/a/probe.png", content_type="", if_version=version
        )
    assert not path_of("media/t/a/probe.png").exists()
    backend.delete("_staging/t/a/probe")


def test_check_storage_round_trip():
    out = io.StringIO()
    call_command("check_storage", stdout=out)
    assert "check_storage: ok" in out.getvalue()
    assert "finalize: ok" in out.getvalue()
    assert not list(path_of("media/_check").glob("*"))


# --- magic bytes --------------------------------------------------------------------------------


def _ebml(doctype: bytes) -> bytes:
    body = b"\x42\x82" + bytes([0x80 | len(doctype)]) + doctype
    return b"\x1a\x45\xdf\xa3" + bytes([0x80 | len(body)]) + body


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00mp41", "video/mp4"),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
        (b"\x00\x00\x00\x08wide\x00\x00\x00\x10mdat", "video/quicktime"),
        (b"\x00\x00\x00\x08freemdat", None),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1", None),
        (_ebml(b"webm"), "video/webm"),
        (_ebml(b"matroska"), None),
        (b"GIF89a", None),
        (b"", None),
    ],
)
def test_signature_detection(head, expected):
    assert signatures.detect(head) == expected
