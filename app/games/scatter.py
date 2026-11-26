"""Scatter (the slot) adapter: wraps services/scatter for the shared Games hub and Records pages.

Scatter has no schedule, no declaring and no cancelling: a round is played and settled in one request, so those
rights are simply absent and the matching workspace tabs are None (the shared pages already hide a None tab)."""
from __future__ import annotations

import datetime as dt

from app.core.rbac import Perm
from app.db.models import ScatterRound
from app.games.base import (CreateSpec, Filters, GameAdapter, ReportRow, RoundRow, ScheduleRow, TicketRow,
                            can_see_money)
from app.games.sabong import day_bounds, range_bounds, usernames
from app.services import scatter


def _pick(r: ScatterRound) -> str:
    bits = [f"{r.win_x100 / 100:g}x" if r.win_x100 else "no win"]
    if r.free_spins:
        bits.append(f"{r.free_spins} free spins")
    if r.top_multiplier > 1:
        bits.append(f"x{r.top_multiplier} multiplier")
    if r.capped:
        bits.append("max win")
    return " · ".join(bits)


class ScatterAdapter(GameAdapter):
    def __init__(self) -> None:
        super().__init__(
            key="scatter", nav_active=("scatter",), label="Scatter", icon="i-flame",
            vocabulary={"game": "Scatter", "session": "Day", "round": "Spin", "ticket": "Round"},
            # a round is final when dealt and there is nothing to schedule or declare: those rights exist, held by nobody
            perms={"view": Perm.SCATTER_VIEW, "place": Perm.SCATTER_PLAY, "report": Perm.SCATTER_REPORT,
                   "tickets": Perm.SCATTER_VIEW,           # its rounds list in Records, scoped to the viewer by list_rounds
                   "cancel": None, "manage": None, "declare": None})

    def visible(self, ctx) -> bool:
        return self.shown(ctx) and scatter.enabled(ctx.store) and ctx.can(Perm.SCATTER_VIEW)

    def workspace_urls(self, ctx) -> dict[str, str | None]:
        return {"live": "/scatter", "schedule": None, "declare": None,
                "tickets": "/records?tab=tickets&game=scatter" if can_see_money(ctx) else None}

    def summary_today(self, ctx) -> dict:
        since, until = day_bounds(dt.datetime.now(scatter.MANILA).date())
        money = can_see_money(ctx) and ctx.user.is_top
        t = scatter.totals(ctx.db, ctx.user, since, until) if money else None
        return {"state": "live", "rounds": 1, "open": 1, "tickets": t["rounds"] if t else None,
                "staked": t["staked"] if t else None, "next": None}

    # ---- schedule: none ------------------------------------------------------------------
    def schedule(self, ctx, when: str) -> list[ScheduleRow]:
        return []

    def create_spec(self, ctx) -> CreateSpec | None:
        return None

    # ---- records -------------------------------------------------------------------------
    def _rows(self, ctx, rounds: list[ScatterRound]) -> list[TicketRow]:
        if not rounds:
            return []
        money = can_see_money(ctx)
        names = usernames(ctx, [r.user_id for r in rounds])
        return [TicketRow(game=self.key, code=r.code, at=r.placed_at, who=names.get(r.user_id, "—"),
                          round_label="Scatter spin", pick=_pick(r),
                          stake=r.stake_cents if money else None, status=r.status.value,
                          payout=r.win_cents if money else None, url="") for r in rounds]

    def tickets(self, ctx, f: Filters) -> list[TicketRow]:
        since, until = range_bounds(f)
        return self._rows(ctx, scatter.list_rounds(
            ctx.db, ctx.user, search=f.q or None,
            since=since, until=until, limit=f.limit, offset=f.offset))

    def cancelled(self, ctx, f: Filters) -> list[TicketRow]:
        return []                                   # a round cannot be cancelled: it is over when it is dealt

    def history(self, ctx, f: Filters) -> list[RoundRow]:
        since, until = range_bounds(f)
        rounds = scatter.list_rounds(ctx.db, ctx.user, only_wins=True, since=since, until=until,
                                     limit=f.limit, offset=f.offset)
        # the best rounds first is what a "history" of a slot means; the shared page keeps the order we give
        rounds.sort(key=lambda r: (r.win_x100, r.id), reverse=True)
        return [RoundRow(game=self.key, session=f"{r.placed_at:%Y-%m-%d}", round_label=f"Round {r.code}",
                         result=_pick(r), at=r.placed_at, detail=f"{r.spins} spin{'' if r.spins == 1 else 's'}")
                for r in rounds]

    # ---- reports -------------------------------------------------------------------------
    @staticmethod
    def _row(label: str, rounds: int, staked: int, paid: int, url: str = "") -> ReportRow:
        house = staked - paid
        return ReportRow(label=label, staked=staked, paid=paid, house=house, commission=0, expenses=0,
                         net=house, url=url, tickets=rounds)

    def report_daily(self, ctx, day: dt.date) -> list[ReportRow]:
        since, until = day_bounds(day)
        return [self._row(p["who"], p["rounds"], p["staked"], p["paid"])
                for p in scatter.by_player(ctx.db, ctx.user, since, until)]

    def report_monthly(self, ctx, year: int, month: int) -> list[ReportRow]:
        first = dt.date(year, month, 1)
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        since, _ = day_bounds(first)
        until, _ = day_bounds(nxt)
        days = scatter.by_day(ctx.db, ctx.user, since, until)
        return [self._row(f"{d:%Y-%m-%d}", v["rounds"], v["staked"], v["paid"],
                          f"/records?tab=reports&game=scatter&day={d:%Y-%m-%d}") for d, v in sorted(days.items())]

    def report_session(self, ctx, session_id: int) -> list[ReportRow]:
        return []                                   # Scatter has no sessions; the daily report is the unit

    def commission_url(self, ctx) -> str:
        return "/records?tab=reports&game=scatter"
