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
# A seventh right used only by Records: may this account list the game's tickets? Each adapter maps it in ``perms``;
# a game that does not map it simply has no tickets tab (``can`` answers False). Not part of the six every game must define.
RECORD_RIGHTS = ("tickets",)
# Workspace tabs (plan 4.3) and Records tabs (plan 4.4): same names for every game.
WORKSPACE_TABS = ("live", "schedule", "tickets", "declare")
RECORD_TABS = ("tickets", "cancelled", "commissions", "history", "insights", "reports")
TAB_LABELS = {"tickets": "Tickets", "cancelled": "Cancelled", "commissions": "Commissions",
              "history": "History", "insights": "Insights", "reports": "Reports"}


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
    location_id: int | None = None     # number game: one area


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
    modal_id: str = "create-modal"
    modals: list = field(default_factory=list)     # extra CreateSpec drawers (e.g. quick-register a location)


SCHEDULE_FILTERS = ("all", "today", "upcoming", "past")


@dataclass
class GameAdapter:
    key: str = ""
    label: str = ""
    icon: str = "i-grid"
    vocabulary: dict[str, str] = field(default_factory=dict)      # {"session": "Event", "round": "Fight"}
    perms: dict[str, Any] = field(default_factory=dict)           # right -> Perm (or tuple of alternatives)
    schedule_view: str = "table"                                  # "table" or "cards" (live event cards)
    schedule_default: str = "today"
    nav_active: tuple[str, ...] = ()                              # sidebar 'active' keys that belong to this game

    # ---- rights --------------------------------------------------------------------------
    def can(self, ctx, right: str) -> bool:
        if right not in RIGHTS and right not in RECORD_RIGHTS:
            raise ValueError(f"unknown right {right!r}")
        needed = self.perms.get(right)
        if needed is None:
            return False
        options = needed if isinstance(needed, tuple) else (needed,)
        return any(ctx.can(p) for p in options)

    def shown(self, ctx) -> bool:
        """The Admin's visibility switch (Variables > Games). Off = the game does not exist for anyone."""
        return bool(ctx.store.get(f"games.show_{self.key}"))

    def visible(self, ctx) -> bool:
        """Hidden entirely when the view right is missing (also covers the Bola dev-only gate)."""
        return self.shown(ctx) and self.can(ctx, "view")

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

    async def live(self, request, ctx):
        """Render the Live tab in place. Only games whose workspace_urls['live'] is /games/<key>/live implement it."""
        raise NotImplementedError

    def schedule_context(self, ctx, when: str) -> dict:
        """Extra template context for games whose schedule_view is not the plain table."""
        return {}

    def report_archive(self, ctx, f: Filters) -> list[ReportRow]:
        """Finished sessions, newest first (the old Report archives page). Optional per game."""
        return []

    def insights(self, ctx, f: Filters, sort: str = "stake") -> dict | None:
        """Records > Insights: KPIs and the breakdowns that suit this game. None = nothing to show."""
        return None

    def insight_choices(self, ctx) -> dict:
        """Dropdown choices for the Insights filters: {"areas": [(id, name)], "sessions": [(id, label)], ...}."""
        return {}

    def commission_url(self, ctx) -> str:
        return f"/commissions?game={self.key}"
