"""Serialise one-time startup work across several web workers.

``uvicorn --workers 4`` imports the app four times at once; each would run the schema upgrade and seed the team
catalog. Without a lock they race (two ``ALTER TABLE`` / duplicate inserts). On PostgreSQL the first worker takes a
*transaction-level* advisory lock and the others wait for it, then find the work already done and finish in
milliseconds. A transaction-level lock (not a session-level one) on purpose: it also works through PgBouncer in
transaction mode, and it is released by the database itself if the worker dies. SQLite (dev / tests) needs nothing.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

log = logging.getLogger("stelnexus.startup")


def lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha1(name.encode()).digest()[:8], "big") >> 1     # a positive 63-bit integer


@contextmanager
def startup_lock(engine, name: str = "stelnexus-startup", wait_seconds: float = 180.0, poll: float = 0.5) -> Iterator[bool]:
    """Yields True when this worker holds the lock. After ``wait_seconds`` it carries on anyway (and says so): a stuck
    sibling must not keep the site down."""
    if engine.dialect.name != "postgresql":
        yield True
        return
    key = lock_key(name)
    with engine.connect() as conn, conn.begin():
        deadline = time.monotonic() + wait_seconds
        got = False
        while True:
            got = bool(conn.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar())
            if got or time.monotonic() >= deadline:
                break
            time.sleep(poll)
        if not got:
            log.error("startup lock not acquired within %.0fs: continuing without it", wait_seconds)
        yield got
