"""Metrics as structured log events (docs/spec.md section 16).

Octoreview ships no metrics backend: each `emit_metric` call is one JSON log line
`{"metric": "<name>", ...fields}` that the log pipeline aggregates. Keep names stable;
dashboards and alerts key off them.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("octoreview.metrics")

OUTBOX_DISPATCH_SUMMARY = "outbox_dispatch_summary"
OUTBOX_DEAD_LETTER_COUNT = "outbox.dead_letter_count"


def emit_metric(name: str, **fields) -> None:
    logger.info(name, extra={"metric": name, "metric_fields": fields})
