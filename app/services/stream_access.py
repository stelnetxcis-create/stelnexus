"""Credit rule for watching a stream (see docs/STREAM_ACCESS_PLAN.md).

The rule
    * Everyone in the Users branch (player, sub agent, agent, master) needs credit to watch: a wallet balance of
      at least the stream minimum, which is the minimum bet (PHP 20) unless the Admin set another figure.
    * A viewer who has just bet and whose credit is now too low keeps watching until the fight they bet on is
      finished (settled or void). Then the result is shown, and only after that does the top-up screen replace
      the picture. If a win brings the balance back over the minimum, nothing is shown at all.
    * Staff and Dev accounts are never gated: they need the picture to do their job.
    * Only a stream that is actually playing is gated. A slate, an Admin "message only" and the placeholder sample
      feed have nothing to watch.

Everything is decided here on the server, per viewer, AFTER the shared cached payload is built. A locked viewer
receives a payload with no address and no embed reference at all, so there is nothing to copy.

``decide`` is pure (no database) so every row of the rule can be tested in one table.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.core.branches import USERS, branch_of
from app.db.models import (
    Account,
    CashRequest,
    Fight,
    FightStatus,
    RequestDirection,
    RequestStatus,
    Ticket,
    TicketStatus,
)

OPEN, GRACE, LOCKED = "open", "grace", "locked"
NO_CREDIT = "no_credit"
KEYS = ("stream.require_credit", "stream.min_credit_cents", "stream.grace_until_fight_end",
        "stream.result_pause_seconds")


@dataclass(frozen=True)
class Gate:
    state: str                    # open | grace | locked
    reason: str = ""              # no_credit | after_fight (only when locked)
    balance_cents: int = 0
    minimum_cents: int = 0


def decide(*, balance_cents: int, minimum_cents: int, has_pending_fight: bool, grace_on: bool = True) -> Gate:
    """The whole rule.

    ``has_pending_fight``: the viewer holds an active ticket on a fight that is not yet settled or void.
    The caller evaluates this AFTER the fight's payout is posted, so a winner is never prompted by mistake.
    """
    enough = balance_cents >= minimum_cents
    if enough:
        return Gate(OPEN, "", balance_cents, minimum_cents)
    if has_pending_fight and grace_on:
        return Gate(GRACE, "", balance_cents, minimum_cents)          # keep watching until that fight ends
    # Low credit and nothing pending: locked. A viewer who was watching a moment ago (their fight just ended) gets
    # ``delay_seconds`` of grace on the player side so the result is on screen before the card appears.
    return Gate(LOCKED, NO_CREDIT, balance_cents, minimum_cents)


def is_gated(user) -> bool:
    """Only the Users branch, and only an account that can hold credit. A DEV 'view as player' is never gated."""
    if user is None or getattr(user, "preview_player_id", None) is not None:
        return False
    return branch_of(user.role) == USERS and user.has_wallet


def settings_of(store) -> dict:
    minimum = int(store.get("stream.min_credit_cents") or 0) or int(store.get("payout.min_stake_cents") or 0)
    return {"on": bool(store.get("stream.require_credit")), "minimum": minimum,
            "grace": bool(store.get("stream.grace_until_fight_end")),
            "pause": int(store.get("stream.result_pause_seconds") or 0)}


def balance_of(db: Session, user_id: int) -> int:
    """Read-only: never creates the wallet row (this runs on a GET that every viewer repeats every few seconds)."""
    return int(db.scalar(select(Account.balance_cents).where(Account.code == f"wallet:user:{user_id}")) or 0)


def has_pending_fight(db: Session, user_id: int, *, fight_id: int | None = None, event_id: int | None = None) -> bool:
    """An active ticket on a fight that is not finished, limited to what the viewer is watching:

    * ``fight_id`` (the fight page, the per-fight stream): a ticket on THAT fight only.
    * ``event_id``: a ticket on a not-finished fight of that event.
    * neither (the global stream): a ticket on a fight that is on air right now (started or locked).

    Never "any open fight anywhere": old fights that were never declared would otherwise keep a viewer watching
    for free forever.
    """
    where = [Ticket.user_id == user_id, Ticket.status == TicketStatus.ACTIVE, Ticket.fight_id == Fight.id,
             Fight.status.in_([FightStatus.OPEN, FightStatus.LOCKED])]
    if fight_id is not None:
        where.append(Fight.id == fight_id)
    elif event_id is not None:
        where.append(Fight.event_id == event_id)
    else:
        where.append(or_(Fight.status == FightStatus.LOCKED, Fight.started_at.is_not(None)))
    return bool(db.scalar(select(exists().where(*where))))


def has_pending_load(db: Session, user_id: int) -> bool:
    return bool(db.scalar(select(exists().where(
        CashRequest.requested_by_id == user_id, CashRequest.direction == RequestDirection.IN,
        CashRequest.status == RequestStatus.PENDING))))


def check(db: Session, user, store, *, fight_id: int | None = None, event_id: int | None = None) -> Gate | None:
    """The gate for this viewer, or None when the rule does not apply to them (off, exempt, staff)."""
    cfg = settings_of(store)
    if not cfg["on"] or not is_gated(user):
        return None
    balance = balance_of(db, user.id)
    if balance >= cfg["minimum"]:
        return Gate(OPEN, "", balance, cfg["minimum"])
    return decide(balance_cents=balance, minimum_cents=cfg["minimum"], grace_on=cfg["grace"],
                  has_pending_fight=has_pending_fight(db, user.id, fight_id=fight_id, event_id=event_id))


def apply(db: Session, user, store, data: dict, *, fight_id: int | None = None, event_id: int | None = None) -> dict:
    """Return the payload this viewer may receive. ``data`` is the shared payload and is never modified.
    ``fight_id`` / ``event_id`` say what the viewer is watching, so "the fight they bet on" means that one."""
    if data.get("status") != "playing":
        return data                                    # slate / off / sample feed: nothing real to protect
    gate = check(db, user, store, fight_id=fight_id, event_id=event_id)
    if gate is None or gate.state == OPEN:
        return data
    cfg = settings_of(store)
    info = {"state": gate.state, "reason": gate.reason, "balance_cents": gate.balance_cents,
            "min_cents": gate.minimum_cents, "need_cents": max(0, gate.minimum_cents - gate.balance_cents),
            "topup_url": "/wallet", "pending_load": False, "delay_seconds": 0}
    if gate.state == GRACE:
        return {**data, "gate": info}                  # still playing: the player shows a small banner
    info["pending_load"] = has_pending_load(db, user.id)
    info["delay_seconds"] = cfg["pause"]
    # Locked: keep only what the player needs to draw the card. No address, no embed, no source id.
    keep = {k: data.get(k) for k in ("dvr_window", "autoplay_muted", "rev")}
    return {**keep, "status": "locked", "scope": data.get("scope"), "kind": None, "mode": None, "src": None,
            "embed": None, "loop": False, "start": None, "message": None, "chain_position": None, "name": None,
            "source_id": None, "gate": info}
