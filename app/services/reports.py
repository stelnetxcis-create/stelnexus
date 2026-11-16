"""Reporting. Every figure is derived from the ledger, so the cashflow page can
never disagree with the wallets."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    AccountKind,
    CashRequest,
    Commission,
    CommissionStatus,
    Event,
    EventExpense,
    EventStatus,
    Fight,
    FightStatus,
    LedgerEntry,
    LedgerTxn,
    Player,
    RequestStatus,
    Role,
    SettlementRun,
    RunStatus,
    Status,
    Ticket,
    TicketStatus,
    User,
)
from app.services import ledger


def _sum_kind(db: Session, kinds: list[str], cash_kinds: list[AccountKind], positive: bool) -> int:
    op = LedgerEntry.amount_cents > 0 if positive else LedgerEntry.amount_cents < 0
    value = db.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
        .join(LedgerTxn, LedgerTxn.id == LedgerEntry.txn_id)
        .join(Account, Account.id == LedgerEntry.account_id)
        .where(LedgerTxn.kind.in_(kinds), Account.kind.in_(cash_kinds), op)
    ) or 0
    return abs(int(value))


def _kind_balance(db: Session, kind: AccountKind, *, players_only: bool = False,
                  users_only: bool = False) -> int:
    stmt = select(func.coalesce(func.sum(Account.balance_cents), 0)).where(Account.kind == kind)
    if players_only:
        stmt = stmt.where(Account.owner_player_id.is_not(None))
    if users_only:
        stmt = stmt.where(Account.owner_user_id.is_not(None))
    return int(db.scalar(stmt) or 0)


def cashflow(db: Session) -> dict:
    """Online money only: loads and cash-outs approved through the request flow."""
    online = [AccountKind.GCASH_CLEARING]
    return {
        "cash_in": _sum_kind(db, ["request.in"], online, True),
        "cash_out": _sum_kind(db, ["request.out"], online, False),
        "gcash_float": _kind_balance(db, AccountKind.GCASH_CLEARING),
        "player_balances": _kind_balance(db, AccountKind.WALLET, players_only=True),
        "agent_wallets": _kind_balance(db, AccountKind.WALLET, users_only=True),
        "commission_payable": _kind_balance(db, AccountKind.COMMISSION_PAYABLE),
        "house_revenue": _kind_balance(db, AccountKind.HOUSE_REVENUE) + _kind_balance(db, AccountKind.ROUNDING),
        "commission_expense": _kind_balance(db, AccountKind.COMMISSION_EXPENSE),
        "pool_escrow": _kind_balance(db, AccountKind.FIGHT_POOL),
        "bola_pool_escrow": _kind_balance(db, AccountKind.BOLA_POOL),
        "bola": bola_totals(db),
    }


def bola_totals(db: Session) -> dict:
    """Number-game (Bola) P&L, kept separate from sabong so each game reads on its own."""
    from app.db.models import BolaCommission, BolaTicket, CommissionStatus
    live = [TicketStatus.ACTIVE, TicketStatus.SETTLED]
    staked = int(db.scalar(select(func.coalesce(func.sum(BolaTicket.stake_cents), 0))
                           .where(BolaTicket.status.in_(live))) or 0)
    settled_stake = int(db.scalar(select(func.coalesce(func.sum(BolaTicket.stake_cents), 0))
                                  .where(BolaTicket.status == TicketStatus.SETTLED)) or 0)
    paid = int(db.scalar(select(func.coalesce(func.sum(BolaTicket.payout_cents), 0))
                         .where(BolaTicket.status == TicketStatus.SETTLED)) or 0)
    commission = int(db.scalar(select(func.coalesce(func.sum(BolaCommission.amount_cents), 0))
                               .where(BolaCommission.status != CommissionStatus.REVERSED)) or 0)
    return {"staked": staked, "settled_stake": settled_stake, "paid_out": paid,
            "house_take": settled_stake - paid, "commission": commission,
            "net": settled_stake - paid - commission}


# ---------------------------------------------------------------------------
# Bola reports: per draw, daily, monthly. All figures come from the bola tables
# (which are themselves written only by ledger-backed services), never from floats.
# ---------------------------------------------------------------------------

_BOLA_ROLLUP_KEYS = ("tickets", "winners", "staked", "settled_stake", "paid",
                     "commission", "expenses")


def _bola_rollup(db: Session, draws: list) -> dict[int, dict]:
    """One aggregate pass over the tickets / commissions / expenses of these draws."""
    from app.db.models import BolaCommission, BolaExpense, BolaTicket
    ids = [d.id for d in draws]
    out = {i: {k: 0 for k in _BOLA_ROLLUP_KEYS} for i in ids}
    if not ids:
        return out
    for draw_id, status, count, stake, payout in db.execute(
            select(BolaTicket.draw_id, BolaTicket.status, func.count(BolaTicket.id),
                   func.coalesce(func.sum(BolaTicket.stake_cents), 0),
                   func.coalesce(func.sum(BolaTicket.payout_cents), 0))
            .where(BolaTicket.draw_id.in_(ids))
            .group_by(BolaTicket.draw_id, BolaTicket.status)).all():
        row = out[draw_id]
        if status in (TicketStatus.ACTIVE, TicketStatus.SETTLED):
            row["tickets"] += int(count)
            row["staked"] += int(stake)
        if status == TicketStatus.SETTLED:
            row["settled_stake"] += int(stake)
            row["paid"] += int(payout)
    for draw_id, count in db.execute(
            select(BolaTicket.draw_id, func.count(BolaTicket.id))
            .where(BolaTicket.draw_id.in_(ids), BolaTicket.status == TicketStatus.SETTLED,
                   BolaTicket.payout_cents > 0)
            .group_by(BolaTicket.draw_id)).all():
        out[draw_id]["winners"] = int(count)
    for draw_id, amount in db.execute(
            select(BolaCommission.draw_id, func.coalesce(func.sum(BolaCommission.amount_cents), 0))
            .where(BolaCommission.draw_id.in_(ids),
                   BolaCommission.status != CommissionStatus.REVERSED)
            .group_by(BolaCommission.draw_id)).all():
        out[draw_id]["commission"] = int(amount)
    for draw_id, amount in db.execute(
            select(BolaExpense.draw_id, func.coalesce(func.sum(BolaExpense.amount_cents), 0))
            .where(BolaExpense.draw_id.in_(ids))
            .group_by(BolaExpense.draw_id)).all():
        out[draw_id]["expenses"] = int(amount)
    for row in out.values():
        row["house"] = row["settled_stake"] - row["paid"]
        row["net"] = row["house"] - row["commission"] - row["expenses"]
    return out


def _bola_sum(rows: list[dict]) -> dict:
    keys = _BOLA_ROLLUP_KEYS + ("house", "net")
    return {k: sum(int(r.get(k, 0)) for r in rows) for k in keys}


def bola_draw_report(db: Session, draw, settings) -> dict:
    """Per-draw P&L plus the risk picture: the old report_details.php, rebuilt."""
    from app.db.models import BolaCommission
    from app.services import bola
    figures = _bola_rollup(db, [draw])[draw.id]
    by_tier = {str(tier): int(amount) for tier, amount in db.execute(
        select(BolaCommission.tier, func.coalesce(func.sum(BolaCommission.amount_cents), 0))
        .where(BolaCommission.draw_id == draw.id,
               BolaCommission.status != CommissionStatus.REVERSED)
        .group_by(BolaCommission.tier)).all()}
    return {**figures, "by_tier": by_tier,
            "worst": bola.worst_outcome(db, draw.id, settings),
            "expense_rows": bola.list_expenses(db, draw.id)}


def bola_daily(db: Session, day) -> dict:
    """One date, one row per draw (area x slot), plus the day's totals."""
    from app.db.models import BolaDraw
    draws = list(db.scalars(select(BolaDraw).where(BolaDraw.draw_date == day)
                            .order_by(BolaDraw.scheduled_at, BolaDraw.id)).all())
    rollup = _bola_rollup(db, draws)
    rows = [{"draw": d, **rollup[d.id]} for d in draws]
    return {"day": day, "rows": rows, "totals": _bola_sum(rows)}


