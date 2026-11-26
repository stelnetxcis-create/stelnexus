"""Sliding-window rate limiting (login, bet placement, cash desk, API).

With ``REDIS_URL`` set the counters live in Redis, so the limit holds across every web worker. Without it (one worker) or
while Redis is unreachable they live in this process's memory: the call sites are the same either way.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque

from app.core import redisbus
from app.core.config import get_settings
from app.core.errors import RateLimited

_settings = get_settings()
_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)
_MAX_KEYS = 20000


def _too_many(retry: int) -> RateLimited:
    return RateLimited(f"Too many attempts. Try again in {max(1, retry)}s.")


def _redis_check(key: str, limit: int, window: int):
    """True = allowed, an int = refused (seconds until the oldest hit leaves the window), None = Redis unavailable."""
    now = time.time()
    member = f"{now:.6f}:{uuid.uuid4().hex[:8]}"
    rkey = redisbus.key("rl", key)

    def op(r):
        pipe = r.pipeline(transaction=True)
        pipe.zremrangebyscore(rkey, 0, now - window)
        pipe.zadd(rkey, {member: now})
        pipe.zcard(rkey)
        pipe.zrange(rkey, 0, 0, withscores=True)
        pipe.expire(rkey, window + 5)
        res = pipe.execute()
        if res[2] > limit:                                    # this hit is the one too many: take it back
            r.zrem(rkey, member)
            oldest = res[3][0][1] if res[3] else now
            return int(oldest + window - now)
        return True
    return redisbus.call(op, None)


def check(key: str, limit: int, window_seconds: int) -> None:
    if not _settings.RATE_LIMIT_ENABLED:
        return
    if redisbus.enabled():
        verdict = _redis_check(key, limit, window_seconds)
        if verdict is True:
            return
        if verdict is not None:
            raise _too_many(verdict)
        # Redis is down: fall through to this process's own counters (stricter than nothing)
    _local_check(key, limit, window_seconds)


def _local_check(key: str, limit: int, window_seconds: int) -> None:
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
            raise _too_many(int(bucket[0] + window_seconds - now))
        bucket.append(now)


def reset(key: str) -> None:
    with _lock:
        _hits.pop(key, None)
    if redisbus.enabled():
        redisbus.call(lambda r: r.delete(redisbus.key("rl", key)))


def login_key(ip: str | None, username: str) -> str:
    return f"login:{ip or 'unknown'}:{username.lower()[:32]}"
