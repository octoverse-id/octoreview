import hashlib
import hmac
import io
import json
import logging
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from octoreview.core.audit_context import AuditContext
from octoreview.core.logging import JsonFormatter
from octoreview.events import dispatch
from octoreview.events.models import OutboxEvent
from octoreview.events.services import AUTHOR_ERASED, build_outbox_event, enqueue

pytestmark = pytest.mark.django_db

CTX = AuditContext(request_id="req_evt")


def make_event(event_type="review.created", payload=None, **overrides):
    values = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "event_type": event_type,
        "resource_type": "review",
        "resource_id": "r1",
        "payload": payload if payload is not None else {"review_id": "r1", "rating": 5},
        "audit_context": CTX,
    }
    values.update(overrides)
    return enqueue(**values)


class RecordingTransport:
    def __init__(self, fail=False):
        self.fail = fail
        self.published = []

    def publish(self, event):
        if self.fail:
            raise RuntimeError("receiver down")
        self.published.append(event.id)


@pytest.mark.parametrize("field", ["title", "body", "author_metadata", "author_id", "source_ref"])
def test_payloads_cannot_carry_personal_data(field):
    with pytest.raises(ValueError):
        build_outbox_event(
            tenant_id="t",
            event_type="review.created",
            resource_type="review",
            resource_id="r1",
            payload={field: "x"},
            audit_context=CTX,
        )


def test_author_erased_may_carry_the_author_id():
    event = make_event(AUTHOR_ERASED, payload={"author_id": "u1", "erased_at": "now"})
    assert event.payload["author_id"] == "u1"


def test_logging_transport_logs_the_envelope_never_the_payload(caplog):
    event = make_event(payload={"review_id": "r1", "rating": 5, "secret_marker": "SENTINEL"})
    with caplog.at_level(logging.INFO, logger="octoreview.events.dispatch"):
        dispatch.dispatch_outbox_events(transport=dispatch.LoggingEventTransport())
    lines = [JsonFormatter().format(record) for record in caplog.records]
    assert any(str(event.id) in line for line in lines)
    assert not any("SENTINEL" in line for line in lines)


def test_webhook_signature_is_timestamped():
    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeOpener:
        def __init__(self):
            self.requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            return FakeResponse()

    opener = FakeOpener()
    transport = dispatch.WebhookEventTransport(
        url="https://receiver.example/hook",
        secret="s3cret",
        timeout_seconds=5,
        opener=opener,
        clock=lambda: 1_800_000_000,
    )
    event = make_event()
    transport.publish(event)
    request = opener.requests[0]
    body = request.data
    signature = request.get_header("X-octoreview-signature")
    expected = hmac.new(b"s3cret", b"1800000000." + body, hashlib.sha256).hexdigest()
    assert signature == f"t=1800000000,v1={expected}"
    assert request.get_header("X-octoreview-event") == "review.created"
    assert request.get_header("X-octoreview-delivery") == str(event.id)
    document = json.loads(body)
    assert document["data"] == {"review_id": "r1", "rating": 5}
    assert document["type"] == "review.created"


def test_dispatch_publishes_and_marks_published():
    event = make_event()
    transport = RecordingTransport()
    summary = dispatch.dispatch_outbox_events(transport=transport)
    event.refresh_from_db()
    assert summary["published"] == 1
    assert event.status == OutboxEvent.Status.PUBLISHED
    assert event.published_at is not None


def test_failures_back_off_then_dead_letter(settings):
    """A scheduled (plain) run retries a failed event once its backoff has elapsed."""

    settings.OUTBOX_MAX_ATTEMPTS = 2
    event = make_event()
    transport = RecordingTransport(fail=True)
    dispatch.dispatch_outbox_events(transport=transport)
    event.refresh_from_db()
    assert event.status == OutboxEvent.Status.FAILED
    assert event.available_at > timezone.now()

    summary = dispatch.dispatch_outbox_events(transport=transport)
    assert summary["failed"] + summary["dead_lettered"] == 0  # still inside its backoff

    OutboxEvent.objects.filter(id=event.id).update(available_at=timezone.now())
    dispatch.dispatch_outbox_events(transport=transport)
    event.refresh_from_db()
    assert event.status == OutboxEvent.Status.DEAD_LETTER