def bola_monthly(db: Session, year: int, month: int) -> dict:
    """One row per day of the month (the old monthly report capped at 3 draws a day;
    this one covers every slot of every area)."""
    from app.db.models import BolaDraw
    start = dt.date(year, month, 1)
    end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    draws = list(db.scalars(select(BolaDraw)
                            .where(BolaDraw.draw_date >= start, BolaDraw.draw_date < end)
                            .order_by(BolaDraw.draw_date)).all())
    rollup = _bola_rollup(db, draws)
    by_day: dict[str, dict] = {}
    for draw in draws:
        key = draw.draw_date.isoformat()
        row = by_day.setdefault(key, {"day": key, "draws": 0,
                                      **{k: 0 for k in _BOLA_ROLLUP_KEYS + ("house", "net")}})
        row["draws"] += 1
        for k in _BOLA_ROLLUP_KEYS + ("house", "net"):
            row[k] += rollup[draw.id][k]
    rows = [by_day[k] for k in sorted(by_day)]
    return {"month": f"{year:04d}-{month:02d}", "start": start, "end": end,
            "rows": rows, "totals": _bola_sum(rows)}


def _bola_agent_scope(db: Session, actor: User, agent_id: int | None) -> set[int] | None:
    """Which seller/earner ids a statement may cover. None = the whole network (operator tier)."""
    from app.core.rbac import assert_user_in_scope, has_global_scope, scope_ids
    if agent_id is not None:
        assert_user_in_scope(db, actor, agent_id)
        return {agent_id}
    return None if has_global_scope(actor) else scope_ids(db, actor)


