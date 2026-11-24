"""Media race tests (docs/spec.md section 12).

Most interleavings are driven deterministically: the test runs the completion phases itself and
injects the competing operation between them, so the race is the same on every run and on both
databases. The quota and lock-order races need real row-lock contention and run as threads on
PostgreSQL. Every test asserts row states, no storage orphans, and counters equal to a recompute.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from octoreview.core.audit_context import AuditContext
from octoreview.core.errors import DomainError
from octoreview.lifecycle import erasure
from octoreview.lifecycle import media as lifecycle_media
from octoreview.lifecycle.recompute import recompute_ratings
from octoreview.media import files
from octoreview.media.models import MediaStatus, ReviewMedia
from tests.conftest import postgres_only
from tests.test_media import (
    APP,
    JPEG,
    PNG,
    SCOPES,
    create_review,
    intent,
    put,
    set_settings,
)

CTX = AuditContext(actor_id="race-test")
TENANT = "tenant-a"


@pytest.fixture
def admin(make_key):
    return make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})


def staged(api_client, headers, token, external_id="sku-1", author_id="u1"):
    """A review with one pending media item whose bytes are already PUT."""

    review = create_review(api_client, headers, token, external_id=external_id, author_id=author_id)
    data = intent(api_client, headers, token, review["id"]).json()["data"]
    assert put(APIClient(), data["upload"]["url"], PNG).status_code == 204
    return review["id"], data["media"]["id"]


def stored_files(review_id) -> set[str]:
    """Every file under the media root that belongs to this review or its media."""

    root = Path(settings.MEDIA_ROOT)
    media_ids = [
        str(i) for i in ReviewMedia.objects.filter(review_id=review_id).values_list("id", flat=True)
    ]
    found = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        if str(review_id) in relative or any(media_id in path.name for media_id in media_ids):
            found.add(relative)
    return found


def expected_files(review_id) -> set[str]:
    rows = ReviewMedia.objects.filter(review_id=review_id)
    ready = {row.storage_key for row in rows if row.status == MediaStatus.READY}
    staging = {row.staging_key for row in rows if row.status == MediaStatus.PENDING}
    return ready | {key for key in staging if (Path(settings.MEDIA_ROOT) / key).exists()}


def assert_consistent(review_id):
    assert stored_files(review_id) == expected_files(review_id)
    assert recompute_ratings(repair=False) == []


def run_phases(review_id, media_id, between_two_and_three=None):
    media, attempt = lifecycle_media._phase_one(review_id, media_id, TENANT)
    outcome = lifecycle_media._phase_two(media)
    if between_two_and_three:
        between_two_and_three()
    return lifecycle_media._phase_three(review_id, media_id, TENANT, attempt, outcome, CTX)


# --- deterministic interleavings ----------------------------------------------------------------


@pytest.mark.django_db
def test_complete_vs_complete_with_a_re_put_between(api_client, headers, admin, monkeypatch):
    review_id, media_id = staged(api_client, headers, admin)
    real_read_head = files.read_head
    re_put = []

    def read_head_after_a_re_put(key, length, *, if_version):
        if not re_put:
            re_put.append(key)
            files.write_stream(key, [PNG], limit=len(PNG))  # same bytes, new inode
        return real_read_head(key, length, if_version=if_version)

    monkeypatch.setattr(files, "read_head", read_head_after_a_re_put)
    with pytest.raises(DomainError) as first:
        lifecycle_media.complete(review_id, media_id, CTX, tenant_id=TENANT)
    assert first.value.code == "upload_changed"
    assert ReviewMedia.objects.get().completion_lease_until is None

    second = lifecycle_media.complete(review_id, media_id, CTX, tenant_id=TENANT)
    assert second.status == MediaStatus.READY
    assert_consistent(review_id)


@pytest.mark.django_db
def test_stale_attempt_loses_to_a_newer_one(api_client, headers, admin):
    review_id, media_id = staged(api_client, headers, admin)
    media_a, attempt_a = lifecycle_media._phase_one(review_id, media_id, TENANT)
    # A stalls past its lease; B takes over and finishes first.
    ReviewMedia.objects.update(completion_lease_until=timezone.now() - timedelta(seconds=1))
    outcome_a = lifecycle_media._phase_two(media_a)
    media_b, attempt_b = lifecycle_media._phase_one(review_id, media_id, TENANT)
    with pytest.raises(DomainError) as missing:
        lifecycle_media._phase_two(media_b)  # A already moved the staged file
    assert missing.value.code == "upload_not_found"
    lifecycle_media._release_lease(review_id, media_id, TENANT, attempt_b)
    result = lifecycle_media._phase_three(review_id, media_id, TENANT, attempt_a, outcome_a, CTX)
    # B's release cleared the attempt id, so A's result is discarded with its final object.
    assert result.status == MediaStatus.PENDING
    assert stored_files(review_id) == set()
    assert recompute_ratings(repair=False) == []


@pytest.mark.django_db
def test_complete_vs_media_delete(api_client, headers, admin):
    review_id, media_id = staged(api_client, headers, admin)
    result = run_phases(
        review_id,
        media_id,
        lambda: lifecycle_media.delete_media(review_id, media_id, CTX, tenant_id=TENANT),
    )
    assert result.status == MediaStatus.DELETED
    assert stored_files(review_id) == set()
    assert_consistent(review_id)


@pytest.mark.django_db
def test_complete_vs_erasure(api_client, headers, admin):
    review_id, media_id = staged(api_client, headers, admin)

    def erase():
        erasure.erase_author(
            tenant_id="tenant-a", application_id="app-1", author_id="u1", audit_context=CTX
        )

    result = run_phases(review_id, media_id, erase)
    assert result.status == MediaStatus.DELETING
    assert lifecycle_media.cleanup_media().deleted == 1
    assert ReviewMedia.objects.get().status == MediaStatus.DELETED
    assert stored_files(review_id) == set()
    assert_consistent(review_id)


@pytest.mark.django_db
def test_local_put_vs_complete(api_client, headers, admin):
    review_id, media_id = staged(api_client, headers, admin)
    staging_key = ReviewMedia.objects.get().staging_key
    # A PUT that passed the pending check lands after phase two moved the staged file.
    result = run_phases(
        review_id,
        media_id,
        lambda: files.write_stream(staging_key, [JPEG], limit=len(JPEG)),
    )
    assert result.status == MediaStatus.READY
    final = Path(settings.MEDIA_ROOT) / result.storage_key
    assert final.read_bytes() == PNG  # served bytes never change
    assert_consistent(review_id)


@pytest.mark.django_db
def test_local_put_vs_put(api_client, headers, admin):
    review_id, media_id = staged(api_client, headers, admin)
    staging_key = ReviewMedia.objects.get().staging_key

    def slow_put():
        yield PNG[:10]
        files.write_stream(staging_key, [JPEG], limit=len(JPEG))  # the other PUT finishes first
        yield PNG[10:]

    files.write_stream(staging_key, slow_put(), limit=len(PNG))
    staged_path = Path(settings.MEDIA_ROOT) / staging_key
    assert staged_path.read_bytes() == PNG  # last writer wins, whole
    assert not list(staged_path.parent.glob(f"{staged_path.name}.part-*"))
    assert (
        lifecycle_media.complete(review_id, media_id, CTX, tenant_id=TENANT).status
        == MediaStatus.READY
    )
    assert_consistent(review_id)


@pytest.mark.django_db
def test_cleanup_vs_complete(api_client, headers, admin, monkeypatch):
    review_id, media_id = staged(api_client, headers, admin)
    long_ago = timezone.now() - timedelta(days=1)

    # Cleanup claims the expired row, then a completion lease appears before cleanup locks it.
    real_claim = lifecycle_media._claim

    def claim_then_lease(queryset, now):
        claimed = real_claim(queryset, now)
        ReviewMedia.objects.filter(pk=media_id).update(
            completion_lease_until=timezone.now() + timedelta(minutes=5)
        )
        return claimed

    ReviewMedia.objects.update(upload_expires_at=long_ago)
    monkeypatch.setattr(lifecycle_media, "_claim", claim_then_lease)
    result = lifecycle_media.cleanup_media()
    assert result.deleted == 0
    row = ReviewMedia.objects.get()
    assert row.status == MediaStatus.PENDING
    assert (Path(settings.MEDIA_ROOT) / row.staging_key).exists()

    # A live lease also keeps the row out of the claim itself.
    monkeypatch.setattr(lifecycle_media, "_claim", real_claim)
    ReviewMedia.objects.update(cleanup_claimed_until=None)
    assert lifecycle_media.cleanup_media().deleted == 0
    assert ReviewMedia.objects.get().status == MediaStatus.PENDING
    assert_consistent(review_id)


# --- PostgreSQL lock contention -----------------------------------------------------------------


def run_threads(*targets):
    barrier = threading.Barrier(len(targets))
    results, errors = [None] * len(targets), []

    def runner(index, target):
        try:
            barrier.wait(timeout=10)
            results[index] = target()
        except Exception as exc:  # surfaced through `errors`
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=runner, args=(index, target))
        for index, target in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    return results


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_concurrent_intents_respect_the_quota(api_client, headers, admin):
    reviews = [
        create_review(api_client, headers, admin, external_id=f"sku-{i}")["id"] for i in range(2)
    ]
    set_settings(max_storage_bytes=len(PNG) + len(PNG) // 2)

    def make_intent(review_id):
        return lambda: intent(APIClient(), headers, admin, review_id).status_code

    statuses = run_threads(*(make_intent(review_id) for review_id in reviews))
    assert sorted(statuses) == [201, 409]
    assert ReviewMedia.objects.count() == 1


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_intent_vs_complete_on_the_same_review_with_a_quota(api_client, headers, admin):
    review_id, media_id = staged(api_client, headers, admin)
    set_settings(max_storage_bytes=len(PNG) * 4)

    statuses = run_threads(
        lambda: intent(APIClient(), headers, admin, review_id).status_code,
        lambda: (
            APIClient()
            .post(
                f"/api/v1/reviews/{review_id}/media/{media_id}/complete?{APP}",
                {},
                format="json",
                **headers(admin),
            )
            .status_code
        ),
    )
    assert statuses == [201, 200]
    assert ReviewMedia.objects.filter(status=MediaStatus.READY).count() == 1
    assert_consistent(review_id)


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_lock_order_has_no_deadlock(api_client, headers, admin):
    """Media writes (settings → review → media) against review writes (entity → review)."""

    set_settings(max_storage_bytes=len(PNG) * 50)
    for round_number in range(5):
        review_id, media_id = staged(api_client, headers, admin, author_id=f"u{round_number}")
        statuses = run_threads(*mixed_writers(headers, admin, review_id, media_id))
        # Every request finished with a product answer; none hit a deadlock (500).
        assert 500 not in statuses, statuses
        assert_consistent(review_id)


def mixed_writers(headers, token, review_id, media_id):
    base = f"/api/v1/reviews/{review_id}"

    def complete_media():
        url = f"{base}/media/{media_id}/complete?{APP}"
        return APIClient().post(url, {}, format="json", **headers(token)).status_code

    def new_intent():
        return intent(APIClient(), headers, token, review_id).status_code

    def edit_review():
        url = f"{base}?{APP}"
        return APIClient().patch(url, {"rating": 3}, format="json", **headers(token)).status_code

    def hide_review():
        url = f"{base}/hide?{APP}"
        return APIClient().post(url, {}, format="json", **headers(token)).status_code

    return complete_media, new_intent, edit_review, hide_review
