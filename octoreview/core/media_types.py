"""The six allowed media types (docs/spec.md section 6.2): one table, read by every module."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaType:
    content_type: str
    kind: str  # "image" | "video"
    extension: str


MEDIA_TYPES: dict[str, MediaType] = {
    entry.content_type: entry
    for entry in (
        MediaType("image/jpeg", "image", "jpg"),
        MediaType("image/png", "image", "png"),
        MediaType("image/webp", "image", "webp"),
        MediaType("video/mp4", "video", "mp4"),
        MediaType("video/webm", "video", "webm"),
        MediaType("video/quicktime", "video", "mov"),
    )
}

ALL_CONTENT_TYPES = tuple(MEDIA_TYPES)
KINDS = ("image", "video")
