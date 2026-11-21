"""Server-side pagination for the list pages.

Each list used to be cut off at a fixed number of rows (300, 150, 50...) with no way to reach older
ones. ``paginate`` asks the service for one extra row to learn whether another page exists (no COUNT
query), and returns a ``Pager`` the templates render with ``ui.pager(pager)``.

    pager, rows = paginate(request, 100, lambda limit, offset: svc.list_things(db, limit=limit, offset=offset))

``?page=N`` is zero-based, every other query parameter (filters, search) is kept in the links.
A page that shows two lists gives each its own parameter (``param="tpage"`` / ``"lpage"``) so
turning one list's page does not reset the other's.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Request

MAX_PAGE = 5000          # a hard ceiling so ?page=99999999 can't ask the database for a huge OFFSET


@dataclass(frozen=True)
class Pager:
    page: int
    per_page: int
    count: int            # rows on this page
    has_next: bool
    prev_url: str | None
    next_url: str | None

    @property
    def first(self) -> int:       # 1-based number of the first row shown
        return self.page * self.per_page + 1 if self.count else 0

    @property
    def last(self) -> int:
        return self.page * self.per_page + self.count

    @property
    def visible(self) -> bool:
        return self.page > 0 or self.has_next


def page_number(request: Request, param: str = "page") -> int:
    try:
        return min(max(int(request.query_params.get(param, "0")), 0), MAX_PAGE)
    except (TypeError, ValueError):
        return 0


def _url(request: Request, page: int, param: str = "page") -> str:
    url = request.url.remove_query_params(param)
    if page > 0:
        url = url.include_query_params(**{param: page})
    return url.path + (f"?{url.query}" if url.query else "")


def make_pager(request: Request, per_page: int, page: int, count: int, has_next: bool,
               param: str = "page") -> Pager:
    return Pager(page=page, per_page=per_page, count=count, has_next=has_next,
                 prev_url=_url(request, page - 1, param) if page > 0 else None,
                 next_url=_url(request, page + 1, param) if has_next else None)


def paginate(request: Request, per_page: int, fetch: Callable[..., Sequence[Any]],
             *, param: str = "page") -> tuple[Pager, list[Any]]:
    """Call ``fetch(limit=per_page + 1, offset=page * per_page)`` and split off the look-ahead row."""
    page = page_number(request, param)
    rows = list(fetch(limit=per_page + 1, offset=page * per_page))
    has_next = len(rows) > per_page
    rows = rows[:per_page]
    return make_pager(request, per_page, page, len(rows), has_next, param), rows
