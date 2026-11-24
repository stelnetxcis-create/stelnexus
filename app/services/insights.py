"""Records > Insights: where the picks and the money are going, per game.

Read-only aggregates built with a handful of grouped queries (never a row-per-ticket loop), so they
stay fast on a busy draw day. Nothing here changes state.

Number game
    * KPIs for the period (tickets, staked, paid out, hit rate, house result on settled tickets)
    * **Hot numbers**: the 10 most picked numbers, with distinct tickets and the money riding on them
    * **Hot pairs**: the 10 most backed ordered pairs, tumbok and sahod shown apart
    * **By draw**: every draw in the period with its own top 3 numbers
    * By day and top bettors

Sabong
    * the same KPIs, the Meron / Wala / Draw split, the busiest fights, by event / day, top bettors

About the "backed" figure on a number: a line is a pair of numbers and its stake backs *both* of them, so
the numbers' totals add up to about twice the staked amount. Ticket counts are exact (a ticket that holds
a number on several lines still counts once).

Scope rules match the ticket lists: the Admin tier sees everything, everyone else only their own
network, and a DEV player-preview sees that player's bets only.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import case, func, select, union_all
from sqlalchemy.orm import Session

from app.core.rbac import has_global_scope, is_top, scope_ids
from app.db.models import (
    BolaDraw,
    BolaLine,
    BolaTicket,
    Event,
    Fight,
    Location,
    Side,
    Ticket,
    TicketStatus,
    User,
)

TOP = 10
LIVE = (TicketStatus.ACTIVE, TicketStatus.SETTLED)   # bets that really stand: not cancelled, not refunded


# --------------------------------------------------------------------------- shared helpers
def _pct(part: int, whole: int) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def _scope(db: Session, actor: User, model, *, global_check) -> list:
    """WHERE clauses limiting tickets to what ``actor`` may see."""
    preview_pid = getattr(actor, "preview_player_id", None)
    if preview_pid is not None:
        return [model.player_id == preview_pid]
    if global_check(actor):
        return []
    return [model.agent_id.in_(scope_ids(db, actor))]


def _kpis(db: Session, model, conds: list) -> dict:
    """Tickets / staked / paid for tickets that stand, plus cancelled count and the settled result."""
    row = db.execute(select(
        func.count(model.id),
        func.coalesce(func.sum(model.stake_cents), 0),
        func.coalesce(func.sum(case((model.status == TicketStatus.SETTLED, model.stake_cents), else_=0)), 0),
        func.coalesce(func.sum(case((model.status == TicketStatus.SETTLED, model.payout_cents), else_=0)), 0),
        func.coalesce(func.sum(case((model.status == TicketStatus.SETTLED, 1), else_=0)), 0),
        func.coalesce(func.sum(case(((model.status == TicketStatus.SETTLED) & (model.payout_cents > 0), 1),
                                    else_=0)), 0),
        func.coalesce(func.max(model.stake_cents), 0),
    ).where(*conds, model.status.in_(LIVE))).one()
    tickets, staked, settled_staked, paid, settled, winners, biggest = (int(x) for x in row)
    cancelled = int(db.scalar(select(func.count(model.id)).where(
        *conds, model.status == TicketStatus.CANCELED)) or 0)
    return {
        "tickets": tickets, "staked_cents": staked, "paid_cents": paid,
        "avg_stake_cents": staked // tickets if tickets else 0, "biggest_stake_cents": biggest,
        "settled": settled, "winners": winners, "hit_rate": _pct(winners, settled),
        "house_cents": settled_staked - paid,          # on settled tickets only; open money is not a result yet
        "cancelled": cancelled,
    }


def _names(db: Session, ctx_who, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: ctx_who(u) for u in db.scalars(select(User).where(User.id.in_(ids))).all()}


def _top_bettors(db: Session, model, conds: list, who, limit: int = TOP) -> list[dict]:
    key = func.coalesce(model.user_id, model.agent_id)
    rows = db.execute(select(
        key, func.count(model.id), func.coalesce(func.sum(model.stake_cents), 0),
        func.coalesce(func.sum(model.payout_cents), 0),
    ).where(*conds, model.status.in_(LIVE), key.is_not(None)).group_by(key)
        .order_by(func.sum(model.stake_cents).desc(), key).limit(limit)).all()
    names = _names(db, who, [r[0] for r in rows])
    total = sum(int(r[2]) for r in rows) or 1
    return [{"who": names.get(r[0], "—"), "tickets": int(r[1]), "staked_cents": int(r[2]),
             "paid_cents": int(r[3]), "share": _pct(int(r[2]), total)} for r in rows]


def _share(rows: list[dict], key: str) -> list[dict]:
    """Add ``share`` = this row's percentage of the biggest row (drives the bar widths)."""
    top = max((r[key] for r in rows), default=0)
    for r in rows:
        r["share"] = round(r[key] * 100 / top) if top else 0
    return rows


