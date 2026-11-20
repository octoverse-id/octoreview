"""Domain metrics (docs/spec.md section 16; docs/operations.md lists each with its fields)."""

from __future__ import annotations

import logging

import pytest
from django.db import transaction

from octoreview.core import metrics
from octoreview.events.dispatch import dispatch_outbox_events
from octoreview.lifecycle.media import cleanup_media
from tests.test_media import (
    APP,
    JPEG,
    SCOPES,
    complete,
    create_review,
    intent,
    set_settings,
    upload,
)

# Real commits, so on_commit callbacks run where production runs them.
pytestmark = pytest.mark.django_db(transaction=True)

PERSONAL = {"author_id", "title", "body", "author_metadata", "metadata", "source_ref"}


@pytest.fixture
def admin(make_key):
    return make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})


@pytest.fixture
def emitted(caplog):
    caplog.set_level(logging.INFO, logger="octoreview.metrics")

    def read(name: str | None = None):
        rows = [(r.metric, r.metric_fields) for r in caplog.records if hasattr(r, "metric")]
        return rows if name is None else [fields for metric, fields in rows if metric == name]

    return read


def test_reviews_count_created_published_and_rejected(emitted, api_client, headers, admin):
    create_review(api_client, headers, admin)
    assert emitted(metrics.REVIEW_CREATED) == [{"tenant_id": "tenant-a", "application_id": "app-1"}]
    assert len(emitted(metrics.REVIEW_PUBLISHED)) == 1

    set_settings(moderation_mode="pre_moderation")
    pending = create_review(api_client, headers, admin, author_id="u2")
    response = api_client.post(
        f"/api/v1/reviews/{pending['id']}/reject?{APP}", {}, format="json", **headers(admin)
    )
    assert response.status_code == 200, response.json()
    assert len(emitted(metrics.REVIEW_CREATED)) == 2
    assert len(emitted(metrics.REVIEW_PUBLISHED)) == 1
    assert emitted(metrics.REVIEW_REJECTED) == [
        {"tenant_id": "tenant-a", "application_id": "app-1"}
    ]


def test_an_upload_counts_intent_ready_and_each_storage_call(emitted, api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    assert upload(api_client, headers, admin, review["id"]).status_code == 200
    scope = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "kind": "image",
        "backend": "local",
    }
    assert emitted(metrics.MEDIA_INTENT) == [{**scope, "size_bytes": 64}]
    assert emitted(metrics.MEDIA_READY) == [{**scope, "size_bytes": 64}]
    calls = emitted(metrics.STORAGE_CALL)
    assert [(c["operation"], c["outcome"]) for c in calls] == [
        ("sign_upload", "ok"),
        ("stat", "ok"),
        ("read_head", "ok"),
        ("finalize", "ok"),
        ("delete", "ok"),  # the staging object, after phase 3
    ]
    assert all(c["backend"] == "local" and c["latency_ms"] >= 0 for c in calls)


def test_rejections_carry_their_reason_and_stage(emitted, api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    assert upload(api_client, headers, admin, review["id"], body=JPEG).status_code == 422
    assert [f["reason"] for f in emitted(metrics.MEDIA_REJECTED)] == ["type_mismatch"]

    set_settings(max_storage_bytes=10)
    assert intent(api_client, headers, admin, review["id"]).status_code == 409
    assert [f["stage"] for f in emitted(metrics.QUOTA_REJECTED)] == ["intent"]


def test_expected_storage_answers_are_not_errors(emitted, api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    created = intent(api_client, headers, admin, review["id"]).json()["data"]
    done = complete(api_client, headers, admin, review["id"], created["media"]["id"])
    assert done.status_code == 409  # upload_not_found: nothing was PUT
    stats = [c for c in emitted(metrics.STORAGE_CALL) if c["operation"] == "stat"]
    assert [c["outcome"] for c in stats] == ["missing"]


def test_cleanup_and_erasure_emit_counts_without_personal_data(emitted, api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    upload(api_client, headers, admin, review["id"])
    response = api_client.post(
        "/api/v1/authors/erase",
        {"application_id": "app-1", "author_id": "u1"},
        format="json",
        **headers(admin),
    )
    assert response.status_code == 200, response.json()
    assert emitted(metrics.ERASE_COMPLETED) == [
        {
            "tenant_id": "tenant-a",
            "application_id": "app-1",
            "reviews_redacted": 1,
            "pending_media_deletes": 1,
        }
    ]
    cleanup_media()
    assert emitted(metrics.CLEANUP_DELETED)[-1]["count"] == 1
    assert emitted(metrics.CLEANUP_FAILED)[-1]["count"] == 0

    dispatch_outbox_events()
    assert emitted(metrics.OUTBOX_LAG_SECONDS)[-1].keys() == {"seconds", "backlog"}

    for name, fields in emitted():
        assert not PERSONAL & set(fields), (name, fields)
        assert "u1" not in map(str, fields.values()), (name, fields)


def test_rolled_back_work_is_never_counted(emitted):
    with pytest.raises(RuntimeError), transaction.atomic():
        metrics.emit_on_commit("test.metric", value=1)
        raise RuntimeError("roll back")
    assert emitted("test.metric") == []
