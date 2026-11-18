"""Rating aggregates (docs/spec.md sections 3.2, 4.1 and 5.1).

`apply_delta` is the only writer of an entity's rating columns: review transitions, erasure and
`recompute_ratings` all route through it, and `make check` greps for other writers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F

from octoreview.entities.models import Entity

HISTOGRAM_FIELDS = {star: f"rating_{star}_count" for star in range(1, 6)}


@dataclass(frozen=True)
class Contribution:
    """What one published review adds to its entity's aggregates."""

    rating: int
    verified: bool


def apply_delta(
    entity_id, *, remove: Contribution | None = None, add: Contribution | None = None
) -> bool:
    """Move one review's contribution; returns whether the aggregates changed.

    The caller holds the entity row lock inside its transaction. F() keeps the update correct
    even if a future caller forgets, and the lock keeps the event ordering deterministic.
    """

    if remove == add:
        return False
    deltas: dict[str, int] = {}

    def bump(field: str, amount: int) -> None:
        deltas[field] = deltas.get(field, 0) + amount

    for contribution, sign in ((remove, -1), (add, 1)):
        if contribution is None:
            continue
        if contribution.rating not in HISTOGRAM_FIELDS:
            raise ValueError(f"rating must be 1-5, got {contribution.rating!r}")
        bump("rating_count", sign)
        bump("rating_sum", sign * contribution.rating)
        bump(HISTOGRAM_FIELDS[contribution.rating], sign)
        if contribution.verified:
            bump("verified_review_count", sign)
    changes = {field: F(field) + amount for field, amount in deltas.items() if amount}
    if not changes:
        return False
    Entity.objects.filter(pk=entity_id).update(**changes)
    return True


AGGREGATE_FIELDS = (
    "rating_count",
    "rating_sum",
    *HISTOGRAM_FIELDS.values(),
    "verified_review_count",
)


def aggregates_from(contributions) -> dict[str, int]:
    """Aggregate column values for a set of published-review contributions."""

    values = dict.fromkeys(AGGREGATE_FIELDS, 0)
    for item in contributions:
        values["rating_count"] += 1
        values["rating_sum"] += item.rating
        values[HISTOGRAM_FIELDS[item.rating]] += 1
        if item.verified:
            values["verified_review_count"] += 1
    return values


def current_aggregates(entity: Entity) -> dict[str, int]:
    return {field: getattr(entity, field) for field in AGGREGATE_FIELDS}


def replace_aggregates(entity_id, values: dict[str, int]) -> None:
    """Repair path for `recompute_ratings`: overwrite with recomputed values (caller locks)."""

    Entity.objects.filter(pk=entity_id).update(
        **{field: values[field] for field in AGGREGATE_FIELDS}
    )


def _round(value: Decimal, places: str) -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def summary(entity: Entity | None) -> dict:
    """The rating summary shape (docs/spec.md section 5.1); zeros for an unknown entity."""

    histogram = {
        str(star): (getattr(entity, field) if entity is not None else 0)
        for star, field in HISTOGRAM_FIELDS.items()
    }
    count = entity.rating_count if entity is not None else 0
    if count:
        average = _round(Decimal(entity.rating_sum) / Decimal(count), "0.01")
        percent = {
            star: _round(Decimal(value) * 100 / Decimal(count), "0.1")
            for star, value in histogram.items()
        }
    else:
        average = None
        percent = {star: 0.0 for star in histogram}
    return {
        "rating_count": count,
        "rating_average": average,
        "histogram": histogram,
        "histogram_percent": percent,
        "verified_review_count": entity.verified_review_count if entity is not None else 0,
    }
