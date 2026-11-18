"""Shared input rules (docs/spec.md sections 1 and 3)."""

from __future__ import annotations

import json
import re

ENTITY_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_TEXT_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

JSON_MAX_BYTES = 16 * 1024
JSON_MAX_DEPTH = 5


def external_id_error(value) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 255:
        return "Must be 1-255 characters."
    if _CONTROL_CHARS.search(value):
        return "Control characters are not allowed."
    return None


def entity_type_error(value) -> str | None:
    if not isinstance(value, str) or not ENTITY_TYPE_PATTERN.fullmatch(value):
        return "Must match ^[a-z0-9][a-z0-9_.-]{0,99}$."
    return None


def plain_text_error(value: str) -> str | None:
    """Review text is plain text: no NUL or C0 controls except newline and tab."""

    if _TEXT_FORBIDDEN.search(value):
        return "Control characters other than newline and tab are not allowed."
    return None


def _depth(value, level: int = 1) -> int:
    if isinstance(value, dict):
        return max((_depth(item, level + 1) for item in value.values()), default=level)
    if isinstance(value, list):
        return max((_depth(item, level + 1) for item in value), default=level)
    return level - 1 if level > 1 else level


def json_object_error(value) -> str | None:
    """JSON columns hold an object of at most 16 KiB and 5 levels (docs/spec.md section 3)."""

    if not isinstance(value, dict):
        return "Must be a JSON object."
    if len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > JSON_MAX_BYTES:
        return f"Must be at most {JSON_MAX_BYTES} bytes when encoded."
    if _depth(value) > JSON_MAX_DEPTH:
        return f"Must be at most {JSON_MAX_DEPTH} levels deep."
    return None
