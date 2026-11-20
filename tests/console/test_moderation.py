"""Moderation in the console goes through the lifecycle services (docs/spec.md section 13)."""

from __future__ import annotations

import pytest

from octoreview.audit.models import AuditLog
from octoreview.events.models import OutboxEvent
from octoreview.media.models import ModerationStatus, ReviewMedia
from octoreview.reviews.models import Review, ReviewStatus
from octoreview.service_auth.models import ServiceClient
from tests.test_media import APP, SCOPES, create_review, set_settings, upload

pytestmark = pytest.mark.django_db

REVIEWS = "/admin/reviews/review/"
MEDIA = "/admin/media/reviewmedia/"


@pytest.fixture
def key(make_key):
    return make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})


@pytest.fixture
def pending(api_client, headers, key):
    set_settings(moderation_mode="pre_moderation")
    return Review.objects.get(pk=create_review(api_client, headers, key, title="Lovely")["id"])


def act(client, path, action, *ids, **extra):
    return client.post(path, {"action": action, "_selected_action": [str(i) for i in ids], **extra})


def test_publishing_shows_the_content_and_carries_the_versions(console, root_client, pending):
    page = act(root_client, REVIEWS, "publish_selected", pending.id)
    assert page.status_code == 200
    html = page.content.decode()
    assert "Lovely" in html
    assert f'name="version_{pending.id}" value="{pending.version}"' in html
    assert f'name="media_version_{pending.id}" value="{pending.media_version}"' in html
    pending.refresh_from_db()
    assert pending.status == ReviewStatus.PENDING  # nothing happens before the confirmation

    done = act(
        root_client,
        REVIEWS,
        "publish_selected",
        pending.id,
        confirm="yes",
        **{
            f"version_{pending.id}": pending.version,
            f"media_version_{pending.id}": pending.media_version,
        },
    )
    assert done.status_code == 302
    pending.refresh_from_db()
    assert pending.status == ReviewStatus.PUBLISHED
    row = AuditLog.objects.filter(action="review.publish", aggregate_id=str(pending.id)).get()
    assert row.actor_id == "admin:root"
    assert OutboxEvent.objects.filter(event_type="review.published", resource_id=str(pending.id))


def test_a_review_edited_after_the_confirmation_page_is_refused(
    console, root_client, pending, api_client, headers, key
):
    shown = (pending.version, pending.media_version)
    edited = api_client.patch(
        f"/api/v1/reviews/{pending.id}?{APP}", {"title": "Changed"}, format="json", **headers(key)
    )
    assert edited.status_code == 200, edited.json()
    done = act(
        root_client,
        REVIEWS,
        "publish_selected",
        pending.id,
        confirm="yes",
        **{f"version_{pending.id}": shown[0], f"media_version_{pending.id}": shown[1]},
    )
    assert done.status_code == 302
    pending.refresh_from_db()
    assert pending.status == ReviewStatus.PENDING
    messages = [m.message for m in done.wsgi_request._messages]
    assert any("changed since you loaded it" in m for m in messages)


def test_reject_runs_through_the_service(console, root_client, pending):
    act(root_client, REVIEWS, "reject_selected", pending.id)
    pending.refresh_from_db()
    assert pending.status == ReviewStatus.REJECTED
    assert AuditLog.objects.get(action="review.reject").actor_id == "admin:root"


def test_a_refused_transition_is_a_message_not_an_error(console, root_client, pending):
    response = act(root_client, REVIEWS, "hide_selected", pending.id)  # pending cannot be hidden
    assert response.status_code == 302
    pending.refresh_from_db()
    assert pending.status == ReviewStatus.PENDING
    messages = [m.message for m in response.wsgi_request._messages]
    assert any("invalid_transition" in m for m in messages)


def test_media_approval_shows_the_file_then_approves(
    console, root_client, api_client, headers, key
):
    review = create_review(api_client, headers, key)  # published while auto_publish
    set_settings(moderation_mode="pre_moderation")
    assert upload(api_client, headers, key, review["id"]).status_code == 200
    media = ReviewMedia.objects.get()
    assert media.moderation_status == ModerationStatus.PENDING
    parent = Review.objects.get(pk=review["id"])

    page = act(root_client, MEDIA, "approve_selected", media.id)
    assert page.status_code == 200
    assert "/api/v1/media-files/" in page.content.decode()  # the moderator can view it

    act(
        root_client,
        MEDIA,
        "approve_selected",
        media.id,
        confirm="yes",
        **{
            f"version_{media.id}": parent.version,
            f"media_version_{media.id}": parent.media_version,
        },
    )
    media.refresh_from_db()
    assert media.moderation_status == ModerationStatus.APPROVED


