"""Rebuild entity aggregates from published reviews (docs/spec.md section 14).

Works in batches by entity id; each batch reads in its own snapshot so a large table never holds
back vacuum, and a repair re-reads under the entity lock before writing.
"""

from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from octoreview.entities import ratings
from octoreview.entities.models import Entity
from octoreview.entities.ratings import Contribution
from octoreview.lifecycle.locks import locked_entity
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
    """Return one record per mismatched entity; repair them when asked."""

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