# --------------------------------------------------------------------------- number game
def _bola_conds(db: Session, actor: User, *, since, until, location_id, draw_id) -> list:
    conds = _scope(db, actor, BolaTicket, global_check=has_global_scope)
    if since is not None:
        conds.append(BolaTicket.placed_at >= since)
    if until is not None:
        conds.append(BolaTicket.placed_at < until)
    if draw_id:
        conds.append(BolaTicket.draw_id == draw_id)
    if location_id:
        conds.append(BolaTicket.draw_id.in_(select(BolaDraw.id).where(BolaDraw.location_id == location_id)))
    return conds


def _number_union(conds: list):
    """Every (number, ticket, draw, amount) a line contributes: once for its first ball, once for its second."""
    amount = BolaLine.tumbok_cents + BolaLine.sahod_cents

    def half(ball):
        return (select(ball.label("n"), BolaLine.ticket_id.label("tid"), BolaLine.draw_id.label("did"),
                       amount.label("amt"))
                .join(BolaTicket, BolaTicket.id == BolaLine.ticket_id)
                .where(*conds, BolaTicket.status.in_(LIVE)))

    return union_all(half(BolaLine.first), half(BolaLine.second)).subquery()


def hot_numbers(db: Session, conds: list, *, sort: str = "stake", limit: int = TOP) -> list[dict]:
    u = _number_union(conds)
    backed = func.coalesce(func.sum(u.c.amt), 0)
    tickets = func.count(func.distinct(u.c.tid))
    order = (tickets.desc(), backed.desc()) if sort == "tickets" else (backed.desc(), tickets.desc())
    rows = db.execute(select(u.c.n, tickets, backed).group_by(u.c.n).order_by(*order, u.c.n).limit(limit)).all()
    out = [{"number": int(n), "tickets": int(t), "backed_cents": int(b)} for n, t, b in rows]
    return _share(out, "tickets" if sort == "tickets" else "backed_cents")


def hot_pairs(db: Session, conds: list, *, limit: int = TOP) -> list[dict]:
    total = func.coalesce(func.sum(BolaLine.tumbok_cents + BolaLine.sahod_cents), 0)
    rows = db.execute(select(
        BolaLine.first, BolaLine.second, func.count(func.distinct(BolaLine.ticket_id)),
        func.coalesce(func.sum(BolaLine.tumbok_cents), 0), func.coalesce(func.sum(BolaLine.sahod_cents), 0), total,
    ).join(BolaTicket, BolaTicket.id == BolaLine.ticket_id)
        .where(*conds, BolaTicket.status.in_(LIVE))
        .group_by(BolaLine.first, BolaLine.second).order_by(total.desc(), BolaLine.first, BolaLine.second)
        .limit(limit)).all()
    out = [{"first": int(a), "second": int(b), "tickets": int(t), "tumbok_cents": int(tu),
            "sahod_cents": int(sa), "total_cents": int(to)} for a, b, t, tu, sa, to in rows]
    return _share(out, "total_cents")


