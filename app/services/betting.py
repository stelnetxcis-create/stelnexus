"""Bet placement and ticket cancellation.

Fixes vs. the original teller flow:
* a bet is one atomic unit: stake debit + escrow credit + ticket row, or nothing;
* bets are refused once a fight is locked/settled (the old panel had no visible
  cut-off, so a ticket could be sold after the result was known);
* duplicate submits are collapsed by idempotency key;
* cancellation reverses the stake instead of deleting the row, and keeps the
  cancel log the original `cancelT.php` reported on.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, LockedState, NotFound, ValidationError
from app.core.money import bps_of, fmt, odds_of
from app.core.rbac import scope_ids
from app.core.security import random_code
from app.core.settings_registry import SettingsStore
from app.db.models import (
    Account,
    Event,
    EventStatus,
    Fight,
    FightStatus,
    PayoutModel,
    Player,
    Result,
    Role,
    Side,
    Station,
    Ticket,
    TicketStatus,
    User,
)
from app.services import accounts, booklets, events, ledger, players


def pool_snapshot(db: Session, fight: Fight) -> dict:
    """Active stakes per side. ``main`` is the Meron+Wala pari-mutuel pool; Draw is a
    separate fixed-odds side bet and never dilutes it. ``total`` is every stake held."""
    rows = db.execute(
        select(Ticket.side, func.coalesce(func.sum(Ticket.stake_cents), 0), func.count(Ticket.id))
        .where(Ticket.fight_id == fight.id, Ticket.status == TicketStatus.ACTIVE)
        .group_by(Ticket.side)
    ).all()
    totals = {s.value: 0 for s in Side}
    counts = {s.value: 0 for s in Side}
    for side, amount, count in rows:
        totals[str(side)] = int(amount)
        counts[str(side)] = int(count)
    main = totals["meron"] + totals["wala"]
    return {"meron": totals["meron"], "wala": totals["wala"], "draw": totals["draw"],
            "main": main, "total": main + totals["draw"], "counts": counts}


def side_multipliers(snapshot: dict, cut_bps: int, settings: SettingsStore) -> dict:
    """Current pool multiple (bps) per side, plus whether the minimum-odds rule would
    cancel the fight if that side won. Informational; settlement recomputes."""
    min_odds = int(settings.get("payout.min_odds_bps"))
    net = snapshot["main"] - bps_of(snapshot["main"], cut_bps)
    out: dict[str, dict] = {}
    for side in ("meron", "wala"):
        pool = snapshot[side]
        bps = (net * 10000) // pool if pool else 0
        out[side] = {"bps": bps, "below_min": bool(pool and min_odds and bps < min_odds)}
    out["draw"] = {"bps": int(settings.get("payout.draw_odds_bps")), "below_min": False}
    return out


def estimate_payout(db: Session, fight: Fight, side: Side, stake_cents: int,
                    settings: SettingsStore) -> int:
    """Informational only -- the final payout is computed at settlement."""
    if side == Side.DRAW:
        return odds_of(stake_cents, int(settings.get("payout.draw_odds_bps")))
    event = events.get_event(db, fight.event_id)
    if event.payout_model == PayoutModel.FIXED:
        odds = (fight.fixed_odds_meron_bps if side == Side.MERON else fight.fixed_odds_wala_bps) \
            or int(settings.get("payout.default_fixed_odds_bps"))
        return odds_of(stake_cents, odds)
    snap = pool_snapshot(db, fight)
    main = snap["main"] + stake_cents
    cut = bps_of(main, events.house_cut_for(db, event, settings))
    net = main - cut
    side_pool = snap[side.value] + stake_cents
    return (stake_cents * net) // side_pool if side_pool else 0


def _ticket_code(db: Session, settings: SettingsStore) -> tuple[str, int | None]:
    if settings.get("booklet.use_pages_for_tickets"):
        page = booklets.take_page(db)
        if page is not None:
            return page.code, page.id
    for _ in range(20):
        code = random_code(8)
        if not db.scalar(select(Ticket.id).where(Ticket.code == code)):
            return code, None
    raise Conflict("Could not allocate a ticket code, please retry.")


def place_bet(
    db: Session, actor: User, *, fight_id: int, side: Side, stake_cents: int,
    settings: SettingsStore, player_id: int | None = None, station: Station | None = None,
    funded_by: str = "wallet", is_diy: bool = False, idempotency_key: str | None = None,
    numbers: str | None = None, kubrador: str | None = None, cavo: str | None = None,
    ip: str | None = None,
) -> Ticket:
    if idempotency_key:
        existing = db.scalar(select(Ticket).where(Ticket.idempotency_key == idempotency_key))
        if existing is not None:
            return existing

    fight = db.scalar(select(Fight).where(Fight.id == fight_id).with_for_update())
    if fight is None:
        raise NotFound("Game not found.")
    event = events.get_event(db, fight.event_id)
    if event.status != EventStatus.OPEN:
        raise LockedState("This event is not open for betting.")
    if fight.status != FightStatus.OPEN:
        raise LockedState(f"Game #{fight.number} is {fight.status} -- betting is closed.")

    minimum, maximum = int(settings.get("payout.min_stake_cents")), int(settings.get("payout.max_stake_cents"))
    if stake_cents < minimum or stake_cents > maximum:
        raise ValidationError(f"Bet must be between {fmt(minimum)} and {fmt(maximum)}.")
    if side not in (Side.MERON, Side.WALA, Side.DRAW):
        raise ValidationError("Side must be Meron, Wala or Draw.")
    if side == Side.DRAW and not settings.get("fight.draw_bets_enabled"):
        raise LockedState("Draw bets are switched off.")
    if fight.bet_mode in ("meron", "wala") and side.value != fight.bet_mode:
        raise LockedState(f"Game #{fight.number} only accepts {fight.bet_mode.upper()} bets right now.")
    if funded_by not in ("wallet", "cash"):
        raise ValidationError("Funding must be load credit or cash.")

    player: Player | None = None
    agent_id: int | None = None
    if player_id is not None:
        player = players.get_player(db, actor, player_id)
        players.assert_can_bet(db, player, stake_cents, settings)
        agent_id = player.agent_id
    if actor.role in (Role.MASTER, Role.GOLD, Role.SUB) and agent_id is None:
        agent_id = actor.id

    if funded_by == "wallet":
        if player is None:
            raise ValidationError("Load-credit bets need a registered player.")
        source = players.wallet(db, player)
    else:
        if station is None:
            raise ValidationError("Cash-funded bets need a station drawer.")
        if actor.role not in (Role.ADMIN, Role.TELLER):
            raise Forbidden("Only a teller or admin can sell cash tickets.")
        source = accounts.cash_for_station(db, station)

    pool = accounts.pool_for_fight(db, fight)
    est = estimate_payout(db, fight, side, stake_cents, settings)
    code, page_id = _ticket_code(db, settings)

    txn = ledger.post(
        db, kind="bet.stake",
        entries=[(source, +stake_cents), (pool, -stake_cents)],
        actor=actor, memo=f"Bet {code} · game #{fight.number} · {side.value}",
        ref_type="fight", ref_id=fight.id,
        idempotency_key=f"bet:{idempotency_key}" if idempotency_key else None,
    )

    ticket = Ticket(
        code=code, event_id=event.id, fight_id=fight.id,
        player_id=player.id if player else None,
        station_id=station.id if station else None,
        teller_id=actor.id, agent_id=agent_id, booklet_page_id=page_id,
        side=side, stake_cents=stake_cents, funded_by=funded_by, est_payout_cents=est,
        status=TicketStatus.ACTIVE, is_diy=is_diy, stake_txn_id=txn.id,
        idempotency_key=idempotency_key, placed_ip=ip,
        numbers=(numbers or None), kubrador=(kubrador or None), cavo=(cavo or None),
    )
    db.add(ticket)
    db.flush()
    audit.record(db, actor, "bet.place", entity_type="ticket", entity_id=ticket.id,
                 after={"code": code, "fight": fight.number, "side": side.value,
                        "stake_cents": stake_cents, "player_id": player.id if player else None})
    return ticket


def get_ticket(db: Session, ticket_id: int) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFound("Ticket not found.")
    return ticket


def get_ticket_by_code(db: Session, code: str) -> Ticket:
    ticket = db.scalar(select(Ticket).where(Ticket.code == code.strip().upper()))
    if ticket is None:
        raise NotFound("No ticket with that code.")
    return ticket


def assert_ticket_visible(db: Session, actor: User, ticket: Ticket) -> Ticket:
    if actor.role == Role.ADMIN:
        return ticket
    if actor.role == Role.TELLER:
        if ticket.teller_id != actor.id and ticket.station_id != _station_id(db, actor):
            raise Forbidden("That ticket belongs to another station.")
        return ticket
    if ticket.agent_id not in scope_ids(db, actor) and ticket.teller_id != actor.id:
        raise Forbidden("That ticket is outside your network.")
    return ticket


def _station_id(db: Session, actor: User) -> int | None:
    return db.scalar(select(Station.id).where(Station.user_id == actor.id))


def cancel_ticket(db: Session, actor: User, *, ticket_id: int, reason: str,
                  settings: SettingsStore) -> Ticket:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationError("A cancellation reason is required (min 3 characters).")
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if ticket is None:
        raise NotFound("Ticket not found.")
    assert_ticket_visible(db, actor, ticket)
    if ticket.status != TicketStatus.ACTIVE:
        raise LockedState(f"Ticket {ticket.code} is already {ticket.status}.")
    fight = events.get_fight(db, ticket.fight_id)
    if fight.status in (FightStatus.SETTLED, FightStatus.LOCKED, FightStatus.VOID):
        raise LockedState("Betting for this game is closed; tickets can no longer be cancelled.")

    if actor.role != Role.ADMIN:
        window = int(settings.get("fight.cancel_window_seconds"))
        age = (dt.datetime.now(dt.timezone.utc) - ticket.placed_at).total_seconds()
        if age > window:
            raise Forbidden(f"The {window}s cancellation window has passed. Ask an admin to cancel it.")

    stake_txn = db.get(ledger.LedgerTxn, ticket.stake_txn_id) if ticket.stake_txn_id else None
    if stake_txn is not None:
        ledger.reverse(db, stake_txn, actor=actor, reason=reason,
                       idempotency_key=f"cancel:{ticket.id}", memo=f"Cancel ticket {ticket.code}")
    before = {"status": ticket.status}
    ticket.status = TicketStatus.CANCELED
    ticket.canceled_at = dt.datetime.now(dt.timezone.utc)
    ticket.canceled_by_id = actor.id
    ticket.cancel_reason = reason[:255]
    if ticket.booklet_page_id:
        booklets.release_page(db, ticket.booklet_page_id)
        ticket.booklet_page_id = None
    db.flush()
    audit.record(db, actor, "bet.cancel", entity_type="ticket", entity_id=ticket.id,
                 before=before, after={"status": ticket.status, "code": ticket.code}, reason=reason)
    return ticket


def list_tickets(
    db: Session, actor: User, *, event_id: int | None = None, fight_id: int | None = None,
    player_id: int | None = None, status: TicketStatus | None = None, search: str | None = None,
    canceled_only: bool = False, limit: int = 100, offset: int = 0,
) -> list[Ticket]:
    stmt = select(Ticket)
    if actor.role == Role.TELLER:
        station_id = _station_id(db, actor)
        stmt = stmt.where(or_(Ticket.teller_id == actor.id, Ticket.station_id == station_id))
    elif actor.role != Role.ADMIN:
        stmt = stmt.where(Ticket.agent_id.in_(scope_ids(db, actor)))
    if event_id:
        stmt = stmt.where(Ticket.event_id == event_id)
    if fight_id:
        stmt = stmt.where(Ticket.fight_id == fight_id)
    if player_id:
        stmt = stmt.where(Ticket.player_id == player_id)
    if canceled_only:
        stmt = stmt.where(Ticket.status == TicketStatus.CANCELED)
    elif status:
        stmt = stmt.where(Ticket.status == status)
    if search:
        term = f"%{search.strip()[:40]}%"
        stmt = stmt.where(or_(Ticket.code.ilike(term), Ticket.kubrador.ilike(term), Ticket.cavo.ilike(term)))
    stmt = stmt.order_by(Ticket.id.desc()).limit(min(limit, 1000)).offset(max(offset, 0))
    return list(db.scalars(stmt).all())


def beneficiary_account(db: Session, ticket: Ticket) -> Account:
    """Where a payout or refund lands: player wallet, or the station drawer for
    walk-in cash tickets."""
    if ticket.funded_by == "wallet" and ticket.player_id:
        player = db.get(Player, ticket.player_id)
        if player is not None:
            return accounts.wallet_for_player(db, player)
    if ticket.station_id:
        station = db.get(Station, ticket.station_id)
        if station is not None:
            return accounts.cash_for_station(db, station)
    return accounts.central_cash(db)


# ---------------------------------------------------------------------------
# Counter claim (scan the ticket, hand over the cash)
# ---------------------------------------------------------------------------

def parse_scan(payload: str) -> tuple[str, str]:
    """A QR may carry the bare ticket code or the /tickets/<id> link. -> (kind, value)."""
    raw = (payload or "").strip()
    if "/tickets/" in raw:
        tail = raw.split("/tickets/", 1)[1].split("/", 1)[0].split("?", 1)[0]
        if tail.isdigit():
            return "id", tail
    return "code", raw[:24].upper()


def find_by_scan(db: Session, payload: str) -> Ticket:
    kind, value = parse_scan(payload)
    if not value:
        raise ValidationError("Scan or type a ticket code.")
    return get_ticket(db, int(value)) if kind == "id" else get_ticket_by_code(db, value)


def claim_status(ticket: Ticket) -> str:
    """none | pending | claimed | auto | lost | open"""
    if ticket.status == TicketStatus.ACTIVE:
        return "open"
    if ticket.status == TicketStatus.CANCELED:
        return "none"
    if ticket.payout_cents <= 0:
        return "lost"
    if ticket.claimed_at is not None:
        return "auto" if ticket.funded_by == "wallet" else "claimed"
    return "pending"


def claim_ticket(db: Session, actor: User, *, ticket_id: int) -> Ticket:
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if ticket is None:
        raise NotFound("Ticket not found.")
    assert_ticket_visible(db, actor, ticket)
    state = claim_status(ticket)
    if state == "open":
        raise LockedState(f"Game is not settled yet, ticket {ticket.code} cannot be claimed.")
    if state == "none":
        raise LockedState(f"Ticket {ticket.code} was cancelled and refunded.")
    if state == "lost":
        raise LockedState(f"Ticket {ticket.code} did not win. Nothing to pay.")
    if state in ("claimed", "auto"):
        when = ticket.claimed_at.strftime("%Y-%m-%d %H:%M") if ticket.claimed_at else "earlier"
        raise Conflict(f"Ticket {ticket.code} was already claimed ({when}).")
    if ticket.funded_by == "wallet":
        raise Conflict("This ticket was paid straight into the player's load credit.")
    ticket.claimed_at = dt.datetime.now(dt.timezone.utc)
    ticket.claimed_by_id = actor.id
    db.flush()
    audit.record(db, actor, "bet.claim", entity_type="ticket", entity_id=ticket.id,
                 after={"code": ticket.code, "payout_cents": ticket.payout_cents})
    return ticket