def test_a_scheduled_run_delivers_a_retry_but_never_touches_dead_letters():
    retry = make_event()
    OutboxEvent.objects.filter(id=retry.id).update(
        status=OutboxEvent.Status.FAILED, attempts=1, available_at=timezone.now()
    )
    dead = make_event()
    OutboxEvent.objects.filter(id=dead.id).update(status=OutboxEvent.Status.DEAD_LETTER, attempts=5)
    dispatch.dispatch_outbox_events(transport=RecordingTransport())
    retry.refresh_from_db()
    dead.refresh_from_db()
    assert retry.status == OutboxEvent.Status.PUBLISHED
    assert (dead.status, dead.attempts) == (OutboxEvent.Status.DEAD_LETTER, 5)


def test_retry_failed_replays_a_dead_letter_once_per_run():
    event = make_event()
    OutboxEvent.objects.filter(id=event.id).update(
        status=OutboxEvent.Status.DEAD_LETTER, attempts=5
    )
    failing = RecordingTransport(fail=True)
    summary = dispatch.dispatch_outbox_events(transport=failing, retry_failed=True, limit=50)
    assert summary["failed"] + summary["dead_lettered"] == 1
    ok = RecordingTransport()
    OutboxEvent.objects.filter(id=event.id).update(available_at=timezone.now())
    dispatch.dispatch_outbox_events(transport=ok, retry_failed=True)
    event.refresh_from_db()
    assert event.status == OutboxEvent.Status.PUBLISHED


def test_delivered_author_erased_row_is_deleted():
    event = make_event(AUTHOR_ERASED, payload={"author_id": "u1", "erased_at": "now"})
    dispatch.dispatch_outbox_events(transport=RecordingTransport())
    assert not OutboxEvent.objects.filter(id=event.id).exists()


def test_expired_claims_are_recovered():
    event = make_event()
    OutboxEvent.objects.filter(id=event.id).update(
        status=OutboxEvent.Status.PROCESSING,
        claim_expires_at=timezone.now() - timedelta(seconds=1),
    )
    summary = dispatch.dispatch_outbox_events(transport=RecordingTransport(), limit=0)
    assert summary["recovered"] == 0  # limit 0 does nothing
    summary = dispatch.dispatch_outbox_events(transport=RecordingTransport(), limit=5)
    event.refresh_from_db()
    assert summary["recovered"] == 1
    assert event.status == OutboxEvent.Status.PENDING
    assert event.recoveries == 1


def test_prune_keeps_recent_rows_and_undelivered_author_erased():
    old = timezone.now() - timedelta(days=40)
    published_old = make_event()
    published_new = make_event()
    dead_old = make_event()
    erased_dead = make_event(AUTHOR_ERASED, payload={"author_id": "u1"})
    OutboxEvent.objects.filter(id=published_old.id).update(
        status=OutboxEvent.Status.PUBLISHED, published_at=old
    )
    OutboxEvent.objects.filter(id=published_new.id).update(
        status=OutboxEvent.Status.PUBLISHED, published_at=timezone.now()
    )
    OutboxEvent.objects.filter(id__in=[dead_old.id, erased_dead.id]).update(
        status=OutboxEvent.Status.DEAD_LETTER
    )
    OutboxEvent.objects.filter(id__in=[dead_old.id, erased_dead.id]).update(updated_at=old)
    out = io.StringIO()
    call_command(
        "prune_outbox_events",
        "--published-older-than",
        "30",
        "--dead-letter-older-than",
        "30",
        stdout=out,
    )
    assert json.loads(out.getvalue()) == {"dead_letter_deleted": 1, "published_deleted": 1}
    remaining = set(OutboxEvent.objects.values_list("id", flat=True))
    assert remaining == {published_new.id, erased_dead.id}


def test_dispatch_command_prints_a_summary(settings):
    make_event()
    out = io.StringIO()
    call_command("dispatch_outbox_events", stdout=out)
    assert json.loads(out.getvalue())["published"] == 1
