"""Row locks in the one documented order (docs/spec.md section 12):

    ApplicationSettings → Entity → Review → ReviewMedia (ascending id within a type)

Every multi-row write goes through these helpers, so two transactions can never wait on each
other in opposite orders.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import transaction

from octoreview.core.db import holding_locks
from octoreview.core.errors import NotFoundError
from octoreview.entities.models import Entity
from octoreview.reviews.models import Review


@contextmanager
def locked_entity(entity_id) -> Iterator[Entity]:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("locked_entity must run inside transaction.atomic()")
    entity = Entity.objects.select_for_update().get(pk=entity_id)
    with holding_locks():
        yield entity


@contextmanager
def locked_review(review_id) -> Iterator[tuple[Entity, Review]]:
    """Lock a review's entity, then the review itself."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("locked_review must run inside transaction.atomic()")
    entity_id = Review.objects.filter(pk=review_id).values_list("entity_id", flat=True).first()
    if entity_id is None:
        raise NotFoundError()
    entity = Entity.objects.select_for_update().get(pk=entity_id)
    review = Review.objects.select_for_update().get(pk=review_id)
    with holding_locks():
        yield entity, review
