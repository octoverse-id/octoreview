"""Metrics as structured log events (docs/spec.md section 16).

Octoreview ships no metrics backend: each `emit_metric` call is one JSON log line
`{"metric": "<name>", ...fields}` that the log pipeline aggregates. Keep names stable;
dashboards and alerts key off them (docs/operations.md lists every metric and its fields).
Fields carry scope ids, kinds, reasons and counts, never personal data.
"""

from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger("octoreview.metrics")

OUTBOX_DISPATCH_SUMMARY = "outbox_dispatch_summary"
OUTBOX_DEAD_LETTER_COUNT = "outbox.dead_letter_count"
OUTBOX_LAG_SECONDS = "outbox.lag_seconds"
REVIEW_CREATED = "review.created"
REVIEW_PUBLISHED = "review.published"
REVIEW_REJECTED = "review.rejected"
MEDIA_INTENT = "media.intent"
MEDIA_READY = "media.ready"
MEDIA_REJECTED = "media.rejected"
QUOTA_REJECTED = "quota.rejected"
STORAGE_CALL = "storage.call"
CLEANUP_DELETED = "cleanup_media.deleted"
CLEANUP_FAILED = "cleanup_media.failed"
ERASE_COMPLETED = "erase.completed"


def emit_metric(name: str, **fields) -> None:
    logger.info(name, extra={"metric": name, "metric_fields": fields})


def emit_on_commit(name: str, **fields) -> None:
    """Emit once the surrounding transaction commits, so rolled-back work is never counted."""

    transaction.on_commit(lambda: emit_metric(name, **fields))
