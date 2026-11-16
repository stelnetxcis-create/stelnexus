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
    }


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
                   "recent": []}
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
