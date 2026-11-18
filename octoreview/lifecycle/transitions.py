"""The review transition table (docs/spec.md section 4.1).

Views, services and the parametrized test all read this one table; any pair it omits is a 409
`invalid_transition` naming the actions that are allowed from that status.
"""

from __future__ import annotations

from octoreview.core.errors import DomainError
from octoreview.reviews.models import ReviewStatus as S

PUBLISH, REJECT, HIDE, REMOVE, EDIT = "publish", "reject", "hide", "remove", "edit"

REVIEW_TRANSITIONS: dict[tuple[str, str], str] = {
    (S.PENDING, PUBLISH): S.PUBLISHED,
    (S.PENDING, REJECT): S.REJECTED,
    (S.PUBLISHED, HIDE): S.HIDDEN,
    (S.HIDDEN, PUBLISH): S.PUBLISHED,
    (S.REJECTED, PUBLISH): S.PUBLISHED,
    (S.PENDING, REMOVE): S.REMOVED,
    (S.PUBLISHED, REMOVE): S.REMOVED,
    (S.REJECTED, REMOVE): S.REMOVED,
    (S.HIDDEN, REMOVE): S.REMOVED,
}

# Content edits are not status changes, but they follow the same "allowed from" rule.
EDITABLE_STATUSES = frozenset({S.PENDING, S.PUBLISHED})


def allowed_actions(status: str) -> list[str]:
    actions = sorted(action for (source, action) in REVIEW_TRANSITIONS if source == status)
    if status in EDITABLE_STATUSES:
        actions.append(EDIT)
    return sorted(actions)


def invalid_transition(status: str, action: str) -> DomainError:
    return DomainError(
        f"A {status} review cannot {action}; use one of the allowed actions.",
        {"from_status": status, "action": action, "allowed_actions": allowed_actions(status)},
        code="invalid_transition",
    )


def next_status(status: str, action: str) -> str:
    target = REVIEW_TRANSITIONS.get((status, action))
    if target is None:
        raise invalid_transition(status, action)
    return target
