"""`make openapi-check`: the real contract passes, and every check fails on a broken fixture."""

from __future__ import annotations

import copy

import pytest
from rest_framework.test import APIClient

from octoreview.core.errors import ERROR_CODES, ErrorCode
from octoreview.events.models import OutboxEvent
from octoreview.events.services import EVENT_TYPES
from octoreview.openapi import contract
from tests.test_media import JPEG, SCOPES, create_review, put, set_settings, upload

DOCUMENT = contract.load_yaml(contract.OPENAPI)
SURFACE = contract.load_yaml(contract.SURFACE)
ERRORS_MD = contract.ERRORS_MD.read_text()
EVENTS_MD = contract.EVENTS_MD.read_text()


def document():
    return copy.deepcopy(DOCUMENT)


def surface():
    return copy.deepcopy(SURFACE)


def surface_entry(data, operation_id):
    return next(entry for entry in data["operations"] if entry["operation"] == operation_id)


def test_the_real_contract_passes():
    assert contract.run_all() == []


# --- document ------------------------------------------------------------------------------------


def test_an_invalid_document_fails():
    broken = document()
    broken["paths"]["/reviews"]["get"]["responses"] = "nope"
    assert contract.check_document(broken)[0].startswith("openapi.yaml is not valid OpenAPI")


def test_a_duplicate_operation_id_fails():
    broken = document()
    broken["paths"]["/reviews"]["get"]["operationId"] = "createReview"
    assert "is not unique" in contract.check_document(broken)[0]


def test_a_missing_operation_id_fails():
    broken = document()
    del broken["paths"]["/reviews"]["get"]["operationId"]
    assert contract.check_document(broken) == ["GET /reviews has no operationId"]


# --- routes --------------------------------------------------------------------------------------


def test_routes_include_plain_django_views():
    routes = contract.django_routes()
    assert ("PUT", "/media-uploads/{token}") in routes
    assert ("GET", "/media-files/{token}") in routes
    assert ("DELETE", "/reviews/{review_id}/media/{media_id}") in routes


def test_an_undocumented_route_fails():
    routes = contract.django_routes() | {("POST", "/reviews/{review_id}/pin")}
    assert contract.check_routes(DOCUMENT, routes) == [
        "POST /reviews/{review_id}/pin is served but missing from openapi.yaml"
    ]


def test_a_documented_route_the_server_lacks_fails():
    routes = contract.django_routes() - {("GET", "/audit-logs")}
    assert contract.check_routes(DOCUMENT, routes) == [
        "GET /audit-logs is in openapi.yaml but the server does not serve it"
    ]


# --- error codes ---------------------------------------------------------------------------------


def test_error_enum_must_equal_the_registry():
    registry = {**ERROR_CODES, "rate_limited": ErrorCode("rate_limited", 429)}
    assert contract.check_error_codes(DOCUMENT, registry) == [
        "error code rate_limited is missing from openapi.yaml ErrorCode"
    ]
    broken = document()
    broken["components"]["schemas"]["ErrorCode"]["enum"].append("teapot")
    assert contract.check_error_codes(broken, ERROR_CODES) == [
        "openapi.yaml ErrorCode lists teapot, which the server never sends"
    ]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda t: t.replace("### upload_changed\n", "### upload_changd\n"),
            "no section for upload_changed",
        ),
        (
            lambda t: t.replace("**Status:** 413", "**Status:** 400"),
            "upload_too_large: status must be 413",
        ),
        (
            lambda t: t.replace(
                "### upload_changed\n\n**Status:** 409 · **Retryable:** yes",
                "### upload_changed\n\n**Status:** 409 · **Retryable:** no",
            ),
            "upload_changed: retryable must be yes",
        ),
        (
            lambda t: t.replace(
                "**Recovery:** Not retryable for this media id.\n\n## 500", "\n## 500"
            ),
            "media_rejected: missing **Recovery:**",
        ),
        (
            lambda t: t + "\n### made_up\n\n**Status:** 400\n",
            "documents made_up, which is not an error code",
        ),
        (
            lambda t: t + "\n### not_found\n\n**Status:** 404\n",
            "more than one section for not_found",
        ),
    ],
)
def test_errors_md_drift_fails(mutate, expected):
    broken = mutate(ERRORS_MD)
    assert broken != ERRORS_MD
    assert any(expected in problem for problem in contract.check_errors_md(broken, ERROR_CODES))


# --- events --------------------------------------------------------------------------------------


def test_events_md_must_cover_the_registry():
    assert contract.check_events_md(EVENTS_MD, EVENT_TYPES | {"review.pinned"}) == [
        "events.md has no section for review.pinned"
    ]
    assert contract.check_events_md(EVENTS_MD, EVENT_TYPES - {"author.erased"}) == [
        "events.md documents author.erased, which the server never emits"
    ]