def bola_agent_statement(db: Session, actor: User, *, start: dt.date, end: dt.date,
                         agent_id: int | None = None) -> dict:
    """Per-agent sales and commission statement (the old teller_report / ingresotocabo, rebuilt).

    There is no cash to remit: every stake already moved through the player's wallet. A row shows
    what the agent *sold* (tickets keyed by the seller), what those tickets paid the players, and
    what the agent *earned* in commission (rows keyed by the earner, so a Cavo's line includes the
    share taken from its Kubradors' tickets). Legacy imports and reversed commissions are left out.
    The hidden Admin never appears for anyone else.
    """
    from app.db.models import BolaCommission, BolaDraw, BolaTicket
    scope = _bola_agent_scope(db, actor, agent_id)
    in_range = (BolaDraw.draw_date >= start, BolaDraw.draw_date <= end)

    tickets = (select(BolaTicket.agent_id, BolaTicket.status, func.count(BolaTicket.id),
                      func.coalesce(func.sum(BolaTicket.stake_cents), 0),
                      func.coalesce(func.sum(BolaTicket.payout_cents), 0))
               .join(BolaDraw, BolaDraw.id == BolaTicket.draw_id)
               .where(*in_range, BolaTicket.legacy.is_(False), BolaTicket.agent_id.is_not(None)))
    if scope is not None:
        tickets = tickets.where(BolaTicket.agent_id.in_(scope))
    rows: dict[int, dict] = {}

    def row(uid: int) -> dict:
        return rows.setdefault(uid, {"id": uid, "tickets": 0, "staked": 0, "canceled": 0,
                                     "canceled_cents": 0, "paid": 0, "winners": 0, "commission": 0,
                                     "unclaimed": 0, "claimed": 0})

    for uid, status, count, stake, payout in db.execute(tickets.group_by(
            BolaTicket.agent_id, BolaTicket.status)).all():
        r = row(uid)
        if status in (TicketStatus.ACTIVE, TicketStatus.SETTLED):
            r["tickets"] += int(count)
            r["staked"] += int(stake)
        if status == TicketStatus.CANCELED:
            r["canceled"] += int(count)
            r["canceled_cents"] += int(stake)
        if status == TicketStatus.SETTLED:
            r["paid"] += int(payout)
    winners = (select(BolaTicket.agent_id, func.count(BolaTicket.id))
               .join(BolaDraw, BolaDraw.id == BolaTicket.draw_id)
               .where(*in_range, BolaTicket.legacy.is_(False), BolaTicket.agent_id.is_not(None),
                      BolaTicket.status == TicketStatus.SETTLED, BolaTicket.payout_cents > 0))
    if scope is not None:
        winners = winners.where(BolaTicket.agent_id.in_(scope))
    for uid, count in db.execute(winners.group_by(BolaTicket.agent_id)).all():
        row(uid)["winners"] = int(count)

    earned = (select(BolaCommission.agent_id, BolaCommission.status,
                     func.coalesce(func.sum(BolaCommission.amount_cents), 0))
              .join(BolaDraw, BolaDraw.id == BolaCommission.draw_id)
              .where(*in_range, BolaCommission.status != CommissionStatus.REVERSED))
    if scope is not None:
        earned = earned.where(BolaCommission.agent_id.in_(scope))
    for uid, status, amount in db.execute(earned.group_by(
            BolaCommission.agent_id, BolaCommission.status)).all():
        r = row(uid)
        r["commission"] += int(amount)
        if status == CommissionStatus.CLAIMED:
            r["claimed"] += int(amount)
        else:
            r["unclaimed"] += int(amount)

    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(list(rows)))).all()} if rows else {}
    out = []
    for uid, r in rows.items():
        user = users.get(uid)
        if user is None or (user.is_hidden and actor.role != Role.ADMIN):
            continue
        out.append({**r, "name": "System" if user.is_hidden else user.username,
                    "full_name": user.full_name, "role": str(user.role)})
    out.sort(key=lambda r: (-r["staked"], r["name"]))
    keys = ("tickets", "staked", "canceled", "canceled_cents", "paid", "winners",
            "commission", "unclaimed", "claimed")
    return {"start": start, "end": end, "rows": out,
            "totals": {k: sum(r[k] for r in out) for k in keys}}


