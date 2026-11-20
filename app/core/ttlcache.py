"""A tiny in-process TTL cache for expensive, read-mostly numbers (dashboard KPIs).

* Per process: with several workers each keeps its own copy, so the worst case is every worker
  recomputing once per TTL. That is the point: a handful of queries per TTL instead of per page view.
* Values are deep-copied on the way in and out, so a request that mutates what it received can never
  corrupt what the next request sees. Only cache plain data (dicts, lists, numbers, strings), never
  ORM objects: those are bound to a session that is closed by the time the next request reads them.
* ``DASHBOARD_CACHE_SECONDS=0`` turns it off.
* ``invalidate_after_commit(db)`` clears a prefix once the surrounding transaction has COMMITTED, so
  a staff action (declaring a result, approving a request) is reflected immediately and a concurrent
  reader can't re-cache the pre-commit state.
"""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Hashable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

_lock = threading.Lock()
_store: dict[Hashable, tuple[float, Any]] = {}
_MAX_ENTRIES = 2000


def cached(key: Hashable, ttl: float, compute: Callable[[], Any]) -> Any:
    """Return the cached value for ``key`` if younger than ``ttl`` seconds, else compute and store it."""
    if ttl <= 0:
        return compute()
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and now - hit[0] < ttl:
            return copy.deepcopy(hit[1])
    value = compute()                       # outside the lock: a slow query must not block other keys
    with _lock:
        if len(_store) >= _MAX_ENTRIES:     # bounded memory: drop everything expired, then the oldest
            for k in [k for k, (t, _) in _store.items() if now - t >= ttl]:
                _store.pop(k, None)
            while len(_store) >= _MAX_ENTRIES:
                _store.pop(min(_store, key=lambda k: _store[k][0]), None)
        _store[key] = (time.monotonic(), copy.deepcopy(value))
    return value


def clear(prefix: str | None = None) -> None:
    """Drop everything, or every key whose first element equals ``prefix``."""
    with _lock:
        if prefix is None:
            _store.clear()
        else:
            for k in [k for k in _store if isinstance(k, tuple) and k and k[0] == prefix]:
                _store.pop(k, None)


def invalidate_after_commit(db: Session, prefix: str = "dashboard") -> None:
    """Clear ``prefix`` when (and only if) this session's transaction commits."""
    db.info.setdefault("ttl_invalidate", set()).add(prefix)


@event.listens_for(Session, "after_commit")
def _flush_invalidations(session: Session) -> None:
    for prefix in session.info.pop("ttl_invalidate", ()):
        clear(prefix)


@event.listens_for(Session, "after_rollback")
def _discard_invalidations(session: Session) -> None:
    session.info.pop("ttl_invalidate", None)
