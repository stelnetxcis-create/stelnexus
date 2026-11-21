"""LIMIT / OFFSET clamps shared by every list query.

Services used to clamp with ``min(limit, 1000)`` only. That has no lower bound, and the JSON API
hands ``limit`` straight from the query string, so ``?limit=-1`` reached the database as
``LIMIT -1``: SQLite treats a negative LIMIT as *no limit* (the whole table came back), PostgreSQL
rejects it (a 500). ``clamp_limit`` makes both impossible: the result is always 1..maximum.
"""
from __future__ import annotations

# A page deeper than this is refused by the web layer (see app.web.paging.MAX_PAGE); services apply
# the same ceiling so an API caller cannot ask the database to skip a billion rows either.
MAX_OFFSET = 1_000_000


def clamp_limit(limit: object, maximum: int, default: int | None = None) -> int:
    """``limit`` as an int in 1..maximum. Junk (None, text) falls back to ``default`` (or the max)."""
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = default if default is not None else maximum
    return max(1, min(value, maximum))


def clamp_offset(offset: object) -> int:
    """``offset`` as an int in 0..MAX_OFFSET."""
    try:
        value = int(offset)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, MAX_OFFSET))