def bola_agent_draws(db: Session, actor: User, agent_id: int, *, start: dt.date,
                     end: dt.date) -> list[dict]:
    """One agent, one row per draw in the range: what they sold, what it paid, what they earned."""
    from app.db.models import BolaCommission, BolaDraw, BolaTicket
    _bola_agent_scope(db, actor, agent_id)                  # raises if outside the actor's network
    in_range = (BolaDraw.draw_date >= start, BolaDraw.draw_date <= end)
    per: dict[int, dict] = {}

    def row(draw_id: int) -> dict:
        return per.setdefault(draw_id, {"tickets": 0, "staked": 0, "canceled": 0, "paid": 0,
                                        "commission": 0})

    for draw_id, status, count, stake, payout in db.execute(
            select(BolaTicket.draw_id, BolaTicket.status, func.count(BolaTicket.id),
                   func.coalesce(func.sum(BolaTicket.stake_cents), 0),
                   func.coalesce(func.sum(BolaTicket.payout_cents), 0))
            .join(BolaDraw, BolaDraw.id == BolaTicket.draw_id)
            .where(*in_range, BolaTicket.legacy.is_(False), BolaTicket.agent_id == agent_id)
            .group_by(BolaTicket.draw_id, BolaTicket.status)).all():
        r = row(draw_id)
        if status in (TicketStatus.ACTIVE, TicketStatus.SETTLED):
            r["tickets"] += int(count)
            r["staked"] += int(stake)
        if status == TicketStatus.CANCELED:
            r["canceled"] += int(count)
        if status == TicketStatus.SETTLED:
            r["paid"] += int(payout)
    for draw_id, amount in db.execute(
            select(BolaCommission.draw_id, func.coalesce(func.sum(BolaCommission.amount_cents), 0))
            .join(BolaDraw, BolaDraw.id == BolaCommission.draw_id)
            .where(*in_range, BolaCommission.agent_id == agent_id,
                   BolaCommission.status != CommissionStatus.REVERSED)
            .group_by(BolaCommission.draw_id)).all():
        row(draw_id)["commission"] += int(amount)
    draws = {d.id: d for d in db.scalars(select(BolaDraw).where(BolaDraw.id.in_(list(per)))).all()} if per else {}
    out = [{"draw": draws[i], **r} for i, r in per.items() if i in draws]
    out.sort(key=lambda r: (r["draw"].scheduled_at, r["draw"].id))
    return out


