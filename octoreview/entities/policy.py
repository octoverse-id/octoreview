"""Effective media policy for a review (docs/spec.md section 6.1).

Each field resolves on its own: entity override, then the application settings, then the server
default. Ceilings bound every value at write time, so a resolved value is always within them.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from octoreview.core.media_types import ALL_CONTENT_TYPES
from octoreview.entities.models import POLICY_FIELDS, ApplicationSettings, Entity


@dataclass(frozen=True)
class MediaPolicy:
    max_images: int
    max_videos: int
    max_image_bytes: int
    max_video_bytes: int
    allowed_content_types: tuple[str, ...]
    sources: dict[str, str]

    def limit_for(self, kind: str) -> int:
        return self.max_images if kind == "image" else self.max_videos

    def max_bytes_for(self, kind: str) -> int:
        return self.max_image_bytes if kind == "image" else self.max_video_bytes


def effective_policy(entity: Entity, app_settings: ApplicationSettings | None) -> MediaPolicy:
    values: dict[str, int] = {}
    sources: dict[str, str] = {}
    for field in POLICY_FIELDS:
        entity_value = getattr(entity, field)
        app_value = getattr(app_settings, field, None) if app_settings is not None else None
        if entity_value is not None:
            values[field], sources[field] = entity_value, "entity"
        elif app_value is not None:
            values[field], sources[field] = app_value, "application"
        else:
            values[field], sources[field] = settings.MEDIA_POLICY_DEFAULTS[field], "server"
    allowed = (
        tuple(app_settings.allowed_content_types)
        if app_settings is not None and app_settings.allowed_content_types is not None
        else ALL_CONTENT_TYPES
    )
    return MediaPolicy(**values, allowed_content_types=allowed, sources=sources)


def ceiling_errors(values: dict) -> dict[str, list[str]]:
    """Field errors for any policy value above its ceiling (400 validation_error)."""

    errors: dict[str, list[str]] = {}
    for field in POLICY_FIELDS:
        value = values.get(field)
        if value is None:
            continue
        ceiling = settings.MEDIA_POLICY_CEILINGS[field]
        if value > ceiling:
            errors[field] = [f"Ensure this value is at most {ceiling} (the server ceiling)."]
    return errors
