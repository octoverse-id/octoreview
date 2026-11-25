"""ETag, If-None-Match and 304 on review and rating reads (docs/spec.md section 5.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from octoreview.core import etags
from octoreview.entities.ratings import Contribution, apply_delta
from octoreview.media import files, url_cache
from octoreview.media.models import ReviewMedia
from tests.test_media import APP, create_review, set_settings, upload
from tests.test_ratings import make_entity

pytestmark = pytest.mark.django_db

SCOPES = [
    "entities:read",
    "entities:write",
    "reviews:read",
    "reviews:write",
    "reviews:moderate",
    "reviews:attest",
    "media:write",
    "privacy:erase",
]


@pytest.fixture
def admin(make_key):
    return make_key({"application_id": "app-1", "scopes": SCOPES}, name="admin")


@pytest.fixture
def reader(make_key):
    return make_key({"application_id": "app-1", "scopes": ["reviews:read"]}, name="reader")


def detail(api_client, headers, token, review_id, etag=None, query=""):
    extra = {"If-None-Match": etag} if etag else {}
    return api_client.get(f"/api/v1/reviews/{review_id}?{APP}{query}", **headers(token, **extra))


def listing(api_client, headers, token, etag=None, query=""):
    extra = {"If-None-Match": etag} if etag else {}
    return api_client.get(f"/api/v1/reviews?{APP}{query}", **headers(token, **extra))


# --- the validator round trip -------------------------------------------------------------------


@pytest.mark.parametrize("read", [detail, listing], ids=["detail", "list"])
def test_an_unchanged_review_read_answers_304(api_client, headers, admin, read):
    review = create_review(api_client, headers, admin)
    args = (review["id"],) if read is detail else ()
    first = read(api_client, headers, admin, *args)
    assert first.status_code == 200
    assert first["Cache-Control"] == "private, max-age=60"
    tag = first["ETag"]
    assert tag.startswith('"') and tag.endswith('"')

    again = read(api_client, headers, admin, *args, etag=tag)
    assert again.status_code == 304
    assert again.content == b""
    assert again["ETag"] == tag
    assert again["Cache-Control"] == "private, max-age=60"
    assert "X-Tenant-ID" in again["Vary"]


@pytest.mark.parametrize(
    "header",
    ['W/"{tag}"', '"other", "{tag}"', "*"],
    ids=["weak", "list", "star"],
)
def test_if_none_match_forms(api_client, headers, admin, header):
    review = create_review(api_client, headers, admin)
    tag = detail(api_client, headers, admin, review["id"])["ETag"].strip('"')
    sent = header.format(tag=tag)
    assert detail(api_client, headers, admin, review["id"], etag=sent).status_code == 304


def test_another_etag_gets_the_body(api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    response = detail(api_client, headers, admin, review["id"], etag='"stale"')
    assert response.status_code == 200
    assert response.json()["data"]["id"] == review["id"]


def test_a_304_skips_the_media_query_and_signing(api_client, headers, admin, monkeypatch):
    review = create_review(api_client, headers, admin)
    assert upload(api_client, headers, admin, review["id"]).status_code == 200
    tag = detail(api_client, headers, admin, review["id"])["ETag"]

    def no_signing(*args, **kwargs):
        raise AssertionError("a 304 signs no URL")

    monkeypatch.setattr(files, "download_url", no_signing)
    table = ReviewMedia._meta.db_table
    with CaptureQueriesContext(connection) as queries:
        response = detail(api_client, headers, admin, review["id"], etag=tag)
    assert response.status_code == 304
    assert not [q for q in queries.captured_queries if table in q["sql"]]


# --- what changes the ETag ----------------------------------------------------------------------


def _patch(client, headers, token, review, **body):
    response = client.patch(
        f"/api/v1/reviews/{review['id']}?{APP}", body, format="json", **headers(token)
    )
    assert response.status_code == 200, response.json()


def _post(client, headers, token, path, **body):
    response = client.post(f"/api/v1/{path}?{APP}", body, format="json", **headers(token))
    assert response.status_code == 200, response.json()


def edit(client, headers, token, review):
    _patch(client, headers, token, review, title="Changed")


def attest(client, headers, token, review):
    # Attestation does not bump `version` (it never stales a pending revision).
    _patch(client, headers, token, review, is_verified_purchase=True)


def hide(client, headers, token, review):
    # A status transition does not bump `version` either.
    _post(client, headers, token, f"reviews/{review['id']}/hide", reason="spam")


def remove(client, headers, token, review):
    response = client.delete(f"/api/v1/reviews/{review['id']}?{APP}", **headers(token))
    assert response.status_code == 200


def add_media(client, headers, token, review):
    assert upload(client, headers, token, review["id"]).status_code == 200


def _first_media(review):
    return ReviewMedia.objects.filter(review_id=review["id"]).get()


def reject_media(client, headers, token, review):
    media = _first_media(review)
    _post(client, headers, token, f"reviews/{review['id']}/media/{media.id}/reject")


def delete_media(client, headers, token, review):
    media = _first_media(review)
    response = client.delete(
        f"/api/v1/reviews/{review['id']}/media/{media.id}?{APP}", **headers(token)
    )
    assert response.status_code == 200


def erase(client, headers, token, review):
    # Erasure rewrites the text and the author without bumping `version`.
    response = client.post(
        "/api/v1/authors/erase",
        {"application_id": "app-1", "author_id": "u1"},
        format="json",
        **headers(token),
    )
    assert response.status_code == 200, response.json()


WRITES = [edit, attest, hide, remove, add_media, reject_media, delete_media, erase]
NEEDS_MEDIA = {reject_media, delete_media}


@pytest.mark.parametrize("write", WRITES, ids=[w.__name__ for w in WRITES])
@pytest.mark.parametrize("read", [detail, listing], ids=["detail", "list"])
def test_every_review_write_changes_the_etag(api_client, headers, admin, write, read):
    review = create_review(api_client, headers, admin, title="Great", body="Loved it")
    if write in NEEDS_MEDIA:
        add_media(api_client, headers, admin, review)
    # A moderator's list shows every status, so the row stays on the page after hide/remove.
    query = "&include_unpublished=true&author_id=u1" if read is listing else ""
    args = (review["id"],) if read is detail else ()
    before = read(api_client, headers, admin, *args, query=query)["ETag"]

    write(api_client, headers, admin, review)

    after = read(api_client, headers, admin, *args, etag=before, query=query)
    assert after.status_code == 200
    assert after["ETag"] != before


def test_the_list_count_is_in_the_etag(api_client, headers, admin):
    older = create_review(api_client, headers, admin, author_id="u1")
    create_review(api_client, headers, admin, author_id="u2")
    query = "&limit=1"
    before = listing(api_client, headers, admin, query=query)
    assert before.json()["data"][0]["author_id"] == "u2"
    remove(api_client, headers, admin, older)  # off the page; only the count moves
    after = listing(api_client, headers, admin, etag=before["ETag"], query=query)
    assert after.status_code == 200
    assert after.json()["pagination"]["count"] == 1


def test_the_callers_view_is_in_the_etag(api_client, headers, admin, reader):
    review = create_review(api_client, headers, admin)
    moderator = detail(api_client, headers, admin, review["id"])["ETag"]
    public = detail(api_client, headers, reader, review["id"])
    assert public.status_code == 200
    assert public["ETag"] != moderator
    author = detail(
        api_client, headers, reader, review["id"], query="&author_id=u1&include_unpublished=true"
    )
    assert author["ETag"] not in {moderator, public["ETag"]}


def test_a_new_signing_epoch_changes_the_etag(api_client, headers, admin, monkeypatch):
    review = create_review(api_client, headers, admin)
    start = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: start)
    first = detail(api_client, headers, admin, review["id"])["ETag"]
    later = start + timedelta(seconds=etags.epoch_seconds())
    monkeypatch.setattr(timezone, "now", lambda: later)
    assert detail(api_client, headers, admin, review["id"], etag=first).status_code == 200


@pytest.mark.parametrize(
    "setting, value",
    [
        ("MEDIA_PUBLIC_BASE_URL", "https://cdn.example.invalid"),
        ("STORAGE_S3_BUCKET", "moved-bucket"),
        ("STORAGE_S3_PUBLIC_ENDPOINT_URL", "https://s3.example.invalid"),
        ("LIST_MEDIA_LIMIT", 3),
    ],
)
def test_a_read_url_setting_changes_the_etag(api_client, headers, admin, settings, setting, value):
    # A same-version redeploy that moves where URLs point must not 304 the old URLs.
    review = create_review(api_client, headers, admin)
    before = detail(api_client, headers, admin, review["id"])["ETag"]
    setattr(settings, setting, value)
    assert detail(api_client, headers, admin, review["id"], etag=before).status_code == 200


# --- bodies that must not be revalidated ---------------------------------------------------------


def test_a_body_with_an_unsigned_url_has_no_etag(api_client, headers, admin, monkeypatch):
    review = create_review(api_client, headers, admin)
    add_media(api_client, headers, admin, review)
    url_cache.clear()  # the upload response signed and cached a good URL

    def failing(*args, **kwargs):
        raise files.StorageError("signing", "down")

    monkeypatch.setattr(files, "download_url", failing)
    for response in (
        detail(api_client, headers, admin, review["id"]),
        listing(api_client, headers, admin),
    ):
        assert response.status_code == 200
        assert "ETag" not in response
        assert response["Cache-Control"] == "private, max-age=60"


def test_a_url_capped_below_the_floor_has_no_etag(api_client, headers, admin, monkeypatch):
    # Short-lived storage credentials cap the expiry (S3 `url_expiry`).
    review = create_review(api_client, headers, admin)
    add_media(api_client, headers, admin, review)
    monkeypatch.setattr(files, "url_expiry", lambda requested: timezone.now() + timedelta(hours=1))
    assert "ETag" in detail(api_client, headers, admin, review["id"])
    url_cache.clear()  # that read cached a URL that outlives the epoch
    monkeypatch.setattr(
        files, "url_expiry", lambda requested: timezone.now() + timedelta(seconds=10)
    )
    assert "ETag" not in detail(api_client, headers, admin, review["id"])


def test_a_short_ttl_still_signs_past_the_floor(api_client, headers, admin, settings):
    settings.MEDIA_URL_TTL_SECONDS = 60
    review = create_review(api_client, headers, admin)
    add_media(api_client, headers, admin, review)
    response = detail(api_client, headers, admin, review["id"])
    assert "ETag" in response
    media = response.json()["data"]["media"][0]
    expires = datetime.fromisoformat(media["url_expires_at"])
    assert expires >= etags.url_lifetime_floor(timezone.now() - timedelta(seconds=5))


def test_url_expiry_target(settings):
    now = datetime(2026, 11, 25, 8, 3, 7, tzinfo=UTC)
    settings.MEDIA_URL_TTL_SECONDS = 900
    assert etags.url_expiry_target(now) == now + timedelta(seconds=900)
    settings.MEDIA_URL_TTL_SECONDS = 60
    floor = etags.url_lifetime_floor(now)
    assert etags.url_expiry_target(now) == floor > now + timedelta(seconds=60)
    # The floor: the epoch end (30-second epochs) plus max(TTL/2, max-age).
    assert floor == datetime(2026, 11, 25, 8, 3, 30, tzinfo=UTC) + timedelta(seconds=60)


def test_media_a_reader_cannot_see_still_moves_the_etag(api_client, headers, admin, reader):
    # Pending media is invisible to a public reader, but media_version moves: an extra 200,
    # never a stale 304.
    review = create_review(api_client, headers, admin)
    set_settings(moderation_mode="pre_moderation")
    before = detail(api_client, headers, reader, review["id"])["ETag"]
    add_media(api_client, headers, admin, review)
    assert detail(api_client, headers, reader, review["id"], etag=before).status_code == 200


# --- rating summaries ---------------------------------------------------------------------------


def test_the_rating_summary_answers_304_until_it_moves(api_client, headers, make_key):
    entity = make_entity()
    apply_delta(entity, add=Contribution(5, False))
    token = make_key({"application_id": "app-1", "scopes": ["entities:read"]})
    url = f"/api/v1/entities/{entity.id}/rating?{APP}"
    first = api_client.get(url, **headers(token))
    assert first["Cache-Control"] == "private, max-age=60"
    tag = first["ETag"]
    unchanged = api_client.get(url, **headers(token, **{"If-None-Match": tag}))
    assert unchanged.status_code == 304

    entity.refresh_from_db()
    apply_delta(entity, add=Contribution(3, False))
    moved = api_client.get(url, **headers(token, **{"If-None-Match": tag}))
    assert moved.status_code == 200
    assert moved["ETag"] != tag


def test_the_batch_query_sends_no_validators(api_client, headers, make_key):
    make_entity()
    token = make_key({"application_id": "app-1", "scopes": ["entities:read"]})
    response = api_client.post(
        "/api/v1/entities/ratings/query",
        {"application_id": "app-1", "entity_type": "product", "external_ids": ["sku-1"]},
        format="json",
        **headers(token, **{"If-None-Match": "*"}),
    )
    assert response.status_code == 200
    assert "ETag" not in response