def test_media_reject(console, root_client, api_client, headers, key):
    review = create_review(api_client, headers, key)
    upload(api_client, headers, key, review["id"])
    media = ReviewMedia.objects.get()
    act(root_client, MEDIA, "reject_selected", media.id)
    media.refresh_from_db()
    assert media.moderation_status == ModerationStatus.REJECTED


def test_every_model_is_read_only_and_renders(console, root_client, pending, key):
    for path in (
        REVIEWS,
        MEDIA,
        "/admin/entities/entity/",
        "/admin/audit/auditlog/",
        "/admin/events/outboxevent/",
        "/admin/service_auth/serviceclient/",
        "/admin/service_auth/serviceclientgrant/",
        "/admin/reviews/reviewrevision/",
        "/admin/entities/applicationsettings/",
    ):
        assert root_client.get(path).status_code == 200, path
        assert root_client.get(f"{path}add/").status_code == 403, path
    detail = root_client.get(f"{REVIEWS}{pending.id}/change/")
    assert detail.status_code == 200
    assert b"Lovely" in detail.content


def test_the_key_hash_never_renders(console, root_client, key):
    client = ServiceClient.objects.get()
    page = root_client.get(f"/admin/service_auth/serviceclient/{client.id}/change/")
    assert page.status_code == 200
    assert client.hashed_key.encode() not in page.content
    assert b"hashed_key" not in page.content


def test_publishing_shows_every_attachment_that_goes_public(
    console, root_client, pending, api_client, headers, key
):
    """Media uploaded to a pending review carry moderation `none` and go public with it."""

    assert upload(api_client, headers, key, pending.id).status_code == 200
    assert upload(api_client, headers, key, pending.id).status_code == 200
    page = act(root_client, REVIEWS, "publish_selected", pending.id).content.decode()
    assert "Attachments that go public with it" in page
    assert page.count("/api/v1/media-files/") == 2


def test_a_review_without_attachments_says_so(console, root_client, pending):
    page = act(root_client, REVIEWS, "publish_selected", pending.id).content.decode()
    assert "No attachments." in page


def test_several_media_of_one_review_approve_together(
    console, root_client, api_client, headers, key
):
    review = create_review(api_client, headers, key)
    set_settings(moderation_mode="pre_moderation")
    for _ in range(2):
        assert upload(api_client, headers, key, review["id"]).status_code == 200
    items = list(ReviewMedia.objects.order_by("created_at"))
    page = act(root_client, MEDIA, "approve_selected", *(m.id for m in items)).content.decode()
    assert "No attachments." not in page  # media rows have no attachment list
    parent = Review.objects.get(pk=review["id"])
    versions = {}
    for media in items:
        versions[f"version_{media.id}"] = parent.version
        versions[f"media_version_{media.id}"] = parent.media_version
    act(root_client, MEDIA, "approve_selected", *(m.id for m in items), confirm="yes", **versions)
    assert {m.moderation_status for m in ReviewMedia.objects.all()} == {ModerationStatus.APPROVED}


def test_a_bulk_approval_still_refuses_an_outside_change(
    console, root_client, api_client, headers, key
):
    review = create_review(api_client, headers, key)
    set_settings(moderation_mode="pre_moderation")
    assert upload(api_client, headers, key, review["id"]).status_code == 200
    media = ReviewMedia.objects.get()
    parent = Review.objects.get(pk=review["id"])
    shown = {
        f"version_{media.id}": parent.version,
        f"media_version_{media.id}": parent.media_version,
    }
    # Another upload completes after the page rendered: media_version moves on.
    assert upload(api_client, headers, key, review["id"]).status_code == 200
    act(root_client, MEDIA, "approve_selected", media.id, confirm="yes", **shown)
    media.refresh_from_db()
    assert media.moderation_status == ModerationStatus.PENDING