def _bola_by_draw(db: Session, conds: list, limit: int = 25) -> list[dict]:
    rows = db.execute(select(
        BolaTicket.draw_id, func.count(BolaTicket.id), func.coalesce(func.sum(BolaTicket.stake_cents), 0),
        func.coalesce(func.sum(BolaTicket.payout_cents), 0),
    ).where(*conds, BolaTicket.status.in_(LIVE)).group_by(BolaTicket.draw_id)
        .order_by(BolaTicket.draw_id.desc()).limit(limit)).all()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    draws = {d.id: d for d in db.scalars(select(BolaDraw).where(BolaDraw.id.in_(ids))).all()}
    areas = {a.id: a.name for a in db.scalars(select(Location).where(
        Location.id.in_({d.location_id for d in draws.values()}))).all()}
    u = _number_union([*conds, BolaTicket.draw_id.in_(ids)])
    backed = func.coalesce(func.sum(u.c.amt), 0)
    per: dict[int, list[dict]] = {}
    for did, n, t, b in db.execute(select(u.c.did, u.c.n, func.count(func.distinct(u.c.tid)), backed)
                                   .group_by(u.c.did, u.c.n)).all():
        per.setdefault(int(did), []).append({"number": int(n), "tickets": int(t), "backed_cents": int(b)})
    out = []
    for did, tickets, staked, paid in rows:
        d = draws.get(did)
        leaders = sorted(per.get(did, []), key=lambda x: (-x["backed_cents"], -x["tickets"], x["number"]))[:3]
        out.append({"draw_id": did, "url": f"/bola/draws/{did}",
                    "label": (f"{areas.get(d.location_id, '—')} · Draw #{d.slot}" if d else f"Draw {did}"),
                    "date": d.draw_date.isoformat() if d else "",
                    "result": (d.result_label or "") if d else "",
                    "status": d.status.value if d else "",
                    "tickets": int(tickets), "staked_cents": int(staked), "paid_cents": int(paid),
                    "leaders": leaders})
    out.sort(key=lambda r: (r["date"], r["draw_id"]), reverse=True)
    return out


def _bola_by_day(db: Session, conds: list, limit: int = 31) -> list[dict]:
    rows = db.execute(select(
        BolaDraw.draw_date, func.count(BolaTicket.id), func.coalesce(func.sum(BolaTicket.stake_cents), 0),
        func.coalesce(func.sum(BolaTicket.payout_cents), 0),
    ).join(BolaDraw, BolaDraw.id == BolaTicket.draw_id)
        .where(*conds, BolaTicket.status.in_(LIVE)).group_by(BolaDraw.draw_date)
        .order_by(BolaDraw.draw_date.desc()).limit(limit)).all()
    out = [{"day": d.isoformat(), "tickets": int(t), "staked_cents": int(s), "paid_cents": int(p)}
           for d, t, s, p in rows]
    return _share(out, "staked_cents")


def bola_insights(db: Session, actor: User, who, *, since=None, until=None, location_id: int | None = None,
                  draw_id: int | None = None, sort: str = "stake") -> dict[str, Any]:
    conds = _bola_conds(db, actor, since=since, until=until, location_id=location_id, draw_id=draw_id)
    kpis = _kpis(db, BolaTicket, conds)
    return {
        "kpis": kpis,
        "numbers": hot_numbers(db, conds, sort=sort) if kpis["tickets"] else [],
        "pairs": hot_pairs(db, conds) if kpis["tickets"] else [],
        "draws": _bola_by_draw(db, conds) if kpis["tickets"] else [],
        "days": _bola_by_day(db, conds) if kpis["tickets"] else [],
        "bettors": _top_bettors(db, BolaTicket, conds, who) if kpis["tickets"] else [],
        "sort": sort,
    }


# --------------------------------------------------------------------------- sabong
def _sabong_conds(db: Session, actor: User, *, since, until, event_id) -> list:
    conds = _scope(db, actor, Ticket, global_check=is_top)
    if since is not None:
        conds.append(Ticket.placed_at >= since)
    if until is not None:
        conds.append(Ticket.placed_at < until)
    if event_id:
        conds.append(Ticket.event_id == event_id)
    return conds


def _sabong_sides(db: Session, conds: list) -> list[dict]:
    rows = {r[0].value if hasattr(r[0], "value") else str(r[0]): r for r in db.execute(select(
        Ticket.side, func.count(Ticket.id), func.coalesce(func.sum(Ticket.stake_cents), 0),
        func.coalesce(func.sum(Ticket.payout_cents), 0),
    ).where(*conds, Ticket.status.in_(LIVE)).group_by(Ticket.side)).all()}
    total = sum(int(r[2]) for r in rows.values())
    out = []
    for side in ("meron", "wala", "draw"):
        r = rows.get(side)
        staked = int(r[2]) if r else 0
        out.append({"side": side, "tickets": int(r[1]) if r else 0, "staked_cents": staked,
                    "paid_cents": int(r[3]) if r else 0, "percent": _pct(staked, total)})
    return out