def bola_dashboard(db: Session, actor, settings) -> dict:
    """The dashboard's Bola card. Agents see their own network's stake, never house money."""
    from app.core.rbac import has_global_scope, scope_ids
    from app.db.models import BolaDraw, BolaRun, BolaTicket, DrawStatus
    from app.services import bola
    day = dt.datetime.now(dt.timezone.utc).astimezone(bola.MANILA).date()
    draws = list(db.scalars(select(BolaDraw).where(BolaDraw.draw_date == day)).all())
    stake_stmt = select(func.coalesce(func.sum(BolaTicket.stake_cents), 0)).where(
        BolaTicket.draw_id.in_([d.id for d in draws] or [0]),
        BolaTicket.status != TicketStatus.CANCELED)
    if not has_global_scope(actor):
        stake_stmt = stake_stmt.where(BolaTicket.agent_id.in_(scope_ids(db, actor)))
    return {
        "enabled": bool(settings.get("bola.enabled")),
        "open": sum(1 for d in draws if bola.bettable(d)),
        "awaiting": sum(1 for d in draws if d.status == DrawStatus.LOCKED),
        "settled": sum(1 for d in draws if d.status == DrawStatus.SETTLED),
        "total": len(draws),
        "stake_today": int(db.scalar(stake_stmt) or 0),
        "next": bola.next_draw(db),
        "pending_corrections": int(db.scalar(select(func.count(BolaRun.id))
                                             .where(BolaRun.status == RunStatus.PENDING)) or 0),
    }

def bola_board(db: Session, settings) -> dict | None:
    """The public /live number-game panel: last result and next draw per area. No stakes,
    no names of players or agents. None when either switch is off."""
    from app.core.rbac import bola_dev_only
    if bola_dev_only():
        return None            # the public board is never a dev surface
    if not (settings.get("live.show_bola") and settings.get("bola.enabled")):
        return None
    from app.db.models import DrawStatus
    from app.services import bola
    areas: dict[int, dict] = {}
    for draw in bola.results(db, limit=200):
        if not draw.location.is_active:
            continue           # a deactivated area is gone from the public board
        row = areas.setdefault(draw.location_id, {"name": draw.location.name, "last": None, "next": None})
        if row["last"] is None:
            row["last"] = draw
    for draw in bola.list_draws(db, upcoming=True, limit=200):
        if not draw.location.is_active:
            continue
        row = areas.setdefault(draw.location_id, {"name": draw.location.name, "last": None, "next": None})
        if row["next"] is None and draw.status in (DrawStatus.SCHEDULED, DrawStatus.OPEN, DrawStatus.LOCKED):
            row["next"] = draw
    boards = sorted((r for r in areas.values() if r["last"] or r["next"]),
                    key=lambda r: r["name"])[:12]
    if not boards:
        return None
    return {"areas": boards, "multiplier": bola.multiplier(settings) / 100,
            "bettable": bola.bettable}