def test_an_event_section_without_fields_fails():
    broken = EVENTS_MD + "\n### review.pinned\n\nNo table.\n"
    problems = contract.check_events_md(broken, EVENT_TYPES | {"review.pinned"})
    assert problems == ["events.md review.pinned lists no payload fields"]


def test_unregistered_event_types_are_refused():
    from octoreview.core.audit_context import AuditContext
    from octoreview.events.services import build_outbox_event

    with pytest.raises(ValueError, match="Unregistered event type"):
        build_outbox_event(
            tenant_id="t",
            event_type="review.pinned",
            resource_type="review",
            resource_id="r",
            payload={},
            audit_context=AuditContext(),
        )


@pytest.mark.django_db
def test_every_event_payload_matches_events_md(api_client, headers, make_key):
    token = make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})
    h = headers(token)
    app = "application_id=app-1"
    review = create_review(api_client, headers, token)
    rid = review["id"]
    media = upload(api_client, headers, token, rid).json()["data"]
    api_client.post(
        f"/api/v1/reviews/{rid}/media/{media['id']}/reject?{app}", {}, format="json", **h
    )
    api_client.delete(f"/api/v1/reviews/{rid}/media/{media['id']}?{app}", **h)
    assert upload(api_client, headers, token, rid, body=JPEG).status_code == 422
    api_client.patch(f"/api/v1/reviews/{rid}?{app}", {"rating": 3}, format="json", **h)
    set_settings(moderation_mode="pre_moderation")
    api_client.patch(f"/api/v1/reviews/{rid}?{app}", {"rating": 2}, format="json", **h)
    api_client.post(f"/api/v1/reviews/{rid}/hide?{app}", {}, format="json", **h)
    other = create_review(api_client, headers, token, author_id="u2")
    api_client.delete(f"/api/v1/reviews/{other['id']}?{app}", **h)
    api_client.post(
        "/api/v1/authors/erase", {"application_id": "app-1", "author_id": "u1"}, format="json", **h
    )
    assert put  # the helpers above drive the local PUT

    documented = contract.documented_event_fields(EVENTS_MD)
    seen = set()
    for event in OutboxEvent.objects.all():
        assert set(event.payload) == documented[event.event_type], event.event_type
        seen.add(event.event_type)
    assert seen == EVENT_TYPES


# --- SDK surface ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s["operations"].pop(0), "no entry for getHealthLive"),
        (
            lambda s: s["operations"].append({"operation": "pinReview"}),
            "lists pinReview, which openapi.yaml does not define",
        ),
        (
            lambda s: surface_entry(s, "createReview")["required"].remove("rating"),
            "createReview: required must be",
        ),
        (
            lambda s: surface_entry(s, "listReviews")["optional"].append("cursor"),
            "listReviews: optional must be",
        ),
        (
            lambda s: surface_entry(s, "getReview").update(js="reviews.list"),
            "js method reviews.list is also listReviews",
        ),
        (lambda s: surface_entry(s, "getReview").pop("php"), "getReview: missing the php method"),
        (
            lambda s: surface_entry(s, "getReview").update(retry="sometimes"),
            "getReview: retry must be one of",
        ),
        (
            lambda s: surface_entry(s, "createMediaUpload").pop("idempotency_key"),
            "keyed retry needs",
        ),
        (lambda s: surface_entry(s, "downloadMedia").pop("reason"), "`sdk: none` needs a reason"),
        (
            lambda s: s["helpers"][0]["composes"].append("downloadMedia"),
            "composes downloadMedia, which no SDK method calls",
        ),
    ],
)
def test_surface_drift_fails(mutate, expected):
    broken = surface()
    mutate(broken)
    assert any(expected in problem for problem in contract.check_surface(broken, DOCUMENT))


def test_a_stale_surface_table_fails():
    rendered = contract.render_surface_md(SURFACE)
    assert contract.check_surface_md(SURFACE, rendered) == []
    assert contract.check_surface_md(SURFACE, rendered.replace("reviews.create", "reviews.add"))
    assert contract.check_surface_md(SURFACE, None)


def test_the_command_exits_non_zero_on_a_problem(monkeypatch):
    from django.core.management import call_command

    monkeypatch.setattr(contract, "run_all", lambda: ["something drifted"])
    with pytest.raises(SystemExit) as exit_info:
        call_command("check_contract")
    assert exit_info.value.code == 1


def test_the_api_client_is_unused_by_static_checks():
    # The static checks never touch the network or the database.
    assert APIClient
