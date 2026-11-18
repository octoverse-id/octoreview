from __future__ import annotations

import json
import logging
import re

CONTEXT_FIELDS = (
    "request_id",
    "operation_id",
    "tenant_id",
    "namespace_type",
    "method",
    "route",
    "status_code",
    "error_code",
    "duration_ms",
    "event",
)

# Signed local media tokens and presigned query parameters must never reach a log line
# (docs/spec.md section 16). Scrubbing happens on the final text, so exception messages from
# storage SDKs are covered too.
_TOKEN_PATH = re.compile(r"(/media-(?:uploads|files)/)[^/\s?\"'\\]+")
_SIGNED_PARAM = re.compile(
    r"((?:X-Amz-[A-Za-z-]+|X-Goog-[A-Za-z-]+|sig|Signature|se|sp|sv|sr|skoid|sktid)=)[^&\s\"'\\]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-.]+", re.IGNORECASE)
_SERVICE_KEY = re.compile(r"\bocr_[0-9a-f]{8}_[A-Za-z0-9_\-]+")


def scrub(text: str) -> str:
    text = _TOKEN_PATH.sub(r"\1<redacted>", text)
    text = _SIGNED_PARAM.sub(r"\1<redacted>", text)
    text = _BEARER.sub(r"\1<redacted>", text)
    return _SERVICE_KEY.sub("ocr_<redacted>", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        metric = getattr(record, "metric", None)
        if metric is not None:
            payload["metric"] = metric
            payload.update(getattr(record, "metric_fields", {}) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Scrub values before encoding: scrubbing JSON text could cut an escape sequence.
        return json.dumps(_scrub_values(payload), default=str)


def _scrub_values(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: _scrub_values(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_values(item) for item in value]
    return value
