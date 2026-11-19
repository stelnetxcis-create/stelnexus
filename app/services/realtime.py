"""Server-sent events for the chats, without any new infrastructure.

How it fits together
--------------------
* **Writers never talk to the hub directly.** A SQLAlchemy ``after_flush`` listener notices a new
  ``ChatMessage`` (or a changed ``Fight``) and remembers the topic on the session; ``after_commit``
  then wakes the subscribers. Waking on *commit* (not on flush) matters: a stream that woke early would
  read the table before the row is visible and miss the message until the next heartbeat.
  Every code path that saves a chat message is covered, including ones added later.
* **A stream is a wait loop, not a polling loop.** It sleeps on an ``asyncio.Event`` and only touches the
  database when a topic fires or the heartbeat elapses. Each pass opens its own short-lived session
  (``session_scope``), so an open stream never pins a pooled DB connection.
* **The heartbeat is the safety net.** The app runs one process, so the hub sees every write. Anything
  that writes from *another* process (the CLI, the systemd timers) cannot wake the hub, and shows up at
  the next heartbeat instead (``HEARTBEAT`` seconds). If you ever run several workers, that same
  heartbeat keeps chat working, just slower than instant.
* **Auth is re-checked every pass.** The stream builds its context the same way a request does, so a
  signed-out, expired or demoted account is told ``auth`` and the stream ends.

The wire format is the same JSON the poll endpoints return, so the browser reuses one handler for both.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.core.errors import RateLimited, StelError

log = logging.getLogger("stelnexus.realtime")

HEARTBEAT = 20.0          # seconds between "anything changed?" passes when nobody wrote anything
FAST_HEARTBEAT = 2.0      # used while something expires by itself (typing dots)
MAX_AGE = 600.0           # a stream ends politely after this long; the browser reconnects (re-auth, fresh state)
MAX_PER_USER = 4          # open streams per account (a few tabs)
MAX_TOTAL = 800           # open streams per process
RETRY_MS = 3000           # EventSource reconnect delay we ask the browser to use


# ------------------------------------------------------------------------------------------------ hub

@dataclass(eq=False)
class Subscription:
    user_id: int
    topics: frozenset[str]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    created: float = field(default_factory=time.monotonic)
    started: bool = False

    def clear(self) -> None:
        self.event.clear()

    async def wait(self, timeout: float) -> bool:
        """True when woken by a publish, False when ``timeout`` elapsed."""
        try:
            await asyncio.wait_for(self.event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[Subscription]] = {}
        self._per_user: Counter[int] = Counter()
        self._total = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- subscribers (called on the event loop)
    def subscribe(self, user_id: int, topics: set[str] | frozenset[str]) -> Subscription:
        """Register a stream. Raises ``RateLimited`` (HTTP 429) when a limit is hit, so the browser falls
        back to polling instead of the server running out of streams."""
        with self._lock:
            self._sweep_locked()
            if self._per_user[user_id] >= MAX_PER_USER or self._total >= MAX_TOTAL:
                raise RateLimited("Too many live connections. Falling back to periodic updates.")
            sub = Subscription(user_id=user_id, topics=frozenset(topics))
            self._loop = asyncio.get_running_loop()
            for topic in sub.topics:
                self._subs.setdefault(topic, set()).add(sub)
            self._per_user[user_id] += 1
            self._total += 1
            return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._drop_locked(sub)

    def _drop_locked(self, sub: Subscription) -> None:
        known = False
        for topic in sub.topics:
            group = self._subs.get(topic)
            if group and sub in group:
                group.discard(sub)
                known = True
                if not group:
                    self._subs.pop(topic, None)
        if known:
            self._per_user[sub.user_id] -= 1
            if self._per_user[sub.user_id] <= 0:
                del self._per_user[sub.user_id]
            self._total -= 1

    def _sweep_locked(self) -> None:
        """Forget streams that were registered but never started (client vanished before the first byte)."""
        now = time.monotonic()
        stale = [s for group in self._subs.values() for s in group if not s.started and now - s.created > 30]
        for sub in set(stale):
            self._drop_locked(sub)

    # -- publishing (any thread)
    def publish(self, *topics: str) -> None:
        loop = self._loop
        if loop is None or not topics:
            return                                   # nobody has ever subscribed in this process
        try:
            loop.call_soon_threadsafe(self._wake, topics)
        except RuntimeError:                         # loop closed (shutdown / test teardown)
            pass

    def _wake(self, topics: tuple[str, ...]) -> None:
        with self._lock:
            targets = {s for t in topics for s in self._subs.get(t, ())}
        for sub in targets:
            sub.event.set()

    # -- introspection (tests, health)
    @property
    def total(self) -> int:
        return self._total

    def count_for(self, user_id: int) -> int:
        return self._per_user.get(user_id, 0)


hub = Hub()


# ------------------------------------------------------------------------------- commit-time publishing

def chat_topic() -> str:
    return "chat"


def fight_topic(fight_id: int) -> str:
    return f"fight:{fight_id}"


_INFO_KEY = "rt_topics"


def _topics_of(session: Session) -> set[str]:
    return session.info.setdefault(_INFO_KEY, set())


def _install_listeners() -> None:
    from app.db.models import ChatMessage, Fight            # local: models import the session module

    @event.listens_for(Session, "after_flush")
    def _collect(session: Session, _ctx: Any) -> None:
        for obj in session.new:
            if isinstance(obj, ChatMessage):
                _topics_of(session).add(chat_topic() if obj.fight_id is None else fight_topic(obj.fight_id))
        for obj in session.dirty:
            if isinstance(obj, Fight) and obj.id is not None:
                _topics_of(session).add(fight_topic(obj.id))   # lock / declare / cancel: the chat view follows

    @event.listens_for(Session, "after_commit")
    def _publish(session: Session) -> None:
        topics = session.info.pop(_INFO_KEY, None)
        if topics:
            hub.publish(*topics)

    @event.listens_for(Session, "after_rollback")
    def _forget(session: Session) -> None:
        session.info.pop(_INFO_KEY, None)


_install_listeners()


# ------------------------------------------------------------------------------------------------ SSE

@dataclass
class Snapshot:
    """What one pass of a stream learned. ``payload`` is the same dict the poll endpoint returns."""
    payload: dict
    last_id: int = 0          # highest message id in ``payload["messages"]`` (0 = none), used as the SSE event id
    fast: bool = False        # something is about to expire by itself: look again soon
    more: bool = False        # the page was full (a client far behind): read the next one straight away, do not sleep


def frame(event_name: str, data: dict | None = None, event_id: int | None = None) -> str:
    out = []
    if event_id:
        out.append(f"id: {event_id}")
    out.append(f"event: {event_name}")
    out.append("data: " + json.dumps(data if data is not None else {}, separators=(",", ":"), default=str))
    return "\n".join(out) + "\n\n"


def _digest(payload: dict) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def stream(request: Any, sub: Subscription, build: Callable[[int], Snapshot], *, after: int = 0,
                 heartbeat: float | None = None, max_age: float | None = None) -> AsyncIterator[str]:
    """The SSE body. ``build(after)`` runs in a worker thread with its own DB session each pass."""
    heartbeat = HEARTBEAT if heartbeat is None else heartbeat
    max_age = MAX_AGE if max_age is None else max_age
    began = time.monotonic()
    last_digest = ""
    sub.started = True
    try:
        yield f"retry: {RETRY_MS}\n\n"
        while True:
            if await request.is_disconnected():
                return
            sub.clear()                                    # clear BEFORE reading: a write during the read re-arms it
            try:
                snap = await run_in_threadpool(build, after)
            except StelError as exc:                       # signed out, role changed, game gone...
                yield frame("auth", {"status": exc.status_code, "detail": str(exc)})
                return
            except Exception:                              # noqa: BLE001 - never leak a traceback into a stream
                log.exception("stream pass failed")
                yield frame("retry", {})
                return
            if snap.last_id > after:
                after = snap.last_id
            digest = _digest(snap.payload)
            if digest != last_digest:
                last_digest = digest
                yield frame("state", snap.payload, snap.last_id or None)
            else:
                yield ": ping\n\n"                         # keeps idle-timeouts in proxies and phones at bay
            if time.monotonic() - began > max_age:
                yield frame("bye", {})
                return
            if snap.more and snap.last_id:
                continue                                   # backlog: drain it without waiting for a wake-up
            await sub.wait(FAST_HEARTBEAT if snap.fast else heartbeat)
    finally:
        hub.unsubscribe(sub)


SSE_HEADERS = {
    "Cache-Control": "no-store, no-transform",
    "X-Accel-Buffering": "no",        # nginx: do not buffer the stream
}


def resume_id(request: Any, after: int) -> int:
    """Where to resume: the browser's ``Last-Event-ID`` (sent on every automatic reconnect, and newer than the
    ``?after=`` baked into the original URL) wins over the query string."""
    raw = (request.headers.get("last-event-id") or "").strip()
    return max(after, int(raw)) if raw.isdigit() else max(after, 0)


def open_stream(request: Any, user_id: int, topics: set[str], build: Callable[[int], Snapshot], *,
                after: int = 0) -> StreamingResponse:
    """Register the stream and return the ``text/event-stream`` response (HTTP 429 if over a limit)."""
    from app.core import ratelimit
    ratelimit.check(f"sse:{user_id}", 40, 60)            # a reconnect storm costs the same as a poll storm
    sub = hub.subscribe(user_id, topics)
    return StreamingResponse(stream(request, sub, build, after=resume_id(request, after)),
                             media_type="text/event-stream", headers=SSE_HEADERS)
