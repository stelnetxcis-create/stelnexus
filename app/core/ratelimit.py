"""Sliding-window rate limiting (login, bet placement, cash desk, API).

Single-process in-memory bucket. For a multi-worker deployment put Redis behind
the same interface -- the call sites do not change.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.core.config import get_settings
from app.core.errors import RateLimited

_settings = get_settings()
_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)
_MAX_KEYS = 20000


def check(key: str, limit: int, window_seconds: int) -> None:
    if not _settings.RATE_LIMIT_ENABLED:
        return
    now = time.monotonic()
    with _lock:
        if len(_hits) > _MAX_KEYS:  # crude eviction, keeps memory bounded
            for stale in [k for k, v in _hits.items() if not v or now - v[-1] > window_seconds]:
                _hits.pop(stale, None)
        bucket = _hits[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry = max(1, int(bucket[0] + window_seconds - now))
            raise RateLimited(f"Too many attempts. Try again in {retry}s.")
        bucket.append(now)


def reset(key: str) -> None:
    with _lock:
        _hits.pop(key, None)


def login_key(ip: str | None, username: str) -> str:
    return f"login:{ip or 'unknown'}:{username.lower()[:32]}"
