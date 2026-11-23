"""Sabong (Meron / Wala) adapter: wraps services/events, betting and reports."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.core.rbac import Perm, is_top, scope_ids
from app.db.models import Event, EventStatus, Fight, FightStatus, Location, Ticket, TicketStatus, User
from app.games.base import Filters, GameAdapter, ReportRow, RoundRow, TicketRow, can_see_money
from app.services import betting, bola, events, reports


def day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Start (inclusive) and end (exclusive) of a Manila calendar day, as aware datetimes."""
    start = bola.local_instant(day, dt.time.min)
    return start, start + dt.timedelta(days=1)


def range_bounds(f: Filters) -> tuple[dt.datetime | None, dt.datetime | None]:
    since = day_bounds(f.start)[0] if f.start else None
    until = day_bounds(f.end)[1] if f.end else None
    return since, until


def usernames(ctx, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: ctx.who(u) for u in ctx.db.scalars(select(User).where(User.id.in_(ids))).all()}


def canceller_names(ctx, ids) -> dict[int, str]:
    """The hidden Admin account always shows as 'System' in cancel logs, to everyone."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: ("System" if u.is_hidden else u.username)
            for u in ctx.db.scalars(select(User).where(User.id.in_(ids))).all()}


class SabongAdapter(GameAdapter):
    def __init__(self) -> None:
        super().__init__(
            key="sabong", nav_active=("events", "change_name"), label="Sabong", icon="i-target",
            vocabulary={"game": "Sabong", "session": "Event", "round": "Fight", "ticket": "Ticket"},
            perms={"view": Perm.FIGHTS_VIEW, "place": Perm.BETS_PLACE, "cancel": Perm.CANCEL_LOG,
                   "tickets": Perm.BETS_VIEW, "report": Perm.REPORTS_VIEW,
                   "manage": (Perm.EVENTS_MANAGE, Perm.EVENTS_OPEN), "declare": Perm.FIGHTS_DECLARE})

    # extra right names used by Records (tickets list, cancelled list) map to the same table
    def can(self, ctx, right: str) -> bool:
        if right == "tickets":
            return ctx.can(self.perms["tickets"])
        return super().can(ctx, right)

    def visible(self, ctx) -> bool:
        return ctx.can(Perm.FIGHTS_VIEW) or ctx.can(Perm.EVENTS_VIEW)

    # ---- workspace -------------------------------------------------------------------
    def workspace_urls(self, ctx) -> dict[str, str | None]:
        return {
            "live": "/change_name" if ctx.can(Perm.FIGHTS_VIEW) else None,
            "schedule": "/events" if ctx.can(Perm.EVENTS_VIEW) else None,
            "tickets": "/records?tab=tickets&game=sabong" if ctx.can(Perm.BETS_VIEW) and can_see_money(ctx) else None,
            "declare": "/staff/declarator" if ctx.can(Perm.STAFF_DECLARATOR) else None,
        }

    def summary_today(self, ctx) -> dict:
        db = ctx.db
        open_rounds = int(db.scalar(select(func.count(Fight.id)).where(
            Fight.status.in_([FightStatus.OPEN, FightStatus.LOCKED]))) or 0)
        live_event = db.scalar(select(Event).where(Event.status == EventStatus.OPEN)
                               .order_by(Event.event_date.desc(), Event.id.desc()))
        since, until = day_bounds(bola.utcnow().astimezone(bola.MANILA).date())
        stmt = select(func.count(Ticket.id), func.coalesce(func.sum(Ticket.stake_cents), 0)).where(
            Ticket.placed_at >= since, Ticket.placed_at < until, Ticket.status != TicketStatus.CANCELED)
        if not is_top(ctx.user):
            stmt = stmt.where(Ticket.agent_id.in_(scope_ids(db, ctx.user)))
        tickets, staked = db.execute(stmt).one()
        return {"state": "live" if live_event else ("open" if open_rounds else "closed"),
                "rounds": open_rounds, "tickets": int(tickets),
                "staked": int(staked) if can_see_money(ctx) else None,
                "next": live_event.title if live_event else None}

    # ---- records ---------------------------------------------------------------------
    def _rows(self, ctx, tickets: list[Ticket]) -> list[TicketRow]:
        if not tickets:
            return []
        db, money = ctx.db, can_see_money(ctx)
        fights = {f.id: f for f in db.scalars(select(Fight).where(Fight.id.in_({t.fight_id for t in tickets}))).all()}
        evs = {e.id: e for e in db.scalars(select(Event).where(Event.id.in_({f.event_id for f in fights.values()}))).all()}
        names = usernames(ctx, [t.user_id or t.agent_id for t in tickets])
        cancellers = canceller_names(ctx, [t.canceled_by_id for t in tickets])
        out = []
        for t in tickets:
            fight = fights.get(t.fight_id)
            title = evs[fight.event_id].title if fight and fight.event_id in evs else ""
            out.append(TicketRow(
                game=self.key, code=t.code, at=t.placed_at, who=names.get(t.user_id or t.agent_id, "—"),
                round_label=f"{title} · Fight #{fight.number}" if fight else "—", pick=t.side.value.title(),
                stake=t.stake_cents if money else None, status=t.status.value,
                payout=(t.payout_cents if t.payout_cents else 0) if money else None,
                url=f"/tickets/{t.id}", cancelled_at=t.canceled_at,
                cancelled_by=cancellers.get(t.canceled_by_id, "—") if t.canceled_by_id else "",
                reason=t.cancel_reason or ""))
        return out

    def tickets(self, ctx, f: Filters) -> list[TicketRow]:
        since, until = range_bounds(f)
        status = TicketStatus(f.status) if f.status in ("active", "canceled", "settled", "refunded") else None
        return self._rows(ctx, betting.list_tickets(
            ctx.db, ctx.user, status=status, search=f.q or None, event_id=f.session_id, fight_id=f.round_id,
            since=since, until=until, limit=f.limit, offset=f.offset))

    def cancelled(self, ctx, f: Filters) -> list[TicketRow]:
        since, until = range_bounds(f)
        return self._rows(ctx, betting.list_tickets(
            ctx.db, ctx.user, canceled_only=True, search=f.q or None, event_id=f.session_id, fight_id=f.round_id,
            since=since, until=until, limit=f.limit, offset=f.offset))

    def history(self, ctx, f: Filters) -> list[RoundRow]:
        fights = reports.history(ctx.db, limit=f.limit, offset=f.offset)
        evs = {e.id: e for e in ctx.db.scalars(select(Event).where(Event.id.in_({x.event_id for x in fights}))).all()} if fights else {}
        pools = betting.pool_snapshots(ctx.db, [x.id for x in fights]) if can_see_money(ctx) else {}
        loc_ids = {e.location_id for e in evs.values() if e.location_id}
        locs = {l.id: l for l in ctx.db.scalars(select(Location).where(Location.id.in_(loc_ids))).all()} if loc_ids else {}
        out = []
        for x in fights:
            snap = pools.get(x.id)
            out.append(RoundRow(
                game=self.key, session=evs[x.event_id].title if x.event_id in evs else "—",
                round_label=f"Fight #{x.number}", result=x.result.value if x.result else x.status.value,
                at=x.declared_at, detail=(f"Meron {snap['meron']/100:,.2f} · Wala {snap['wala']/100:,.2f} · "
                                          f"Draw {snap['draw']/100:,.2f}") if snap else "",
                url=f"/events/{x.event_id}",
                location=" · ".join(p for p in (locs[evs[x.event_id].location_id].name,
                                               locs[evs[x.event_id].location_id].city) if p)
                if x.event_id in evs and evs[x.event_id].location_id in locs else ""))
        return out

    # ---- reports (built from the same figures as the archive page) --------------------
    @staticmethod
    def _row(label: str, s: dict, url: str = "") -> ReportRow:
        return ReportRow(label=label, staked=s["staked"], paid=s["payouts"], house=s["house"],
                         commission=s["commission"], expenses=s["expenses"], net=s["net"], url=url)

    def _events_on(self, ctx, start: dt.date, end: dt.date) -> list[Event]:
        return list(ctx.db.scalars(select(Event).where(
            Event.event_date >= start, Event.event_date < end, Event.status != EventStatus.DRAFT)
            .order_by(Event.event_date, Event.id)).all())

    def report_daily(self, ctx, day: dt.date) -> list[ReportRow]:
        evs = self._events_on(ctx, day, day + dt.timedelta(days=1))
        summ = reports.event_summaries(ctx.db, [e.id for e in evs])
        return [self._row(e.title, summ[e.id], f"/records?tab=reports&game=sabong&session={e.id}") for e in evs]

    def report_monthly(self, ctx, year: int, month: int) -> list[ReportRow]:
        start = dt.date(year, month, 1)
        end = dt.date(year + (month == 12), month % 12 + 1, 1)
        evs = self._events_on(ctx, start, end)
        summ = reports.event_summaries(ctx.db, [e.id for e in evs])
        by_day: dict[dt.date, dict] = {}
        for e in evs:
            agg = by_day.setdefault(e.event_date, {k: 0 for k in summ[e.id]})
            for k, v in summ[e.id].items():
                agg[k] += v
        return [self._row(d.isoformat(), agg, f"/records?tab=reports&game=sabong&day={d.isoformat()}")
                for d, agg in sorted(by_day.items())]

    def report_archive(self, ctx, f: Filters) -> list[ReportRow]:
        evs = reports.archived_events(ctx.db, limit=f.limit, offset=f.offset)
        summ = reports.event_summaries(ctx.db, [e.id for e in evs])
        return [self._row(f"{e.event_date:%Y-%m-%d} · {e.title}", summ[e.id],
                          f"/records?tab=reports&game=sabong&view=session&session={e.id}") for e in evs]

    def report_session(self, ctx, session_id: int) -> list[ReportRow]:
        """One row per fight of the event; the figures that exist per fight (stake, payout, house)."""
        event = events.get_event(ctx.db, session_id)
        out = []
        live = [TicketStatus.ACTIVE, TicketStatus.SETTLED, TicketStatus.REFUNDED]
        for fight in events.fights_of(ctx.db, event.id):
            staked, paid = ctx.db.execute(select(
                func.coalesce(func.sum(Ticket.stake_cents), 0), func.coalesce(func.sum(Ticket.payout_cents), 0)
            ).where(Ticket.fight_id == fight.id, Ticket.status.in_(live))).one()
            fig = reports.fight_figures(ctx.db, fight)
            out.append(ReportRow(label=f"Fight #{fight.number}", staked=int(staked), paid=int(paid),
                                 house=fig["system_over"], commission=0, expenses=0, net=fig["system_over"]))
        return out
