"""Number game (Bola, Tumbok-Sahod) adapter: wraps services/bola and reports."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.core.rbac import Perm, bola_visible_to
from app.db.models import BolaDraw, BolaTicket, DrawStatus, TicketStatus
from app.games.base import Filters, GameAdapter, ReportRow, RoundRow, TicketRow, can_see_money
from app.games.sabong import canceller_names, day_bounds, range_bounds, usernames
from app.services import bola, reports


def _draw_label(d: BolaDraw) -> str:
    area = d.location.name if d.location else "—"
    return f"{area} · Draw #{d.slot} {bola.local(d.scheduled_at):%H:%M}"


class BolaAdapter(GameAdapter):
    def __init__(self) -> None:
        super().__init__(
            key="bola", nav_active=("bola",), label="Number game", icon="i-hash",
            vocabulary={"game": "Number game", "session": "Draw day", "round": "Draw", "ticket": "Ticket"},
            perms={"view": Perm.BOLA_VIEW, "place": Perm.BOLA_PLACE, "cancel": Perm.CANCEL_LOG,
                   "tickets": Perm.BOLA_VIEW, "report": Perm.BOLA_EXPOSURE,
                   "manage": (Perm.BOLA_MANAGE, Perm.BOLA_OPEN), "declare": Perm.BOLA_DECLARE})

    def can(self, ctx, right: str) -> bool:
        if right == "tickets":
            return ctx.can(self.perms["tickets"])
        return super().can(ctx, right)

    def visible(self, ctx) -> bool:
        # switched off, or dev-gated away from this account -> the game does not exist for them
        return (bool(ctx.store.get("bola.enabled")) and bola_visible_to(ctx.user)
                and ctx.can(Perm.BOLA_VIEW))

    def workspace_urls(self, ctx) -> dict[str, str | None]:
        return {
            "live": "/bola",
            "schedule": "/bola" if ctx.can(Perm.BOLA_MANAGE) or ctx.can(Perm.BOLA_VIEW) else None,
            "tickets": "/records?tab=tickets&game=bola" if can_see_money(ctx) else None,
            "declare": "/staff/declarator" if ctx.can(Perm.STAFF_DECLARATOR) else None,
        }

    def summary_today(self, ctx) -> dict:
        board = bola.lobby(ctx.db)
        nxt = board["next"]
        since, until = day_bounds(board["day"])
        tickets, staked = ctx.db.execute(
            select(func.count(BolaTicket.id), func.coalesce(func.sum(BolaTicket.stake_cents), 0)).where(
                BolaTicket.placed_at >= since, BolaTicket.placed_at < until,
                BolaTicket.status != TicketStatus.CANCELED)).one() if can_see_money(ctx) and ctx.user.is_top else (None, None)
        return {"state": "live" if board["open"] else ("open" if board["total"] else "closed"),
                "rounds": board["total"], "open": board["open"], "tickets": tickets,
                "staked": int(staked) if staked is not None else None,
                "next": f"{bola.local(nxt.scheduled_at):%H:%M}" if nxt else None}

    # ---- records ---------------------------------------------------------------------
    def _rows(self, ctx, tickets: list[BolaTicket]) -> list[TicketRow]:
        if not tickets:
            return []
        money = can_see_money(ctx)
        draws = {d.id: d for d in ctx.db.scalars(select(BolaDraw).options(joinedload(BolaDraw.location)).where(
            BolaDraw.id.in_({t.draw_id for t in tickets}))).unique().all()}
        names = usernames(ctx, [t.user_id or t.agent_id for t in tickets])
        cancellers = canceller_names(ctx, [t.canceled_by_id for t in tickets])
        out = []
        for t in tickets:
            d = draws.get(t.draw_id)
            out.append(TicketRow(
                game=self.key, code=t.code, at=t.placed_at, who=names.get(t.user_id or t.agent_id, "—"),
                round_label=_draw_label(d) if d else "—",
                pick=f"{t.kind.value} {' '.join(str(n) for n in t.numbers)}",
                stake=t.stake_cents if money else None, status=t.status.value,
                payout=t.payout_cents if money else None, url=f"/bola/tickets/{t.id}",
                cancelled_at=t.canceled_at,
                cancelled_by=cancellers.get(t.canceled_by_id, "—") if t.canceled_by_id else "",
                reason=t.cancel_reason or ""))
        return out

    def tickets(self, ctx, f: Filters) -> list[TicketRow]:
        since, until = range_bounds(f)
        status = TicketStatus(f.status) if f.status in ("active", "canceled", "settled", "refunded") else None
        return self._rows(ctx, bola.list_tickets(
            ctx.db, ctx.user, status=status, search=f.q or None, draw_id=f.round_id or f.session_id,
            since=since, until=until, limit=f.limit, offset=f.offset))

    def cancelled(self, ctx, f: Filters) -> list[TicketRow]:
        since, until = range_bounds(f)
        return self._rows(ctx, bola.list_tickets(
            ctx.db, ctx.user, canceled_only=True, search=f.q or None, draw_id=f.round_id or f.session_id,
            since=since, until=until, limit=f.limit, offset=f.offset))

    def history(self, ctx, f: Filters) -> list[RoundRow]:
        draws = list(ctx.db.scalars(select(BolaDraw).options(joinedload(BolaDraw.location)).where(
            BolaDraw.status == DrawStatus.SETTLED)
            .order_by(BolaDraw.declared_at.desc().nulls_last(), BolaDraw.id.desc())
            .limit(min(max(f.limit, 1), 500)).offset(max(f.offset, 0))).unique().all())
        return [RoundRow(game=self.key, session=f"{d.draw_date:%Y-%m-%d}", round_label=_draw_label(d),
                         result=f"{d.ball_first}-{d.ball_second}" if d.ball_first is not None else "—",
                         at=d.declared_at,
                         detail=f"{d.ball_first}, {d.ball_second}" if d.ball_first is not None else "",
                         url=f"/bola/draws/{d.id}", location=d.location.name if d.location else "")
                for d in draws]

    # ---- reports ---------------------------------------------------------------------
    @staticmethod
    def _row(label: str, r: dict, url: str = "") -> ReportRow:
        return ReportRow(label=label, staked=r["staked"], paid=r["paid"], house=r["house"],
                         commission=r["commission"], expenses=r["expenses"], net=r["net"], url=url)

    def report_daily(self, ctx, day: dt.date) -> list[ReportRow]:
        rep = reports.bola_daily(ctx.db, day)
        return [self._row(_draw_label(r["draw"]), r, f"/bola/draws/{r['draw'].id}") for r in rep["rows"]]

    def report_monthly(self, ctx, year: int, month: int) -> list[ReportRow]:
        rep = reports.bola_monthly(ctx.db, year, month)
        return [self._row(r["day"], r, f"/records?tab=reports&game=bola&day={r['day']}") for r in rep["rows"]]

    def report_session(self, ctx, session_id: int) -> list[ReportRow]:
        draw = bola.get_draw(ctx.db, session_id)
        rep = reports.bola_draw_report(ctx.db, draw, ctx.store)
        return [self._row(_draw_label(draw), rep, f"/bola/draws/{draw.id}/report.csv")]
