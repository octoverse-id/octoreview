"""In-process cache of signed read URLs, per signing epoch (docs/spec.md section 8).

Every read URL is signed to outlive its epoch by max(TTL/2, max-age) (`etags.url_expiry_target`),
so a URL reused only within its epoch is as fresh as one signed for the request. The key carries
the epoch and the deployment fingerprint; the audience is decided per request before the lookup,
so a visibility change never reaches a URL signed for another audience.

Bounded LRU, one per worker process; gunicorn's gthread workers share it across threads.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime

from django.conf import settings

from octoreview.core import etags

_entries: OrderedDict = OrderedDict()
_lock = threading.Lock()


def key(storage_key: str, media_id, audience: str, now: datetime) -> tuple:
    return (etags.deployment(), etags.signing_epoch(now), storage_key, str(media_id), audience)


def get(cache_key: tuple) -> dict | None:
    if settings.MEDIA_URL_CACHE_SIZE <= 0:
        return None
    with _lock:
        value = _entries.get(cache_key)
        if value is not None:
            _entries.move_to_end(cache_key)
    # A copy: callers add the URL to a representation they may change.
    return dict(value) if value is not None else None


def put(cache_key: tuple, value: dict) -> None:
    with _lock:
        _entries[cache_key] = dict(value)
        _entries.move_to_end(cache_key)
        # A size of 0 empties the cache here, so it never holds an entry.
        while len(_entries) > settings.MEDIA_URL_CACHE_SIZE:
            _entries.popitem(last=False)


def clear() -> None:
    with _lock:
        _entries.clear()


def size() -> int:
    with _lock:
        return len(_entries)