def _sabong_fights(db: Session, conds: list, limit: int = TOP) -> list[dict]:
    def side_sum(side):
        return func.coalesce(func.sum(case((Ticket.side == side, Ticket.stake_cents), else_=0)), 0)

    total = func.coalesce(func.sum(Ticket.stake_cents), 0)
    rows = db.execute(select(
        Ticket.fight_id, func.count(Ticket.id), total, side_sum(Side.MERON), side_sum(Side.WALA), side_sum(Side.DRAW),
    ).where(*conds, Ticket.status.in_(LIVE)).group_by(Ticket.fight_id)
        .order_by(total.desc(), Ticket.fight_id.desc()).limit(limit)).all()
    if not rows:
        return []
    fights = {f.id: f for f in db.scalars(select(Fight).where(Fight.id.in_([r[0] for r in rows]))).all()}
    events = {e.id: e for e in db.scalars(select(Event).where(
        Event.id.in_({f.event_id for f in fights.values()}))).all()}
    out = []
    for fid, tickets, staked, meron, wala, draw in rows:
        f = fights.get(fid)
        e = events.get(f.event_id) if f else None
        sides = {"meron": int(meron), "wala": int(wala), "draw": int(draw)}
        lead = max(sides, key=lambda k: sides[k]) if staked else ""
        out.append({"label": f"{e.title if e else '—'} · Fight #{f.number if f else fid}",
                    "url": f"/events/{f.event_id}" if f else "", "tickets": int(tickets),
                    "staked_cents": int(staked), "meron_cents": sides["meron"], "wala_cents": sides["wala"],
                    "draw_cents": sides["draw"], "lead": lead,
                    "lead_percent": _pct(sides[lead], int(staked)) if lead else 0.0,
                    "result": (f.result.value if f and f.result else (f.status.value if f else ""))})
    return _share(out, "staked_cents")


def _sabong_by_event(db: Session, conds: list, limit: int = 15) -> list[dict]:
    rows = db.execute(select(
        Ticket.event_id, func.count(Ticket.id), func.coalesce(func.sum(Ticket.stake_cents), 0),
        func.coalesce(func.sum(Ticket.payout_cents), 0),
    ).where(*conds, Ticket.status.in_(LIVE)).group_by(Ticket.event_id)
        .order_by(Ticket.event_id.desc()).limit(limit)).all()
    events = {e.id: e for e in db.scalars(select(Event).where(Event.id.in_([r[0] for r in rows]))).all()} \
        if rows else {}
    out = [{"label": events[r[0]].title if r[0] in events else f"Event {r[0]}",
            "date": events[r[0]].event_date.isoformat() if r[0] in events else "",
            "url": f"/events/{r[0]}", "tickets": int(r[1]), "staked_cents": int(r[2]), "paid_cents": int(r[3])}
           for r in rows]
    return _share(out, "staked_cents")


def _sabong_by_day(db: Session, conds: list, limit: int = 31) -> list[dict]:
    rows = db.execute(select(
        Event.event_date, func.count(Ticket.id), func.coalesce(func.sum(Ticket.stake_cents), 0),
        func.coalesce(func.sum(Ticket.payout_cents), 0),
    ).join(Event, Event.id == Ticket.event_id)
        .where(*conds, Ticket.status.in_(LIVE)).group_by(Event.event_date)
        .order_by(Event.event_date.desc()).limit(limit)).all()
    out = [{"day": d.isoformat(), "tickets": int(t), "staked_cents": int(s), "paid_cents": int(p)}
           for d, t, s, p in rows]
    return _share(out, "staked_cents")


def sabong_insights(db: Session, actor: User, who, *, since=None, until=None,
                    event_id: int | None = None) -> dict[str, Any]:
    conds = _sabong_conds(db, actor, since=since, until=until, event_id=event_id)
    kpis = _kpis(db, Ticket, conds)
    live = kpis["tickets"] > 0
    return {
        "kpis": kpis,
        "sides": _sabong_sides(db, conds) if live else [],
        "fights": _sabong_fights(db, conds) if live else [],
        "events": _sabong_by_event(db, conds) if live else [],
        "days": _sabong_by_day(db, conds) if live else [],
        "bettors": _top_bettors(db, Ticket, conds, who) if live else [],
    }


__all__ = ["bola_insights", "sabong_insights", "hot_numbers", "hot_pairs", "TOP"]
