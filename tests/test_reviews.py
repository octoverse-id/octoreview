import pytest

from octoreview.entities.models import ApplicationSettings, Entity
from octoreview.events.models import OutboxEvent
from octoreview.lifecycle.recompute import recompute_ratings
from octoreview.lifecycle.transitions import REVIEW_TRANSITIONS, allowed_actions
from octoreview.reviews.models import Review, ReviewStatus

pytestmark = pytest.mark.django_db

ALL_SCOPES = [
    "entities:read",
    "entities:write",
    "reviews:read",
    "reviews:write",
    "reviews:moderate",
    "reviews:attest",
]
REVIEWS = "/api/v1/reviews"
APP = "application_id=app-1"
BASE = {"application_id": "app-1", "entity_type": "product", "external_id": "sku-1"}


@pytest.fixture
def admin(make_key):
    return make_key({"application_id": "app-1", "scopes": ALL_SCOPES}, name="admin")


@pytest.fixture
def writer(make_key):
    return make_key({"application_id": "app-1", "scopes": ["reviews:read", "reviews:write"]})


def set_mode(mode):
    ApplicationSettings.objects.update_or_create(
        tenant_id="tenant-a", application_id="app-1", defaults={"moderation_mode": mode}
    )


def create(api_client, headers, token, **body):
    payload = {**BASE, "author_id": "u1", "rating": 5, "title": "Great", "body": "Loved it"}
    payload.update(body)
    return api_client.post("/api/v1/reviews", payload, format="json", **headers(token))


def entity():
    return Entity.objects.get(external_id="sku-1")


def event_types():
    return list(
        OutboxEvent.objects.order_by("created_at", "id").values_list("event_type", flat=True)
    )


def act(api_client, headers, token, review, action, **body):
    if action == "remove":
        return api_client.delete(f"{REVIEWS}/{review['id']}?{APP}", **headers(token))
    if action == "publish":
        body.setdefault("expected_version", review["version"])
        body.setdefault("expected_media_version", review["media_version"])
    return api_client.post(
        f"{REVIEWS}/{review['id']}/{action}?{APP}",
        body,
        format="json",
        **headers(token),
    )


# --- create -------------------------------------------------------------------------------------


def test_create_auto_publish_counts_and_emits(api_client, headers, admin):
    response = create(api_client, headers, admin)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "published"
    assert (data["version"], data["media_version"]) == (1, 1)
    assert entity().rating_count == 1
    assert event_types() == ["review.created", "review.published", "entity.rating_changed"]
    for event in OutboxEvent.objects.all():
        assert not {"title", "body", "author_id", "author_metadata"} & set(event.payload)


def test_create_pre_moderation_is_pending_and_uncounted(api_client, headers, admin):
    set_mode("pre_moderation")
    data = create(api_client, headers, admin).json()["data"]
    assert data["status"] == "pending"
    assert entity().rating_count == 0
    assert event_types() == ["review.created"]


def test_duplicate_review_returns_the_existing_id(api_client, headers, admin):
    first = create(api_client, headers, admin).json()["data"]
    again = create(api_client, headers, admin, rating=1)
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "review_exists"
    assert error["details"] == {"review_id": first["id"]}


def test_source_ref_opens_a_second_slot_and_needs_attest(api_client, headers, admin, writer):
    create(api_client, headers, admin)
    by_order = create(api_client, headers, admin, source_ref="order-9", is_verified_purchase=True)
    assert by_order.status_code == 201
    assert entity().verified_review_count == 1
    denied = create(api_client, headers, writer, author_id="u2", source_ref="order-1")
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["required_scope"] == "reviews:attest"
    denied_flag = create(api_client, headers, writer, author_id="u2", is_verified_purchase=True)
    assert denied_flag.status_code == 403


def test_removed_review_frees_the_slot(api_client, headers, admin):
    first = create(api_client, headers, admin).json()["data"]
    act(api_client, headers, admin, first, "remove")
    assert create(api_client, headers, admin).status_code == 201


def test_inactive_entity_refuses_new_reviews(api_client, headers, admin):
    create(api_client, headers, admin)
    Entity.objects.update(is_active=False)
    response = create(api_client, headers, admin, author_id="u2")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "entity_inactive"


