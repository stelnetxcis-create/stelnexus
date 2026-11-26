"""Optional Redis for several web workers. Everything here is best effort and never raises into a request.

What lives in Redis (and nothing else: no money, no sessions, nothing that must survive a restart):
  * rate-limit counters (core/ratelimit.py),
  * "something changed" wake-ups for the live chat streams, so a message committed by worker A wakes the browsers
    connected to worker B at once instead of at their next 20 s heartbeat (services/realtime.py),
  * cache invalidations (core/ttlcache.py), so a commit clears the live cache in every worker,
  * the chat "is typing" dots.

If REDIS_URL is empty the app behaves exactly like a single process. If Redis is configured but unreachable, a short
circuit breaker sends every caller to its in-process fallback for BREAKER_SECONDS, then tries again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from app.core.config import get_settings

log = logging.getLogger("stelnexus.redis")
_cfg = get_settings()

NODE = uuid.uuid4().hex[:12]        # this process: it ignores its own broadcasts (it already acted locally)
BREAKER_SECONDS = 5.0
_lock = threading.Lock()
_client: Any = None
_down_until = 0.0
_last_warn = 0.0
_async_factory: Callable[[], Any] | None = None      # tests inject a fake async client here


def enabled() -> bool:
    return bool(_cfg.REDIS_URL)


def key(*parts: str) -> str:
    return ":".join((_cfg.REDIS_PREFIX, *parts))


def _failed(exc: BaseException) -> None:
    global _down_until, _last_warn
    now = time.monotonic()
    _down_until = now + BREAKER_SECONDS
    if now - _last_warn > 30:
        _last_warn = now
        log.warning("Redis unavailable (%s): using per-process fallbacks, retrying in %.0fs", type(exc).__name__, BREAKER_SECONDS)


def client() -> Any:
    """The shared sync client, or None when Redis is off, missing or in its cool-down."""
    global _client
    if not enabled() or time.monotonic() < _down_until:
        return None
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    import redis
                    _client = redis.Redis.from_url(
                        _cfg.REDIS_URL, decode_responses=True, socket_timeout=_cfg.REDIS_TIMEOUT,
                        socket_connect_timeout=_cfg.REDIS_TIMEOUT, health_check_interval=30, max_connections=64)
                except Exception as exc:                       # noqa: BLE001 - library missing, bad URL...
                    _failed(exc)
                    return None
    return _client


def call(fn: Callable[[Any], Any], default: Any = None) -> Any:
    """Run ``fn(client)``; any Redis problem trips the breaker and returns ``default`` (the caller falls back)."""
    c = client()
    if c is None:
        return default
    try:
        return fn(c)
    except Exception as exc:                                   # noqa: BLE001
        _failed(exc)
        return default


def reset_for_tests(client_obj: Any = None) -> None:
    global _client, _down_until
    _client, _down_until = client_obj, 0.0


# ------------------------------------------------------------------------------------------------ broadcasts

def publish_topics(topics: Iterable[str]) -> None:
    """Wake the live streams subscribed to these topics in every OTHER process (workers, CLI, timers)."""
    t = sorted(set(topics))
    if t:
        call(lambda r: r.publish(key("wake"), json.dumps({"n": NODE, "t": t})))


def publish_invalidate(prefixes: Iterable[Any]) -> None:
    p = list(prefixes)
    if p:
        call(lambda r: r.publish(key("inv"), json.dumps({"n": NODE, "p": p})))


def handle_message(channel: str, data: str) -> None:
    """What this process does when another one broadcasts. Pure and synchronous: easy to test."""
    try:
        msg = json.loads(data)
    except (TypeError, ValueError):
        return
    if not isinstance(msg, dict) or msg.get("n") == NODE:
        return
    if channel == key("wake"):
        from app.services.realtime import hub
        hub._wake(tuple(str(t) for t in (msg.get("t") or ())))
    elif channel == key("inv"):
        from app.core import ttlcache
        for prefix in msg.get("p") or ():
            ttlcache.clear(prefix, broadcast=False)


async def _listen() -> None:
    """Subscribe for as long as the process lives; reconnect forever (a Redis restart must not need a web restart)."""
    while True:
        r = None
        try:
            if _async_factory is not None:
                r = _async_factory()
            else:
                import redis.asyncio as aioredis
                r = aioredis.from_url(_cfg.REDIS_URL, decode_responses=True, socket_connect_timeout=2, health_check_interval=30)
            ps = r.pubsub()
            await ps.subscribe(key("wake"), key("inv"))
            async for item in ps.listen():
                if item.get("type") == "message":
                    handle_message(str(item.get("channel")), item.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001
            log.warning("Redis subscriber lost (%s); reconnecting in 2s", type(exc).__name__)
            await asyncio.sleep(2)
        finally:
            if r is not None:
                try:
                    await r.aclose()
                except Exception:                              # noqa: BLE001
                    pass


def start_listener() -> asyncio.Task | None:
    """Called from the app's startup (inside the running loop). None when Redis is not configured."""
    if not enabled():
        return None
    return asyncio.get_running_loop().create_task(_listen(), name="redis-subscriber")


# ------------------------------------------------------------------------------------------------ typing dots

_TYPING_SEP = "\x1f"


def typing_touch(user_id: int, name: str, ttl: float) -> bool:
    """Remember who is typing, for every worker. False = Redis not available (caller keeps its local dict)."""
    exp = time.time() + ttl

    def op(r):
        pipe = r.pipeline()
        pipe.zadd(key("typing"), {f"{user_id}{_TYPING_SEP}{name}": exp})
        pipe.expire(key("typing"), int(ttl) + 30)
        pipe.execute()
        return True
    return bool(call(op, False))


def typing_clear(user_id: int) -> None:
    def op(r):
        for member in r.zrange(key("typing"), 0, -1):
            if member.split(_TYPING_SEP, 1)[0] == str(user_id):
                r.zrem(key("typing"), member)
    call(op)


def typing_list(exclude_user_id: int) -> list[str] | None:
    """Names of everybody currently typing except me, or None when Redis is not available."""
    def op(r):
        r.zremrangebyscore(key("typing"), 0, time.time())
        out = []
        for member in sorted(r.zrange(key("typing"), 0, -1)):
            uid, _, name = member.partition(_TYPING_SEP)
            if uid != str(exclude_user_id):
                out.append(name)
        return out
    return call(op, None)


# ------------------------------------------------------------------------------------------------ deployment check

def deployment_warnings(env: dict[str, str] | None = None) -> list[str]:
    """Said once at startup: several workers without Redis work, but not as well."""
    env = os.environ if env is None else env
    try:
        workers = int(env.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        workers = 1
    out = []
    if workers > 1 and not enabled():
        out.append(f"{workers} web workers but REDIS_URL is empty: rate limits are counted per worker (about {workers}x "
                   "looser), chat updates can lag up to 20 s, and cache clears do not reach other workers. See docs/SCALING.md.")
    if workers > 1 and _cfg.DATABASE_URL.startswith("sqlite"):
        out.append("several workers on SQLite: use PostgreSQL.")
    pool = _cfg.DB_POOL_SIZE + _cfg.DB_MAX_OVERFLOW
    if workers > 1 and not _cfg.DB_PGBOUNCER and not _cfg.DATABASE_URL.startswith("sqlite") and pool * workers > 100:
        out.append(f"database connections can reach {pool} x {workers} = {pool * workers}, above PostgreSQL's default "
                   "max_connections of 100: raise max_connections, lower DB_POOL_SIZE / DB_MAX_OVERFLOW, or use PgBouncer.")
    return out
