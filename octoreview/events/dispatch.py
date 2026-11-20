"""Outbox delivery (docs/spec.md section 10). Ported from octonomy `events/dispatch.py`.

Differences from octonomy, each required by the spec:
- the logging transport logs the envelope only, never the payload;
- webhooks are signed `t=<unix>,v1=<hmac(secret, t + "." + body)>` so captured deliveries
  cannot be replayed outside the tolerance window;
- `retry_failed` also replays dead letters;
- a delivered `author.erased` row is hard-deleted at once, because it is the one event that
  carries a raw author id;
- there is no namespace write kill-switch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Protocol
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from octoreview.core.metrics import (
    OUTBOX_DEAD_LETTER_COUNT,
    OUTBOX_DISPATCH_SUMMARY,
    OUTBOX_LAG_SECONDS,
    emit_metric,
)
from octoreview.events.models import OutboxEvent
from octoreview.events.services import AUTHOR_ERASED

logger = logging.getLogger(__name__)


class EventTransport(Protocol):
    def publish(self, event: OutboxEvent) -> None: ...


def event_envelope(event: OutboxEvent) -> dict:
    """The delivered document: envelope fields plus the thin `data` payload."""

    return {
        "id": str(event.id),
        "type": event.event_type,
        "occurred_at": event.created_at.isoformat() if event.created_at else None,
        "tenant_id": event.tenant_id,
        "application_id": event.application_id,
        "namespace_type": event.namespace_type,
        "namespace_id": event.namespace_id,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "request_id": event.request_id,
        "data": event.payload,
    }


class LoggingEventTransport:
    """Development transport. Logs who/what/when, never the payload (docs/spec.md section 10)."""

    def publish(self, event: OutboxEvent) -> None:
        logger.info(
            "outbox_event_published",
            extra={
                "event": {
                    "id": str(event.id),
                    "type": event.event_type,
                    "tenant_id": event.tenant_id,
                    "application_id": event.application_id,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                    "occurred_at": event.created_at.isoformat() if event.created_at else None,
                }
            },
        )


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    # A redirect would re-send a signed body to a host the operator never configured.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def webhook_signature(secret: str, timestamp: int, body: bytes) -> str:
    signed = str(timestamp).encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class WebhookEventTransport:
    def __init__(self, *, url: str, secret: str, timeout_seconds: int, opener=None, clock=None):
        parsed = urllib_parse.urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ImproperlyConfigured(
                "OCTOREVIEW_WEBHOOK_URL must be an absolute http(s) URL; fix the value."
            )
        self.url = url
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.opener = opener or urllib_request.build_opener(_NoRedirectHandler)
        self.clock = clock or time.time

    def publish(self, event: OutboxEvent) -> None:
        body = json.dumps(
            event_envelope(event), default=str, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "octoreview-outbox",
            "X-Octoreview-Event": event.event_type,
            "X-Octoreview-Delivery": str(event.id),
            "X-Octoreview-Signature": webhook_signature(self.secret, int(self.clock()), body),
        }
        request = urllib_request.Request(self.url, data=body, headers=headers, method="POST")
        with self.opener.open(request, timeout=self.timeout_seconds) as response:
            status_code = getattr(response, "status", None)
            if status_code is None:
                status_code = response.getcode()
            if status_code < 200 or status_code >= 300:
                raise RuntimeError(f"webhook returned HTTP {status_code}")


def transport_from_settings() -> EventTransport:
    if settings.OUTBOX_TRANSPORT == "webhook":
        return WebhookEventTransport(
            url=settings.OUTBOX_WEBHOOK_URL,
            secret=settings.OUTBOX_WEBHOOK_SECRET,
            timeout_seconds=settings.OUTBOX_WEBHOOK_TIMEOUT_SECONDS,
        )
    return LoggingEventTransport()


def dispatch_outbox_events(
    *,
    limit: int = 100,
    retry_failed: bool = False,
    transport: EventTransport | None = None,
) -> dict[str, int]:
    transport = transport or transport_from_settings()
    summary = {"published": 0, "failed": 0, "dead_lettered": 0, "recovered": 0}
    # One delivery attempt per event per run: a replayed event that fails again goes back to
    # `failed` with a future available_at, and must not be claimed again in the same loop.
    attempted: set = set()
    for _ in range(max(0, limit)):
        event = _claim_next_event(retry_failed=retry_failed, exclude=attempted)
        if event is None:
            break
        attempted.add(event.id)
        result = _publish_claimed_event(event, transport)
        if result is not None:
            summary[result] += 1
    summary["recovered"] = _recover_expired_claims(limit=max(0, limit))
    _emit_dispatch_metrics(summary)
    return summary


def _skip_locked(queryset):
    if getattr(connection.features, "has_select_for_update_skip_locked", False):
        return queryset.select_for_update(skip_locked=True)
    return queryset


def _next_event(*, retry_failed: bool, exclude: set) -> OutboxEvent | None:
    statuses = [OutboxEvent.Status.PENDING]
    if retry_failed:
        statuses += [OutboxEvent.Status.FAILED, OutboxEvent.Status.DEAD_LETTER]
    queryset = OutboxEvent.objects.filter(status__in=statuses).exclude(id__in=exclude)
    if not retry_failed:
        queryset = queryset.filter(available_at__lte=timezone.now())
    return _skip_locked(queryset.order_by("available_at", "created_at", "id")).first()


def _claim_next_event(*, retry_failed: bool, exclude: set) -> OutboxEvent | None:
    with transaction.atomic():
        event = _next_event(retry_failed=retry_failed, exclude=exclude)
        if event is None:
            return None
        if event.status == OutboxEvent.Status.DEAD_LETTER:
            # An operator replay gives a dead letter a fresh retry budget.
            event.attempts = 0
        event.status = OutboxEvent.Status.PROCESSING
        event.claim_id = uuid.uuid4()
        event.claimed_at = timezone.now()
        event.claim_expires_at = event.claimed_at + timedelta(
            seconds=settings.OUTBOX_CLAIM_TIMEOUT_SECONDS
        )
        event.save(
            update_fields=[
                "status",
                "attempts",
                "claim_id",
                "claimed_at",
                "claim_expires_at",
                "updated_at",
            ]
        )
        return event


def _claimed_event(event_id, claim_id) -> OutboxEvent | None:
    queryset = OutboxEvent.objects.filter(
        id=event_id, claim_id=claim_id, status=OutboxEvent.Status.PROCESSING
    )
    return _skip_locked(queryset).first()


def _publish_claimed_event(event: OutboxEvent, transport: EventTransport) -> str | None:
    claim_id = event.claim_id
    try:
        transport.publish(event)
    except Exception as exc:
        with transaction.atomic():
            claimed = _claimed_event(event.id, claim_id)
            if claimed is None:
                logger.warning("outbox_claim_lost", extra={"event": {"id": str(event.id)}})
                return None
            return _mark_failed(claimed, exc)

    with transaction.atomic():
        claimed = _claimed_event(event.id, claim_id)
        if claimed is None:
            logger.error("outbox_delivered_but_claim_lost", extra={"event": {"id": str(event.id)}})
            return None
        if claimed.event_type == AUTHOR_ERASED:
            # The one event with a raw author id is removed the moment it is delivered.
            claimed.delete()
        else:
            _mark_published(claimed)
    return "published"


def _clear_claim(event: OutboxEvent) -> None:
    event.claim_id = None
    event.claimed_at = None
    event.claim_expires_at = None


CLAIM_FIELDS = ["claim_id", "claimed_at", "claim_expires_at", "updated_at"]


def _mark_published(event: OutboxEvent) -> None:
    event.status = OutboxEvent.Status.PUBLISHED
    event.attempts += 1
    event.last_error = ""
    event.published_at = timezone.now()
    _clear_claim(event)
    event.save(update_fields=["status", "attempts", "last_error", "published_at", *CLAIM_FIELDS])


def retry_delay_seconds(attempt_number: int) -> int:
    base = settings.OUTBOX_RETRY_BASE_SECONDS
    return min(settings.OUTBOX_RETRY_MAX_SECONDS, base * (2 ** max(attempt_number - 1, 0)))


def _mark_failed(event: OutboxEvent, exc: Exception) -> str:
    event.attempts += 1
    # Transport errors can echo URLs; keep the message short and never log the payload.
    event.last_error = str(exc)[:500]
    _clear_claim(event)
    if event.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
        event.status = OutboxEvent.Status.DEAD_LETTER
        event.save(update_fields=["status", "attempts", "last_error", *CLAIM_FIELDS])
        return "dead_lettered"
    event.status = OutboxEvent.Status.FAILED
    event.available_at = timezone.now() + timedelta(seconds=retry_delay_seconds(event.attempts))
    event.save(update_fields=["status", "attempts", "last_error", "available_at", *CLAIM_FIELDS])
    return "failed"


def _recover_expired_claims(*, limit: int) -> int:
    recovered = 0
    for _ in range(limit):
        with transaction.atomic():
            queryset = OutboxEvent.objects.filter(
                status=OutboxEvent.Status.PROCESSING, claim_expires_at__lte=timezone.now()
            ).order_by("claim_expires_at", "created_at", "id")
            event = _skip_locked(queryset).first()
            if event is None:
                break
            # The worker vanished mid-flight; the delivery outcome is unknown, so redeliver.
            event.recoveries += 1
            event.last_error = "outbox claim expired before publish completed"
            event.status = OutboxEvent.Status.PENDING
            event.available_at = timezone.now() + timedelta(
                seconds=retry_delay_seconds(max(event.recoveries, event.attempts))
            )
            _clear_claim(event)
            event.save(
                update_fields=["status", "recoveries", "last_error", "available_at", *CLAIM_FIELDS]
            )
        recovered += 1
    return recovered


def _emit_dispatch_metrics(summary: dict[str, int]) -> None:
    now = timezone.now()
    deliverable = Q(
        status__in=[OutboxEvent.Status.PENDING, OutboxEvent.Status.FAILED], available_at__lte=now
    )
    stuck = Q(status=OutboxEvent.Status.PROCESSING, claim_expires_at__lte=now)
    backlog = OutboxEvent.objects.filter(deliverable | stuck).aggregate(
        count=Count("id"), oldest=Min("available_at")
    )
    lag_seconds = round((now - backlog["oldest"]).total_seconds(), 2) if backlog["oldest"] else 0
    emit_metric(
        OUTBOX_DISPATCH_SUMMARY, **summary, backlog=backlog["count"], lag_seconds=lag_seconds
    )
    # The alerting signal on its own (docs/operations.md: lag over 10 minutes).
    emit_metric(OUTBOX_LAG_SECONDS, seconds=lag_seconds, backlog=backlog["count"])
    emit_metric(
        OUTBOX_DEAD_LETTER_COUNT,
        count=OutboxEvent.objects.filter(status=OutboxEvent.Status.DEAD_LETTER).count(),
    )


def prune_outbox_events(
    *, published_older_than_days: int, dead_letter_older_than_days: int
) -> dict:
    """Delete published and dead-letter rows past their retention, in batches.

    An undelivered `author.erased` is never pruned: deleting it would mean downstream consumers
    are never told to purge their copies (docs/spec.md section 10).
    """

    now = timezone.now()
    published_cutoff = now - timedelta(days=published_older_than_days)
    dead_cutoff = now - timedelta(days=dead_letter_older_than_days)
    published = _delete_in_batches(
        OutboxEvent.objects.filter(
            status=OutboxEvent.Status.PUBLISHED, published_at__lt=published_cutoff
        )
    )
    dead = _delete_in_batches(
        OutboxEvent.objects.filter(
            status=OutboxEvent.Status.DEAD_LETTER, updated_at__lt=dead_cutoff
        ).exclude(event_type=AUTHOR_ERASED)
    )
    return {"published_deleted": published, "dead_letter_deleted": dead}


def _delete_in_batches(queryset, batch_size: int = 1000) -> int:
    total = 0
    while True:
        ids = list(queryset.order_by("id").values_list("id", flat=True)[:batch_size])
        if not ids:
            return total
        deleted, _ = OutboxEvent.objects.filter(id__in=ids).delete()
        total += deleted