@pytest.mark.parametrize(
    "body",
    [
        {"title": "bad\x00title"},
        {"body": "bell\x07"},
        {"rating": 0},
        {"rating": 6},
        {"title": "x" * 201},
        {"author_metadata": {"avatar_url": "http://insecure.example/a.png"}},
        {"entity_type": "Bad Type"},
        {"entity_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
def test_create_validation(api_client, headers, admin, body):
    response = create(api_client, headers, admin, **body)
    assert response.status_code == 400


def test_text_is_stored_verbatim(api_client, headers, admin):
    data = create(api_client, headers, admin, body="<script>x</script>\n\tok").json()["data"]
    assert data["body"] == "<script>x</script>\n\tok"


# --- transition table ---------------------------------------------------------------------------

STATUSES = [s for s in ReviewStatus.values]
ACTIONS = ["publish", "reject", "hide", "remove"]


def review_in(api_client, headers, admin, status):
    set_mode("pre_moderation")
    review = create(api_client, headers, admin).json()["data"]
    path = {
        "pending": [],
        "published": ["publish"],
        "rejected": ["reject"],
        "hidden": ["publish", "hide"],
        "removed": ["remove"],
    }[status]
    for action in path:
        review = act(api_client, headers, admin, review, action).json()["data"]
    assert review["status"] == status
    return review


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("action", ACTIONS)
def test_every_status_action_pair(api_client, headers, admin, status, action):
    review = review_in(api_client, headers, admin, status)
    before = entity().rating_count
    response = act(api_client, headers, admin, review, action)
    target = REVIEW_TRANSITIONS.get((status, action))
    if target is None:
        assert response.status_code == 409
        details = response.json()["error"]["details"]
        assert details["allowed_actions"] == allowed_actions(status)
        assert entity().rating_count == before
        return
    assert response.status_code == 200
    assert response.json()["data"]["status"] == target
    expected = before + (target == "published") - (status == "published")
    assert entity().rating_count == expected
    assert recompute_ratings(repair=False) == []


def test_publish_requires_and_checks_expected_versions(api_client, headers, admin):
    set_mode("pre_moderation")
    review = create(api_client, headers, admin).json()["data"]
    missing = api_client.post(
        f"{REVIEWS}/{review['id']}/publish?{APP}",
        {},
        format="json",
        **headers(admin),
    )
    assert missing.status_code == 400
    stale = act(api_client, headers, admin, review, "publish", expected_version=99)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "review_changed"


def test_moderator_never_publishes_unseen_edits(api_client, headers, admin):
    set_mode("pre_moderation")
    seen = create(api_client, headers, admin).json()["data"]
    api_client.patch(
        f"{REVIEWS}/{seen['id']}?{APP}",
        {"body": "edited after load"},
        format="json",
        **headers(admin),
    )
    response = act(api_client, headers, admin, seen, "publish")
    assert response.status_code == 409
    assert Review.objects.get(pk=seen["id"]).status == "pending"


def test_reject_and_hide_store_the_reason(api_client, headers, admin):
    review = review_in(api_client, headers, admin, "published")
    hidden = act(api_client, headers, admin, review, "hide", reason="spam").json()["data"]
    assert hidden["status_reason"] == "spam"


# --- edits and revisions ------------------------------------------------------------------------


def patch(api_client, headers, token, review, **body):
    return api_client.patch(
        f"{REVIEWS}/{review['id']}?{APP}",
        body,
        format="json",
        **headers(token),
    )


def test_auto_publish_edit_applies_and_bumps_version(api_client, headers, admin):
    review = create(api_client, headers, admin).json()["data"]
    data = patch(api_client, headers, admin, review, rating=3).json()["data"]
    assert (data["rating"], data["version"]) == (3, 2)
    assert entity().rating_3_count == 1 and entity().rating_5_count == 0


def test_pre_moderation_edit_becomes_a_revision(api_client, headers, admin):
    review = review_in(api_client, headers, admin, "published")
    data = patch(api_client, headers, admin, review, rating=2, body="changed").json()["data"]
    assert (data["rating"], data["body"], data["version"]) == (5, "Loved it", 1)
    revision = api_client.get(f"{REVIEWS}/{review['id']}/revision?{APP}", **headers(admin)).json()[
        "data"
    ]
    assert (revision["rating"], revision["base_version"]) == (2, 1)
    approved = api_client.post(
        f"{REVIEWS}/{review['id']}/revision/approve?{APP}",
        {"revision_id": revision["id"], "expected_version": 1, "expected_media_version": 1},
        format="json",
        **headers(admin),
    )
    assert approved.status_code == 200
    data = approved.json()["data"]
    assert (data["rating"], data["body"], data["version"]) == (2, "changed", 2)
    assert entity().rating_2_count == 1
    assert recompute_ratings(repair=False) == []


def test_newer_edit_supersedes_the_revision(api_client, headers, admin):
    review = review_in(api_client, headers, admin, "published")
    patch(api_client, headers, admin, review, rating=2)
    old = api_client.get(f"{REVIEWS}/{review['id']}/revision?{APP}", **headers(admin)).json()
    patch(api_client, headers, admin, review, rating=3)
    response = api_client.post(
        f"{REVIEWS}/{review['id']}/revision/approve?{APP}",
        {"revision_id": old["data"]["id"], "expected_version": 99, "expected_media_version": 1},
        format="json",
        **headers(admin),
    )
    # superseded wins over review_changed (docs/spec.md section 4.4).
    assert response.json()["error"]["code"] == "revision_superseded"


def test_attestation_does_not_stale_a_pending_revision(api_client, headers, admin):
    review = review_in(api_client, headers, admin, "published")
    patch(api_client, headers, admin, review, title="new title")
    attested = patch(api_client, headers, admin, review, is_verified_purchase=True).json()["data"]
    assert attested["version"] == 1
    revision = api_client.get(f"{REVIEWS}/{review['id']}/revision?{APP}", **headers(admin)).json()
    approved = api_client.post(
        f"{REVIEWS}/{review['id']}/revision/approve?{APP}",
        {"revision_id": revision["data"]["id"], "expected_version": 1, "expected_media_version": 1},
        format="json",
        **headers(admin),
    )
    assert approved.status_code == 200
    assert entity().verified_review_count == 1


def test_stale_revision_after_mode_switch(api_client, headers, admin):
    review = review_in(api_client, headers, admin, "published")
    patch(api_client, headers, admin, review, rating=2)
    revision = api_client.get(f"{REVIEWS}/{review['id']}/revision?{APP}", **headers(admin)).json()
    set_mode("auto_publish")
    patch(api_client, headers, admin, review, rating=4)
    response = api_client.post(
        f"{REVIEWS}/{review['id']}/revision/approve?{APP}",
        {"revision_id": revision["data"]["id"], "expected_version": 2, "expected_media_version": 1},
        format="json",
        **headers(admin),
    )
    assert response.json()["error"]["code"] == "revision_stale"


def test_reject_revision_drops_it(api_client, headers, admin):
    review = review_in(api_client, headers, admin, "published")
    patch(api_client, headers, admin, review, rating=1)
    revision = api_client.get(f"{REVIEWS}/{review['id']}/revision?{APP}", **headers(admin)).json()
    rejected = api_client.post(
        f"{REVIEWS}/{review['id']}/revision/reject?{APP}",
        {"revision_id": revision["data"]["id"]},
        format="json",
        **headers(admin),
    )
    assert rejected.status_code == 200
    assert (
        api_client.get(f"{REVIEWS}/{review['id']}/revision?{APP}", **headers(admin)).status_code
        == 404
    )


def test_source_ref_is_immutable(api_client, headers, admin):
    review = create(api_client, headers, admin).json()["data"]
    response = patch(api_client, headers, admin, review, source_ref="order-1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_ref_immutable"


def test_edits_refused_outside_pending_and_published(api_client, headers, admin):
    review = review_in(api_client, headers, admin, "hidden")
    response = patch(api_client, headers, admin, review, rating=1)
    assert response.status_code == 409
    assert response.json()["error"]["details"]["action"] == "edit"


def test_verified_flag_change_needs_attest(api_client, headers, admin, writer):
    review = create(api_client, headers, admin).json()["data"]
    response = patch(api_client, headers, writer, review, is_verified_purchase=True)
    assert response.status_code == 403


# --- visibility ---------------------------------------------------------------------------------


def test_lists_show_published_only_by_default(api_client, headers, admin, writer):
    set_mode("pre_moderation")
    pending = create(api_client, headers, admin, author_id="u1").json()["data"]
    published = review_in_author(api_client, headers, admin, "u2")
    ids = [r["id"] for r in api_client.get(f"{REVIEWS}?{APP}", **headers(writer)).json()["data"]]
    assert ids == [published["id"]]
    denied = api_client.get(f"{REVIEWS}?{APP}&status=pending", **headers(writer))
    assert denied.status_code == 403
    mine = api_client.get(
        f"{REVIEWS}?{APP}&author_id=u1&include_unpublished=true",
        **headers(writer),
    ).json()["data"]
    assert [r["id"] for r in mine] == [pending["id"]]
    queue = api_client.get(f"{REVIEWS}?{APP}&status=pending", **headers(admin)).json()["data"]
    assert [r["id"] for r in queue] == [pending["id"]]


def review_in_author(api_client, headers, admin, author):
    review = create(api_client, headers, admin, author_id=author).json()["data"]
    if review["status"] == "pending":
        review = act(api_client, headers, admin, review, "publish").json()["data"]
    return review


def test_pending_revision_queue_needs_moderate(api_client, headers, admin, writer):
    review = review_in(api_client, headers, admin, "published")
    patch(api_client, headers, admin, review, rating=1)
    assert (
        api_client.get(f"{REVIEWS}?{APP}&has_pending_revision=true", **headers(writer)).status_code
        == 403
    )
    queue = api_client.get(f"{REVIEWS}?{APP}&has_pending_revision=true", **headers(admin))
    assert [r["id"] for r in queue.json()["data"]] == [review["id"]]


def test_detail_returns_any_status(api_client, headers, admin, writer):
    review = review_in(api_client, headers, admin, "rejected")
    response = api_client.get(f"{REVIEWS}/{review['id']}?{APP}", **headers(writer))
    assert response.json()["data"]["status"] == "rejected"


def test_filters_and_sort(api_client, headers, admin, writer):
    for author, rating in (("a", 2), ("b", 5), ("c", 4)):
        create(api_client, headers, admin, author_id=author, rating=rating)
    by_rating = api_client.get(f"{REVIEWS}?{APP}&sort=rating", **headers(writer)).json()["data"]
    assert [r["rating"] for r in by_rating] == [5, 4, 2]
    only_four = api_client.get(f"{REVIEWS}?{APP}&rating=4", **headers(writer)).json()["data"]
    assert [r["author_id"] for r in only_four] == ["c"]
    by_key = api_client.get(
        f"{REVIEWS}?{APP}&entity_type=product&external_id=sku-1",
        **headers(writer),
    ).json()["pagination"]["count"]
    assert by_key == 3
    assert api_client.get(f"{REVIEWS}?{APP}&sort=oldest", **headers(writer)).status_code == 400


def test_create_review_rechecks_the_locked_entity(api_client, headers, admin):
    """A deactivation that commits after the caller resolved the entity still wins (spec 4.2)."""

    from octoreview.core.audit_context import AuditContext
    from octoreview.core.errors import DomainError
    from octoreview.lifecycle.services import NewReview, create_review

    create = api_client.post(
        "/api/v1/reviews",
        {
            "application_id": "app-1",
            "entity_type": "product",
            "external_id": "sku-race",
            "author_id": "u1",
            "rating": 5,
        },
        format="json",
        **headers(admin),
    )
    assert create.status_code == 201
    stale = Entity.objects.get(external_id="sku-race")
    Entity.objects.filter(pk=stale.pk).update(is_active=False)
    with pytest.raises(DomainError) as raised:
        create_review(stale, NewReview(author_id="u2", rating=4), AuditContext())
    assert raised.value.code == "entity_inactive"
    assert Review.objects.filter(entity=stale).count() == 1


def test_write_paths_refuse_another_tenants_ids(api_client, headers, admin):
    """Locks and services name the tenant in every query (AGENTS.md), whatever the caller passes."""

    from octoreview.core.audit_context import AuditContext
    from octoreview.core.errors import NotFoundError
    from octoreview.entities.ratings import Contribution, apply_delta
    from octoreview.lifecycle import media as media_lifecycle
    from octoreview.lifecycle import services, transitions

    create = api_client.post(
        "/api/v1/reviews",
        {
            "application_id": "app-1",
            "entity_type": "product",
            "external_id": "sku-scope",
            "author_id": "u1",
            "rating": 5,
        },
        format="json",
        **headers(admin),
    )
    assert create.status_code == 201
    review_id = create.json()["data"]["id"]
    before = Review.objects.values("status", "rating", "version", "media_version").get(pk=review_id)
    ctx = AuditContext()
    intent = media_lifecycle.IntentRequest(kind="image", content_type="image/png", size_bytes=10)
    refused = [
        lambda: services.transition_review(review_id, transitions.HIDE, ctx, tenant_id="tenant-b"),
        lambda: services.update_review(review_id, {"rating": 1}, ctx, tenant_id="tenant-b"),
        lambda: media_lifecycle.create_intent(review_id, intent, ctx, tenant_id="tenant-b"),
    ]
    for call in refused:
        with pytest.raises(NotFoundError):
            call()
    after = Review.objects.values("status", "rating", "version", "media_version").get(pk=review_id)
    assert after == before

    foreign = Entity.objects.get(external_id="sku-scope")
    foreign.tenant_id = "tenant-b"
    assert apply_delta(foreign, add=Contribution(1, False)) is False
    assert Entity.objects.get(external_id="sku-scope").rating_count == 1
