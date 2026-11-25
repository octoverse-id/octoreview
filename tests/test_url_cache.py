"""Signed read URLs cached per signing epoch (docs/spec.md section 8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from octoreview.core import etags
from octoreview.media import files, url_cache
from tests.test_etags import SCOPES, add_media, detail, listing
from tests.test_media import APP, create_review, set_settings

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(make_key):
    return make_key({"application_id": "app-1", "scopes": SCOPES}, name="admin")


@pytest.fixture
def reader(make_key):
    return make_key({"application_id": "app-1", "scopes": ["reviews:read"]}, name="reader")


@pytest.fixture
def signed(monkeypatch):
    """The audiences of every read URL signed from here on."""

    calls: list[str] = []
    real = files.download_url

    def counting(*args, **kwargs):
        calls.append(kwargs["audience"])
        return real(*args, **kwargs)

    monkeypatch.setattr(files, "download_url", counting)
    return calls


@pytest.fixture
def clock(monkeypatch):
    """Pin `timezone.now` 5 seconds into the current epoch, so two reads never straddle an epoch
    by accident and real-time token checks still agree; `clock.at(dt)` moves it."""

    class Clock:
        def at(self, moment):
            monkeypatch.setattr(timezone, "now", lambda: moment)

    length = etags.epoch_seconds()
    real = datetime.now(tz=UTC).timestamp()
    clock = Clock()
    clock.start = datetime.fromtimestamp(real // length * length + 5, tz=UTC)
    clock.at(clock.start)
    return clock


def a_review_with_media(api_client, headers, admin, signed=None, count=1):
    review = create_review(api_client, headers, admin)
    for _ in range(count):
        add_media(api_client, headers, admin, review)
    # The upload responses signed (and cached) their own URLs; count only what follows.
    url_cache.clear()
    if signed is not None:
        signed.clear()
    return review


def urls(response):
    return [m["url"] for m in response.json()["data"]["media"]]


def test_a_second_read_in_the_epoch_signs_nothing(api_client, headers, admin, signed, clock):
    review = a_review_with_media(api_client, headers, admin, signed, count=2)
    first = detail(api_client, headers, admin, review["id"])
    assert signed == ["moderator", "moderator"]
    clock.at(clock.start + timedelta(seconds=etags.epoch_seconds() - 10))  # still the same epoch
    again = detail(api_client, headers, admin, review["id"])
    listed = listing(api_client, headers, admin)
    assert len(signed) == 2
    assert urls(again) == urls(first)
    assert [m["url"] for m in listed.json()["data"][0]["media"]] == urls(first)


def test_a_new_epoch_signs_again(api_client, headers, admin, signed, clock):
    review = a_review_with_media(api_client, headers, admin, signed)
    detail(api_client, headers, admin, review["id"])
    clock.at(clock.start + timedelta(seconds=etags.epoch_seconds()))
    detail(api_client, headers, admin, review["id"])
    assert len(signed) == 2


def test_each_audience_has_its_own_url(api_client, headers, admin, reader, signed, clock):
    # The first item is public; the second waits for moderation, so only its author and a
    # moderator get a URL (publicly visible media is `public` even on the author path).
    review = create_review(api_client, headers, admin)
    add_media(api_client, headers, admin, review)
    set_settings(moderation_mode="pre_moderation")
    add_media(api_client, headers, admin, review)
    url_cache.clear()
    signed.clear()
    author_path = "&author_id=u1&include_unpublished=true"

    def by_status(response):  # the pinned clock gives both items one created_at: no order
        return {m["moderation_status"]: m["url"] for m in response.json()["data"]["media"]}

    moderator = by_status(detail(api_client, headers, admin, review["id"]))
    public = by_status(detail(api_client, headers, reader, review["id"]))
    author = by_status(detail(api_client, headers, reader, review["id"], query=author_path))
    assert signed == ["moderator", "moderator", "public", "author"]
    assert public["pending"] is None
    assert author["none"] == public["none"]  # the same public entry, from the cache
    urls_signed = {moderator["none"], moderator["pending"], public["none"], author["pending"]}
    assert len(urls_signed) == 4

    for token, query in ((admin, ""), (reader, ""), (reader, author_path)):
        detail(api_client, headers, token, review["id"], query=query)
    assert len(signed) == 4


def test_a_visibility_change_is_never_served_a_cached_url(
    api_client, headers, admin, reader, clock
):
    review = a_review_with_media(api_client, headers, admin)
    assert urls(detail(api_client, headers, reader, review["id"]))[0] is not None
    media_id = detail(api_client, headers, admin, review["id"]).json()["data"]["media"][0]["id"]
    rejected = api_client.post(
        f"/api/v1/reviews/{review['id']}/media/{media_id}/reject?{APP}",
        {},
        format="json",
        **headers(admin),
    )
    assert rejected.status_code == 200
    # The public URL is still cached for this epoch, but the reader's audience is now none.
    assert urls(detail(api_client, headers, reader, review["id"])) == [None]


def test_another_deployment_signs_again(api_client, headers, admin, signed, clock, settings):
    review = a_review_with_media(api_client, headers, admin, signed)
    detail(api_client, headers, admin, review["id"])
    settings.STORAGE_S3_BUCKET = "moved-bucket"
    detail(api_client, headers, admin, review["id"])
    assert len(signed) == 2


def test_a_signing_failure_is_not_cached(api_client, headers, admin, monkeypatch, clock):
    review = a_review_with_media(api_client, headers, admin)
    real = files.download_url

    def failing(*args, **kwargs):
        raise files.StorageError("signing", "down")

    monkeypatch.setattr(files, "download_url", failing)
    media = detail(api_client, headers, admin, review["id"]).json()["data"]["media"][0]
    assert media["url_unavailable"] == "signing"
    monkeypatch.setattr(files, "download_url", real)
    assert urls(detail(api_client, headers, admin, review["id"]))[0] is not None


def test_a_capped_url_is_signed_per_request(api_client, headers, admin, signed, monkeypatch, clock):
    # Short-lived storage credentials cap the expiry below the floor: reuse would outlive it.
    review = a_review_with_media(api_client, headers, admin, signed)
    monkeypatch.setattr(files, "url_expiry", lambda requested: clock.start + timedelta(seconds=10))
    detail(api_client, headers, admin, review["id"])
    detail(api_client, headers, admin, review["id"])
    assert len(signed) == 2
    assert url_cache.size() == 0


def test_size_zero_signs_per_request(api_client, headers, admin, signed, clock, settings):
    settings.MEDIA_URL_CACHE_SIZE = 0
    review = a_review_with_media(api_client, headers, admin, signed)
    detail(api_client, headers, admin, review["id"])
    detail(api_client, headers, admin, review["id"])
    assert len(signed) == 2
    assert url_cache.size() == 0


def test_public_cdn_urls_are_neither_signed_nor_cached(
    api_client, headers, admin, reader, signed, clock, settings
):
    settings.MEDIA_PUBLIC_BASE_URL = "https://cdn.example.invalid"
    review = a_review_with_media(api_client, headers, admin, signed)
    url = urls(detail(api_client, headers, reader, review["id"]))[0]
    assert url.startswith("https://cdn.example.invalid/")
    assert signed == []
    assert url_cache.size() == 0


# --- the LRU itself ------------------------------------------------------------------------------


def test_the_cache_is_a_bounded_lru(settings):
    settings.MEDIA_URL_CACHE_SIZE = 2
    url_cache.put(("a",), {"url": "A"})
    url_cache.put(("b",), {"url": "B"})
    assert url_cache.get(("a",)) == {"url": "A"}  # a is now the most recent
    url_cache.put(("c",), {"url": "C"})
    assert url_cache.size() == 2
    assert url_cache.get(("b",)) is None
    assert url_cache.get(("a",)) == {"url": "A"}
    assert url_cache.get(("c",)) == {"url": "C"}


def test_the_cache_holds_copies(settings):
    stored = {"url": "A"}
    url_cache.put(("a",), stored)
    stored["url"] = "changed by the signer"
    first = url_cache.get(("a",))
    first["url"] = "changed by a caller"
    assert url_cache.get(("a",)) == {"url": "A"}


def test_size_zero_at_runtime_serves_nothing_already_cached(settings):
    settings.MEDIA_URL_CACHE_SIZE = 2
    url_cache.put(("a",), {"url": "A"})
    settings.MEDIA_URL_CACHE_SIZE = 0
    assert url_cache.get(("a",)) is None
    url_cache.put(("b",), {"url": "B"})
    assert url_cache.size() == 0
