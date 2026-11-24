"""Sabong (Meron / Wala) adapter: wraps services/events, betting and reports."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.core.rbac import Perm, is_top, scope_ids
from app.db.models import Event, EventStatus, Fight, FightStatus, Location, Ticket, TicketStatus, User
from app.games.base import (CreateSpec, Filters, GameAdapter, ReportRow, RoundRow, ScheduleRow, TicketRow,
                            can_see_money)
from app.services import betting, bola, events, locations, reports
from app.services import insights as insights_svc


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
            key="sabong", nav_active=("events", "change_name"), schedule_view="cards", schedule_default="all", label="Sabong", icon="i-target",
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
            "live": "/games/sabong/live" if ctx.can(Perm.FIGHTS_VIEW) else None,
            "schedule": "/games/sabong/schedule" if ctx.can(Perm.EVENTS_VIEW) else None,
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

    # ---- schedule --------------------------------------------------------------------
    def _events_for(self, ctx, when: str) -> list[Event]:
        today = bola.utcnow().astimezone(bola.MANILA).date()
        evs = events.list_events(ctx.db, limit=100)
        if when == "today":
            return [e for e in evs if e.event_date == today]
        if when == "upcoming":
            return sorted((e for e in evs if e.event_date > today), key=lambda e: (e.event_date, e.id))
        if when == "past":
            return [e for e in evs if e.event_date < today]
        return evs

    def schedule(self, ctx, when: str) -> list[ScheduleRow]:
        return [ScheduleRow(label=e.title, when=e.event_date.isoformat(), status=e.status.value,
                            location=e.location.name if e.location else "—",
                            detail=f"{e.fight_count} fights", url=f"/events/{e.id}")
                for e in self._events_for(ctx, when)]

    async def live(self, request, ctx):
        """Play: straight into the one open event, otherwise the event cards (web layer imported lazily)."""
        from app.web.routes_fights import sabong_live
        return await sabong_live(request, ctx)

    def schedule_context(self, ctx, when: str) -> dict:
        evs = self._events_for(ctx, when)
        return {"cards": evs, "stats": events.lobby_stats(ctx.db, [e.id for e in evs])}

    def create_spec(self, ctx) -> CreateSpec | None:
        if not ctx.can(Perm.EVENTS_CREATE):
            return None
        locs = locations.list_locations(ctx.db, active_only=True)
        need_loc = bool(ctx.store.get("event.require_location")) and bool(locs)
        today = bola.utcnow().astimezone(bola.MANILA).date().isoformat()
        quick = CreateSpec(
            title="Register location", action="/locations/quick", submit="Register", modal_id="loc-quick-modal",
            note="The Admin can add custom fields and defaults later in the Admin Panel.",
            fields=[{"name": "name", "label": "Name", "type": "text", "required": True, "attrs": 'maxlength="80"'},
                    {"name": "city", "label": "City / area", "type": "text", "attrs": 'maxlength="80"'},
                    {"name": "address", "label": "Address", "type": "text", "attrs": 'maxlength="200"'}])
        return CreateSpec(
            title="Create event", action="/events/create", modal_id="event-modal",
            modals=[quick] if ctx.can(Perm.LOCATIONS_CREATE) else [],
            note="Creating an event generates the numbered fights and one escrow pool account per fight.",
            fields=[
                {"name": "title", "label": "Event title", "type": "text", "required": True,
                 "attrs": 'maxlength="120" placeholder="Sunday Derby"'},
                {"name": "location_id", "label": "Location", "type": "select", "required": need_loc, "id": "ev-location",
                 "link": ("loc-quick-modal", "+ Register a new location") if ctx.can(Perm.LOCATIONS_CREATE) else None,
                 "options": [("", "Select a location…" if locs else "No location registered", "")] + [
                     (str(l.id), l.name + (f" · {l.city}" if l.city else ""),
                      f'data-fights="{l.default_fight_count or ""}" data-model="{l.default_payout_model or ""}" '
                      f'data-cut="{l.default_house_cut_bps if l.default_house_cut_bps is not None else ""}"')
                     for l in locs]},
                {"name": "event_date", "label": "Event date", "type": "date", "value": today, "required": True},
                {"name": "fight_count", "label": "Fights in this event", "type": "number", "required": True,
                 "value": int(ctx.store.get("event.default_fight_count")), "attrs": 'min="1" max="200"'},
                {"name": "payout_model", "label": "Payout model", "type": "select",
                 "options": [("", "Location / global default"), ("pool", "Pool (pari-mutuel) + tong"),
                             ("fixed", "Fixed odds per side")]},
                {"name": "house_cut_bps", "label": "Tong override (bps)", "type": "number",
                 "attrs": 'min="0" max="10000" placeholder="blank = use global setting"'}])

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
                url=f"/events/{x.event_id}", role=events.role_of(x, x.result.value if x.result else None) or "",
                proof_url=f"/events/{x.event_id}?fight_id={x.id}" if x.proof_file else "",
                location=" · ".join(p for p in (locs[evs[x.event_id].location_id].name,
                                               locs[evs[x.event_id].location_id].city) if p)
                if x.event_id in evs and evs[x.event_id].location_id in locs else ""))
        return out

    # ---- insights --------------------------------------------------------------------
    def insights(self, ctx, f: Filters, sort: str = "stake") -> dict | None:
        if not can_see_money(ctx):
            return None
        since, until = range_bounds(f)
        return insights_svc.sabong_insights(ctx.db, ctx.user, ctx.who, since=since, until=until,
                                            event_id=f.session_id)

    def insight_choices(self, ctx) -> dict:
        evs = events.list_events(ctx.db, limit=40)
        return {"areas": [], "session_label": "Event",
                "sessions": [(str(e.id), f"{e.event_date:%Y-%m-%d} · {e.title}") for e in evs]}

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
        """One row per fight (stake, payout, house, commission) plus one row for the event's expenses,
        so the rows add up to the event's line on the daily and archive reports."""
        from app.db.models import Commission, CommissionStatus
        event = events.get_event(ctx.db, session_id)
        live = [TicketStatus.ACTIVE, TicketStatus.SETTLED, TicketStatus.REFUNDED]
        commissions = {fid: int(total) for fid, total in ctx.db.execute(
            select(Commission.fight_id, func.coalesce(func.sum(Commission.amount_cents), 0))
            .join(Fight, Fight.id == Commission.fight_id)
            .where(Fight.event_id == event.id, Commission.status != CommissionStatus.REVERSED)
            .group_by(Commission.fight_id)).all()}
        out = []
        for fight in events.fights_of(ctx.db, event.id):
            staked, paid = ctx.db.execute(select(
                func.coalesce(func.sum(Ticket.stake_cents), 0), func.coalesce(func.sum(Ticket.payout_cents), 0)
            ).where(Ticket.fight_id == fight.id, Ticket.status.in_(live))).one()
            house = reports.fight_figures(ctx.db, fight)["system_over"]
            commission = commissions.get(fight.id, 0)
            out.append(ReportRow(label=f"Fight #{fight.number}", staked=int(staked), paid=int(paid), house=house,
                                 commission=commission, expenses=0, net=house - commission))
        expenses = reports.event_summary(ctx.db, event.id)["expenses"]
        if expenses:
            out.append(ReportRow(label="Event expenses", staked=0, paid=0, house=0, commission=0,
                                 expenses=expenses, net=-expenses))
        return out
