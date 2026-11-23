"""Games Standard: the contract every game implements (see GAMES_STANDARD_PLAN.md).

Shared pages (hub, Records) talk to a ``GameAdapter`` and get back *normalized rows*, never ORM
objects. Adding a game means writing one adapter and registering it in ``app/games/__init__.py``.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.core.rbac import hides_totals

# The six rights every game defines (plan 4.7). The adapter maps each to its own permission.
RIGHTS = ("view", "place", "cancel", "report", "manage", "declare")
# Workspace tabs (plan 4.3) and Records tabs (plan 4.4): same names for every game.
WORKSPACE_TABS = ("live", "schedule", "tickets", "declare")
RECORD_TABS = ("tickets", "cancelled", "commissions", "history", "reports")
TAB_LABELS = {"tickets": "Tickets", "cancelled": "Cancelled", "commissions": "Commissions",
              "history": "History", "reports": "Reports"}


def can_see_money(ctx) -> bool:
    """One rule for every shared page: roles that hide totals never get stakes, payouts or KPIs."""
    return ctx.user is not None and not hides_totals(ctx.user)


@dataclass(frozen=True)
class Filters:
    start: dt.date | None = None
    end: dt.date | None = None
    status: str | None = None
    q: str | None = None
    session_id: int | None = None      # event (sabong) / draw (number game)
    round_id: int | None = None        # fight (sabong)
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class TicketRow:
    game: str
    code: str
    at: dt.datetime | None
    who: str
    round_label: str
    pick: str
    stake: int | None            # None when the viewer may not see money
    status: str
    payout: int | None
    url: str = ""
    cancelled_at: dt.datetime | None = None
    cancelled_by: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RoundRow:
    game: str
    session: str
    round_label: str
    result: str
    at: dt.datetime | None
    detail: str                  # pools (sabong) or balls (number game); "" when hidden
    url: str = ""
    location: str = ""
    role: str = ""               # Llamado / Dehado of the declared side (sabong)
    proof_url: str = ""          # result photo, when one was uploaded


@dataclass(frozen=True)
class ReportRow:
    label: str
    staked: int
    paid: int
    house: int
    commission: int
    expenses: int
    net: int
    url: str = ""
    tickets: int | None = None   # live ticket count, when the game can say
    result: str = ""             # declared result, for a single round


@dataclass(frozen=True)
class ScheduleRow:
    label: str
    when: str                     # already formatted, e.g. "2026-11-23" or "2026-11-23 10:48"
    status: str                   # chip text: draft/open/closed/canceled (sabong), scheduled/open/locked/settled/void (number game)
    location: str
    detail: str
    url: str


@dataclass(frozen=True)
class CreateSpec:
    """What the shared Schedule drawer renders. Field dicts: name, label, type, value, required, options, attrs."""
    title: str
    action: str
    fields: list
    submit: str = "Create"
    note: str = ""
    generate: "CreateSpec | None" = None


SCHEDULE_FILTERS = ("today", "upcoming", "past")


@dataclass
class GameAdapter:
    key: str = ""
    label: str = ""
    icon: str = "i-grid"
    vocabulary: dict[str, str] = field(default_factory=dict)      # {"session": "Event", "round": "Fight"}
    perms: dict[str, Any] = field(default_factory=dict)           # right -> Perm (or tuple of alternatives)
    nav_active: tuple[str, ...] = ()                              # sidebar 'active' keys that belong to this game

    # ---- rights --------------------------------------------------------------------------
    def can(self, ctx, right: str) -> bool:
        if right not in RIGHTS:
            raise ValueError(f"unknown right {right!r}")
        needed = self.perms.get(right)
        if needed is None:
            return False
        options = needed if isinstance(needed, tuple) else (needed,)
        return any(ctx.can(p) for p in options)

    def visible(self, ctx) -> bool:
        """Hidden entirely when the view right is missing (also covers the Bola dev-only gate)."""
        return self.can(ctx, "view")

    # ---- workspace -----------------------------------------------------------------------
    def workspace_urls(self, ctx) -> dict[str, str | None]:
        raise NotImplementedError

    def summary_today(self, ctx) -> dict | None:
        raise NotImplementedError

    # ---- records -------------------------------------------------------------------------
    def tickets(self, ctx, f: Filters) -> list[TicketRow]:
        raise NotImplementedError

    def cancelled(self, ctx, f: Filters) -> list[TicketRow]:
        raise NotImplementedError

    def history(self, ctx, f: Filters) -> list[RoundRow]:
        raise NotImplementedError

    def report_daily(self, ctx, day: dt.date) -> list[ReportRow]:
        raise NotImplementedError

    def report_monthly(self, ctx, year: int, month: int) -> list[ReportRow]:
        raise NotImplementedError

    def report_session(self, ctx, session_id: int) -> list[ReportRow]:
        raise NotImplementedError

    def schedule(self, ctx, when: str) -> list[ScheduleRow]:
        raise NotImplementedError

    def create_spec(self, ctx) -> CreateSpec | None:
        """None when the viewer may not create sessions or rounds."""
        raise NotImplementedError

    def report_archive(self, ctx, f: Filters) -> list[ReportRow]:
        """Finished sessions, newest first (the old Report archives page). Optional per game."""
        return []

    def commission_url(self, ctx) -> str:
        return f"/commissions?game={self.key}"