def dashboard(db: Session, actor: User) -> dict:
    today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    open_fights = int(db.scalar(select(func.count(Fight.id)).where(
        Fight.status.in_([FightStatus.OPEN, FightStatus.LOCKED]))) or 0)
    stats = {
        "open_fights": open_fights,
        "events": int(db.scalar(select(func.count(Event.id))) or 0),
        "tickets_today": int(db.scalar(select(func.count(Ticket.id)).where(Ticket.placed_at >= today)) or 0),
        "stake_today": int(db.scalar(select(func.coalesce(func.sum(Ticket.stake_cents), 0))
                                     .where(Ticket.placed_at >= today,
                                            Ticket.status != TicketStatus.CANCELED)) or 0),
        "pending_requests": int(db.scalar(select(func.count(CashRequest.id))
                                          .where(CashRequest.status == RequestStatus.PENDING)) or 0),
        "pending_corrections": int(db.scalar(select(func.count(SettlementRun.id))
                                             .where(SettlementRun.status == RunStatus.PENDING)) or 0),
        "players": int(db.scalar(select(func.count(Player.id))) or 0),
        "agents": int(db.scalar(select(func.count(User.id)).where(
            User.role.not_in([Role.TELLER, Role.ADMIN]))) or 0),
        "active_agents": int(db.scalar(select(func.count(User.id)).where(
            User.role.not_in([Role.TELLER, Role.ADMIN]), User.status == Status.ACTIVE)) or 0),
    }
    stats["locked_fights"] = int(db.scalar(select(func.count(Fight.id)).where(
        Fight.status == FightStatus.LOCKED)) or 0)
    yesterday = today - dt.timedelta(days=1)
    stats["stake_yesterday"] = int(db.scalar(select(func.coalesce(func.sum(Ticket.stake_cents), 0))
                                             .where(Ticket.placed_at >= yesterday, Ticket.placed_at < today,
                                                    Ticket.status != TicketStatus.CANCELED)) or 0)
    stats["stake_delta_pct"] = (round((stats["stake_today"] - stats["stake_yesterday"]) * 100 / stats["stake_yesterday"])
                                if stats["stake_yesterday"] else None)
    stats["live_fights"] = live_fights(db)
    stats["cashflow"] = cashflow(db)
    stats["series"] = daily_series(db, days=7)
    totals = [p["stake"] for p in stats["series"]]
    stats["week_total"] = sum(totals)
    stats["week_avg"] = sum(totals) // len(totals) if totals else 0
    best = max(stats["series"], key=lambda p: p["stake"]) if stats["series"] else None
    stats["week_best"] = best if best and best["stake"] > 0 else None
    return stats


def live_fights(db: Session, limit: int = 6) -> list[dict]:
    """Open and locked games, newest first: what is happening right now."""
    rows = db.execute(
        select(Fight.id, Fight.number, Fight.status, Fight.entry_name_meron, Fight.entry_name_wala,
               Event.id.label("event_id"), Event.title.label("event_title"))
        .join(Event, Event.id == Fight.event_id)
        .where(Fight.status.in_([FightStatus.OPEN, FightStatus.LOCKED]))
        .order_by(Fight.id.desc()).limit(limit)).all()
    return [{"id": r.id, "number": r.number, "status": r.status, "meron": r.entry_name_meron,
             "wala": r.entry_name_wala, "event_id": r.event_id, "event_title": r.event_title} for r in rows]


def daily_series(db: Session, days: int = 7) -> list[dict]:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(func.date(Ticket.placed_at).label("day"),
               func.coalesce(func.sum(Ticket.stake_cents), 0).label("stake"),
               func.coalesce(func.sum(Ticket.payout_cents), 0).label("payout"),
               func.count(Ticket.id).label("tickets"))
        .where(Ticket.placed_at >= start, Ticket.status != TicketStatus.CANCELED)
        .group_by(func.date(Ticket.placed_at)).order_by(func.date(Ticket.placed_at))
    ).all()
    by_day = {str(row.day): row for row in rows}
    out = []
    for offset in range(days):
        day = (start + dt.timedelta(days=offset)).date().isoformat()
        row = by_day.get(day)
        out.append({"day": day, "stake": int(row.stake) if row else 0,
                    "payout": int(row.payout) if row else 0,
                    "tickets": int(row.tickets) if row else 0})
    return out


def agent_performance(db: Session, limit: int = 10) -> list[dict]:
    rows = db.execute(
        select(User.id, User.username, User.role,
               func.coalesce(func.sum(Commission.amount_cents), 0).label("earned"))
        .join(Commission, Commission.agent_id == User.id)
        .where(Commission.status != CommissionStatus.REVERSED, User.role != Role.ADMIN)
        .group_by(User.id).order_by(func.sum(Commission.amount_cents).desc()).limit(limit)
    ).all()
    return [{"id": r.id, "username": r.username, "role": r.role, "earned": int(r.earned)} for r in rows]


def integrity(db: Session) -> dict:
    return ledger.verify_integrity(db)


# ---------------------------------------------------------------------------
# Per-fight figures, event P&L, history, live board
# ---------------------------------------------------------------------------

