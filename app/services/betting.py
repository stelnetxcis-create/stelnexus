"""Bet placement and ticket cancellation.

Online-only: every bet is funded from the player's credit (wallet). There is no
cash counter, station drawer or paper booklet. Fixes vs. the original flow:
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

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core import ttlcache
from app.core.errors import Conflict, Forbidden, LockedState, NotFound, ValidationError
from app.core.money import bps_of, fmt, odds_of
from app.core.rbac import Perm, is_top, permissions_of, scope_ids
from app.core.security import random_code
from app.core.settings_registry import SettingsStore
from app.db.models import (
    NETWORK_ROLES,
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
    Ticket,
    TicketStatus,
    User,
)
from app.services import accounts, events, ledger, players


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


def pool_snapshots(db: Session, fight_ids: list[int]) -> dict[int, dict]:
    """``pool_snapshot`` for many fights in ONE grouped query (history lists used one per row)."""
    out = {fid: {"meron": 0, "wala": 0, "draw": 0, "main": 0, "total": 0,
                 "counts": {s.value: 0 for s in Side}} for fid in fight_ids}
    if not fight_ids:
        return out
    rows = db.execute(
        select(Ticket.fight_id, Ticket.side, func.coalesce(func.sum(Ticket.stake_cents), 0), func.count(Ticket.id))
        .where(Ticket.fight_id.in_(fight_ids), Ticket.status == TicketStatus.ACTIVE)
        .group_by(Ticket.fight_id, Ticket.side)).all()
    for fid, side, amount, count in rows:
        out[fid][str(side)] = int(amount)
        out[fid]["counts"][str(side)] = int(count)
    for snap in out.values():
        snap["main"] = snap["meron"] + snap["wala"]
        snap["total"] = snap["main"] + snap["draw"]
    return out


def ahead_side(snapshot: dict, llamado: str | None = None) -> str:
    """The Llamado (favourite) side: the one staff marked on the fight, otherwise the side holding more stake."""
    if llamado in ("meron", "wala"):
        return llamado
    return "wala" if snapshot["wala"] > snapshot["meron"] else "meron"


def max_odds_bps(settings: SettingsStore, side: str, snapshot: dict, llamado: str | None = None) -> int:
    """Payout ceiling for a winning side in the pool model (0 = no ceiling): the Llamado side, which carries the
    most bets, pays at most ``payout.max_llamado_bps`` (e.g. 1.5x); the Dehado side at most ``payout.max_dehado_bps``
    (e.g. 2.5x). Anything above the ceiling stays with the house."""
    key = "payout.max_llamado_bps" if side == ahead_side(snapshot, llamado) else "payout.max_dehado_bps"
    return int(settings.get(key))


def pool_figures(meron: int, wala: int, house_bps: int, platform_bps: int) -> dict:
    """The pool as the payout screen shows it. The platform cut comes off every stake first (so the pools and
    the total on screen are already after it), then the house commission comes off that total.
    gross = what was staked; platform = the platform cut; total / meron / wala = after the platform cut;
    house = commission on the total; net = what the winners share."""
    main = meron + wala
    net = main - bps_of(main, combined_bps(house_bps, platform_bps))
    platform = bps_of(main, platform_bps)
    total = main - platform
    meron_net = meron - bps_of(meron, platform_bps)
    return {"gross": main, "platform": platform, "total": total, "meron": meron_net, "wala": total - meron_net,
            "house": total - net, "net": net}


def combined_bps(house_bps: int, platform_bps: int) -> int:
    return 10000 - (10000 - platform_bps) * (10000 - house_bps) // 10000


def side_multipliers(snapshot: dict, cut_bps: int, settings: SettingsStore, llamado: str | None = None) -> dict:
    """Current pool multiple (bps) per side, capped at the Llamado / Dehado maximum, plus whether the minimum-odds
    rule would cancel the fight if that side won. Informational; settlement recomputes."""
    min_odds = int(settings.get("payout.min_odds_bps"))
    net = snapshot["main"] - bps_of(snapshot["main"], cut_bps)
    out: dict[str, dict] = {}
    for side in ("meron", "wala"):
        pool = snapshot[side]
        natural = (net * 10000) // pool if pool else 0
        ceiling = max_odds_bps(settings, side, snapshot, llamado)
        bps = min(natural, ceiling) if ceiling else natural
        out[side] = {"bps": bps, "natural": natural, "max": ceiling, "capped": bool(ceiling and natural > ceiling),
                     "below_min": bool(pool and min_odds and natural < min_odds)}
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
    cut = bps_of(main, events.pool_cut_for(db, event, settings))
    net = main - cut
    side_pool = snap[side.value] + stake_cents
    after = dict(snap, main=main, **{side.value: side_pool})
    ceiling = max_odds_bps(settings, side.value, after, fight.llamado_side)
    if ceiling and side_pool:
        net = min(net, side_pool * ceiling // 10000)
    return (stake_cents * net) // side_pool if side_pool else 0


def _ticket_code(db: Session) -> str:
    for _ in range(20):
        code = random_code(8)
        if not db.scalar(select(Ticket.id).where(Ticket.code == code)):
            return code
    raise Conflict("Could not allocate a ticket code, please retry.")


def network_names(db: Session, agent_id: int | None) -> tuple[str | None, str | None]:
    """(sub agent, agent) usernames for a ticket's owning account, read from the real upline chain.
    They are stored in the legacy ``kubrador`` / ``cavo`` columns, which now hold the Sub Agent / Agent.
    Snapshotted on the ticket so the Betting List stays correct even if the tree changes later."""
    kubrador = cavo = None
    current = db.get(User, agent_id) if agent_id else None
    for _ in range(8):
        if current is None:
            break
        if current.role == Role.SUB_AGENT and kubrador is None:
            kubrador = current.username
        elif current.role == Role.AGENT and cavo is None:
            cavo = current.username
        current = db.get(User, current.parent_id) if current.parent_id else None
    return kubrador, cavo


def _replay(existing: Ticket, *, fight_id, side, stake_cents, player_id, user_id=None) -> Ticket:
    """An idempotency-key hit is a double tap only when it is the same bet. A reused key on a
    different bet must fail loudly, not hand back the old ticket as if the new one was placed."""
    try:
        same = (existing.fight_id == int(fight_id) and existing.side == Side(side)
                and existing.stake_cents == int(stake_cents)
                and existing.player_id == (int(player_id) if player_id is not None else None)
                and existing.user_id == user_id)
    except (TypeError, ValueError):
        same = False
    if not same:
        raise Conflict("That idempotency key was already used for a different bet. "
                       "Reload the slip and try again.")
    return existing


def place_bet(
    db: Session, actor: User, *, fight_id: int, side: Side, stake_cents: int,
    settings: SettingsStore, player_id: int | None = None,
    funded_by: str = "wallet", is_diy: bool = False, idempotency_key: str | None = None,
    numbers: str | None = None, kubrador: str | None = None, cavo: str | None = None,
    ip: str | None = None, source: str | None = None,
) -> Ticket:
    if idempotency_key:
        existing = db.scalar(select(Ticket).where(Ticket.idempotency_key == idempotency_key))
        if existing is not None:
            return _replay(existing, fight_id=fight_id, side=side, stake_cents=stake_cents,
                           player_id=player_id, user_id=actor.id if player_id is None else None)

    fight = db.scalar(select(Fight).where(Fight.id == fight_id).with_for_update())
    if fight is None:
        raise NotFound("Game not found.")
    if idempotency_key:
        # A duplicate that was waiting on the game lock: the first request has committed by now,
        # so hand back its ticket instead of tripping the unique key (a double tap is not an error).
        existing = db.scalar(select(Ticket).where(Ticket.idempotency_key == idempotency_key))
        if existing is not None:
            return _replay(existing, fight_id=fight_id, side=side, stake_cents=stake_cents,
                           player_id=player_id, user_id=actor.id if player_id is None else None)
    event = events.get_event(db, fight.event_id)
    if event.status != EventStatus.OPEN:
        raise LockedState("This event is not open for betting.")
    # Livestream betting follows the game order (events.stream_phase): only the CURRENT game, while it has not
    # started, takes bets. Once it is on air everything is closed; the next game opens when it is declared.
    if source == "live":
        cur = events.current_fight(db, fight.event_id)
        phase = events.stream_phase(fight, cur.id if cur else None)
        if phase == "waiting":
            raise LockedState(f"Fight #{fight.number} is not open yet: it opens when fight #{cur.number} is done.")
        if phase == "onair":
            raise LockedState(f"Betting is closed: fight #{fight.number} has started. "
                              f"Fight #{fight.number + 1} opens when it is done.")
    if fight.status != FightStatus.OPEN:
        raise LockedState(f"Fight #{fight.number} is {fight.status} -- betting is closed.")

    minimum, maximum = int(settings.get("payout.min_stake_cents")), int(settings.get("payout.max_stake_cents"))
    if stake_cents < minimum or stake_cents > maximum:
        raise ValidationError(f"Bet must be between {fmt(minimum)} and {fmt(maximum)}.")
    if side not in (Side.MERON, Side.WALA, Side.DRAW):
        raise ValidationError("Side must be Meron, Wala or Draw.")
    if side == Side.DRAW and not settings.get("fight.draw_bets_enabled"):
        raise LockedState("Draw bets are switched off.")
    if fight.bet_mode in ("meron", "wala") and side.value != fight.bet_mode:
        raise LockedState(f"Fight #{fight.number} only accepts {fight.bet_mode.upper()} bets right now.")
    if funded_by != "wallet":
        raise ValidationError("Bets are funded from player credit only.")

    player: Player | None = None
    agent_id: int | None = None
    if player_id is not None:
        player = players.get_player(db, actor, player_id)
        players.assert_can_bet(db, player, stake_cents, settings)
        agent_id = player.agent_id
    if actor.role in NETWORK_ROLES and agent_id is None:
        agent_id = actor.id

    # No player chosen: the logged-in account bets for itself, from its own credit.
    self_bet = player is None
    if self_bet:
        if not actor.has_wallet:
            raise Forbidden(f"A {actor.role_label} account has no balance and cannot place bets.")
        players.assert_account_can_bet(db, actor, stake_cents, settings)
        source = accounts.wallet_for_user(db, actor)
        agent_id = actor.id
    else:
        source = players.wallet(db, player)

    pool = accounts.pool_for_fight(db, fight)
    est = estimate_payout(db, fight, side, stake_cents, settings)
    code = _ticket_code(db)

    txn = ledger.post(
        db, kind="bet.stake",
        entries=[(source, +stake_cents), (pool, -stake_cents)],
        actor=actor, memo=f"Bet {code} · fight #{fight.number} · {side.value}",
        ref_type="fight", ref_id=fight.id,
        idempotency_key=f"bet:{idempotency_key}" if idempotency_key else None,
    )

    chain_kubrador, chain_cavo = network_names(db, agent_id)
    ticket = Ticket(
        code=code, event_id=event.id, fight_id=fight.id,
        player_id=player.id if player else None,
        user_id=actor.id if self_bet else None,
        agent_id=agent_id,
        side=side, stake_cents=stake_cents, funded_by=funded_by, est_payout_cents=est,
        status=TicketStatus.ACTIVE, is_diy=is_diy, stake_txn_id=txn.id,
        idempotency_key=idempotency_key, placed_ip=ip,
        numbers=(numbers or None), kubrador=(kubrador or chain_kubrador), cavo=(cavo or chain_cavo),
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
    preview_pid = getattr(actor, "preview_player_id", None)
    if preview_pid is not None:
        if ticket.player_id != preview_pid:
            raise Forbidden("That ticket is not yours.")
        return ticket
    if is_top(actor):
        return ticket
    if ticket.agent_id not in scope_ids(db, actor):
        raise Forbidden("That ticket is outside your network.")
    return ticket


def cancel_ticket(db: Session, actor: User, *, ticket_id: int, reason: str,
                  settings: SettingsStore) -> Ticket:
    # Undoing a placed bet is dev-only. The routes check this too; checking it here as well means a route added
    # later (or a script) that forgets the permission still cannot cancel a bet for a player, agent or Master.
    if Perm.BETS_CANCEL not in permissions_of(actor):
        raise Forbidden("Only the developer account can cancel a placed bet.")
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
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
        raise LockedState("Betting for this fight is closed; tickets can no longer be cancelled.")

    if not is_top(actor):
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
    db.flush()
    audit.record(db, actor, "bet.cancel", entity_type="ticket", entity_id=ticket.id,
                 before=before, after={"status": ticket.status, "code": ticket.code}, reason=reason)
    return ticket


def list_tickets(
    db: Session, actor: User, *, event_id: int | None = None, fight_id: int | None = None,
    player_id: int | None = None, status: TicketStatus | None = None, search: str | None = None,
    canceled_only: bool = False, limit: int = 100, offset: int = 0,
    since: dt.datetime | None = None, until: dt.datetime | None = None,
) -> list[Ticket]:
    stmt = select(Ticket)
    preview_pid = getattr(actor, "preview_player_id", None)
    if preview_pid is not None:                 # DEV preview of a player: their own bets only
        stmt = stmt.where(Ticket.player_id == preview_pid)
    elif not is_top(actor):
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
    if since is not None:
        stmt = stmt.where(Ticket.placed_at >= since)
    if until is not None:
        stmt = stmt.where(Ticket.placed_at < until)
    if search:
        term = f"%{search.strip()[:40]}%"
        stmt = stmt.where(or_(Ticket.code.ilike(term), Ticket.kubrador.ilike(term), Ticket.cavo.ilike(term)))
    stmt = stmt.order_by(Ticket.id.desc()).limit(clamp_limit(limit, 1000)).offset(clamp_offset(offset))
    return list(db.scalars(stmt).all())


def beneficiary_account(db: Session, ticket: Ticket) -> Account:
    """Where a payout or refund lands: the player's credit wallet.

    Tickets sold before the platform went online-only may have no player; those fall back to
    the central account so historical settlements still balance."""
    if ticket.player_id:
        player = db.get(Player, ticket.player_id)
        if player is not None:
            return accounts.wallet_for_player(db, player)
    if ticket.user_id:
        bettor = db.get(User, ticket.user_id)
        if bettor is not None and bettor.has_wallet:
            return accounts.wallet_for_user(db, bettor)
    return accounts.central_cash(db)
