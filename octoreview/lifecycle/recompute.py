"""Rebuild entity aggregates and reviews' ready_media_count (docs/spec.md section 14).

Works in batches by entity id; each batch reads in its own snapshot so a large table never holds
back vacuum, and a repair re-reads under the entity lock before writing.
"""

from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import Count, Q

from octoreview.entities import ratings
from octoreview.entities.models import Entity
from octoreview.entities.ratings import Contribution
from octoreview.lifecycle.locks import locked_entity, locked_review
from octoreview.media.models import VISIBLE_MODERATION, MediaStatus, ReviewMedia
from octoreview.reviews.models import Review, ReviewStatus

BATCH_SIZE = 500


def _expected(entity_ids) -> dict:
    contributions = defaultdict(list)
    rows = Review.objects.filter(
        entity_id__in=entity_ids, status=ReviewStatus.PUBLISHED
    ).values_list("entity_id", "rating", "is_verified_purchase")
    for entity_id, rating, verified in rows:
        contributions[entity_id].append(Contribution(rating, verified))
    return {
        entity_id: ratings.aggregates_from(contributions[entity_id]) for entity_id in entity_ids
    }


def recompute_ratings(*, repair: bool) -> list[dict]:
    """Return one record per mismatched entity or review; repair them when asked."""

    return _entity_mismatches(repair) + _media_count_mismatches(repair)


def _visible_media(review_id) -> int:
    return ReviewMedia.objects.filter(
        review_id=review_id, status=MediaStatus.READY, moderation_status__in=VISIBLE_MODERATION
    ).count()


def _media_count_mismatches(repair: bool) -> list[dict]:
    visible = Count(
        "media",
        filter=Q(media__status=MediaStatus.READY, media__moderation_status__in=VISIBLE_MODERATION),
    )
    mismatches: list[dict] = []
    last_id = None
    while True:
        queryset = Review.objects.order_by("id")
        if last_id is not None:
            queryset = queryset.filter(id__gt=last_id)
        batch = list(
            queryset.annotate(expected=visible).values_list("id", "ready_media_count", "expected")[
                :BATCH_SIZE
            ]
        )
        if not batch:
            return mismatches
        last_id = batch[-1][0]
        for review_id, stored, expected in batch:
            if stored == expected:
                continue
            mismatches.append(
                {
                    "review_id": str(review_id),
                    "stored": {"ready_media_count": stored},
                    "expected": {"ready_media_count": expected},
                }
            )
            if repair:
                # Recount under the lock: media may have moved since the batch read.
                with transaction.atomic(), locked_review(review_id) as (_entity, review):
                    review.ready_media_count = _visible_media(review_id)
                    review.save(update_fields=["ready_media_count", "updated_at"])


def _entity_mismatches(repair: bool) -> list[dict]:
    mismatches: list[dict] = []
    last_id = None
    while True:
        queryset = Entity.objects.order_by("id")
        if last_id is not None:
            queryset = queryset.filter(id__gt=last_id)
        batch = list(queryset[:BATCH_SIZE])
        if not batch:
            return mismatches
        last_id = batch[-1].id
        expected = _expected([entity.id for entity in batch])
        for entity in batch:
            if ratings.current_aggregates(entity) == expected[entity.id]:
                continue
            mismatches.append(
                {
                    "entity_id": str(entity.id),
                    "stored": ratings.current_aggregates(entity),
                    "expected": expected[entity.id],
                }
            )
            if repair:
                with transaction.atomic(), locked_entity(entity.id):
                    ratings.replace_aggregates(entity.id, _expected([entity.id])[entity.id])