def fight_figures(db: Session, fight: Fight) -> dict:
    """Numbers for one fight.

    * revolving   -- stakes still held on active tickets (money in play, not yet settled)
    * system_over -- what the house kept on the latest settlement (tong + remainder + lost draw stakes)
    * hidden_over -- the integer-division remainder inside system_over
    """
    revolving = db.scalar(select(func.coalesce(func.sum(Ticket.stake_cents), 0)).where(
        Ticket.fight_id == fight.id, Ticket.status == TicketStatus.ACTIVE)) or 0
    run = db.scalar(select(SettlementRun).where(
        SettlementRun.fight_id == fight.id, SettlementRun.status == RunStatus.APPLIED
    ).order_by(SettlementRun.version.desc()))
    totals = (run.totals or {}) if run else {}
    return {"revolving": int(revolving), "system_over": int(totals.get("house_take", 0)),
            "hidden_over": int(totals.get("remainder", 0)),
            "auto_cancel": bool(totals.get("auto_cancel", False))}


def event_summary(db: Session, event_id: int) -> dict:
    live = [TicketStatus.ACTIVE, TicketStatus.SETTLED, TicketStatus.REFUNDED]
    staked, payouts = db.execute(
        select(func.coalesce(func.sum(Ticket.stake_cents), 0), func.coalesce(func.sum(Ticket.payout_cents), 0))
        .where(Ticket.event_id == event_id, Ticket.status.in_(live))).one()
    house = 0
    for run in db.scalars(select(SettlementRun).join(Fight, Fight.id == SettlementRun.fight_id).where(
            Fight.event_id == event_id, SettlementRun.status == RunStatus.APPLIED)).all():
        house += int((run.totals or {}).get("house_take", 0))
    commission = db.scalar(select(func.coalesce(func.sum(Commission.amount_cents), 0))
                           .join(Fight, Fight.id == Commission.fight_id)
                           .where(Fight.event_id == event_id,
                                  Commission.status != CommissionStatus.REVERSED)) or 0
    expenses = db.scalar(select(func.coalesce(func.sum(EventExpense.amount_cents), 0))
                         .where(EventExpense.event_id == event_id)) or 0
    return {"staked": int(staked), "payouts": int(payouts), "house": house, "commission": int(commission),
            "expenses": int(expenses), "net": house - int(commission) - int(expenses)}


def history(db: Session, *, limit: int = 100) -> list[Fight]:
    return list(db.scalars(
        select(Fight).where(Fight.status.in_([FightStatus.SETTLED, FightStatus.VOID]))
        .order_by(Fight.declared_at.desc().nulls_last(), Fight.id.desc()).limit(min(limit, 500))).all())


def archived_events(db: Session, *, limit: int = 200) -> list[Event]:
    return list(db.scalars(select(Event).where(Event.status.in_([EventStatus.CLOSED, EventStatus.CANCELED]))
                           .order_by(Event.event_date.desc(), Event.id.desc()).limit(limit)).all())


def live_board(db: Session, settings) -> dict:
    """Everything the public board shows. Read-only; no names of players or agents."""
    from app.services import betting, events
    event = db.scalar(select(Event).where(Event.status == EventStatus.OPEN)
                      .order_by(Event.event_date.desc(), Event.id.desc()))
    board: dict = {"event": event, "fight": None, "min_odds_bps": int(settings.get("payout.min_odds_bps")),
                   "recent": [], "bola": bola_board(db, settings)}
    if event is None:
        return board
    fights = events.fights_of(db, event.id)
    current = next((f for f in fights if f.status in (FightStatus.OPEN, FightStatus.LOCKED)), None)
    board["recent"] = [f for f in reversed(fights) if f.status == FightStatus.SETTLED][:6]
    if current is not None:
        snap = betting.pool_snapshot(db, current)
        cut = events.house_cut_for(db, event, settings)
        board.update(fight=current, snapshot=snap,
                     multipliers=betting.side_multipliers(snap, cut, settings),
                     draw_enabled=bool(settings.get("fight.draw_bets_enabled")))
    return board
