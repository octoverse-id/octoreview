"""Database helpers shared by every domain module."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import IntegrityError

_SQLITE_UNIQUE = re.compile(r"UNIQUE constraint failed: (?P<columns>.+)$")

# Set while a transaction holds row locks taken through a lock-order helper. Storage calls assert
# it is unset: no network call may run under a row lock (docs/spec.md section 12).
_LOCKS_HELD: ContextVar[bool] = ContextVar("octoreview_locks_held", default=False)


@contextmanager
def holding_locks() -> Iterator[None]:
    token = _LOCKS_HELD.set(True)
    try:
        yield
    finally:
        _LOCKS_HELD.reset(token)


def locks_held() -> bool:
    return _LOCKS_HELD.get()


def unique_violation(exc: IntegrityError) -> str | None:
    """Identify which unique constraint an IntegrityError broke, on PostgreSQL and SQLite.

    PostgreSQL reports the constraint name (`diag.constraint_name`). SQLite reports only the
    column list (`UNIQUE constraint failed: reviews.entity_id, reviews.author_id`), returned as
    `columns:<list>`. Anything else (FK, CHECK) returns None, so callers never mistake it for a
    duplicate (docs/spec.md section 3.3).
    """

    cause = exc.__cause__
    diag = getattr(cause, "diag", None)
    sqlstate = getattr(cause, "sqlstate", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        return diag.constraint_name if sqlstate in (None, "23505") else None
    match = _SQLITE_UNIQUE.search(str(cause or exc))
    if match:
        return "columns:" + match.group("columns").replace(" ", "")
    return None
