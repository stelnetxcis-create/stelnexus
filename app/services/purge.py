"""DEV purge: hard-delete test events, games and number-game draws (Admin only).

Rules
* Ledger history is NEVER touched. Wallet balances and the ledger keep every cent that moved; the
  only change to money tables is that pool accounts lose their link to the deleted game.
* A game / draw that still holds live stakes (open or locked with ACTIVE tickets) is refused: cancel
  or void it first so the players are refunded through the normal path.
* Finished, cancelled, void and empty games can always be removed.
* Every purge is audited with a snapshot of what went away.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.db.models import (
    Account, BolaCommission, BolaDraw, BolaRun, BolaTicket, ChatMessage, ChatToken, Commission, DrawStatus,
    Event, EventExpense, Fight, FightStatus, Role, SettlementRun, Ticket, TicketStatus, User,
)


def _need_admin(actor: User) -> None:
    if actor is None or actor.role != Role.ADMIN:
        raise Forbidden("Deleting games is a DEV tool: Admin only.")


# ----------------------------------------------------------------------------- Meron / Wala

def _live_stakes(db: Session, fight_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Ticket)
                     .where(Ticket.fight_id == fight_id, Ticket.status == TicketStatus.ACTIVE)) or 0


def _wipe_fight(db: Session, fight: Fight) -> dict:
    fid = fight.id
    if fight.status not in (FightStatus.SETTLED, FightStatus.VOID) and _live_stakes(db, fid):
        raise Conflict(f"Game #{fight.number} still holds live stakes. Cancel it first so players are refunded.")
    snap = {"number": fight.number, "status": fight.status.value,
            "tickets": db.scalar(select(func.count()).select_from(Ticket).where(Ticket.fight_id == fid)) or 0}
    db.execute(update(Account).where(Account.owner_fight_id == fid).values(owner_fight_id=None))
    db.execute(update(ChatToken).where(ChatToken.used_fight_id == fid).values(used_fight_id=None))
    db.execute(delete(ChatMessage).where(ChatMessage.fight_id == fid))
    db.execute(delete(Commission).where(Commission.fight_id == fid))
    db.execute(update(SettlementRun).where(SettlementRun.fight_id == fid).values(reverses_run_id=None))
    db.execute(delete(Ticket).where(Ticket.fight_id == fid))
    db.execute(delete(SettlementRun).where(SettlementRun.fight_id == fid))
    db.expire(fight)
    db.execute(delete(Fight).where(Fight.id == fid))
    return snap


def purge_fight(db: Session, actor: User, fight_id: int) -> int:
    """Delete one game. Returns the event id it belonged to."""
    _need_admin(actor)
    fight = db.scalar(select(Fight).where(Fight.id == fight_id).with_for_update())
    if fight is None:
        raise NotFound("Game not found.")
    event = db.get(Event, fight.event_id)
    if event is not None and (db.scalar(select(func.count()).select_from(Fight).where(Fight.event_id == event.id)) or 0) <= 1:
        raise ValidationError("This is the only game in the event. Delete the whole event instead.")
    snap = _wipe_fight(db, fight)
    if event is not None:
        event.fight_count = max(1, (event.fight_count or 1) - 1)
    db.flush()
    audit.record(db, actor, "dev.purge_fight", entity_type="fight", entity_id=fight_id, before=snap)
    return event.id if event else 0


def purge_event(db: Session, actor: User, event_id: int) -> str:
    _need_admin(actor)
    event = db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if event is None:
        raise NotFound("Event not found.")
    fights = list(db.scalars(select(Fight).where(Fight.event_id == event_id)))
    blocked = [f.number for f in fights if f.status not in (FightStatus.SETTLED, FightStatus.VOID) and _live_stakes(db, f.id)]
    if blocked:
        raise Conflict("Games " + ", ".join(f"#{n}" for n in blocked[:8]) +
                       " still hold live stakes. Cancel the event first so players are refunded.")
    title, count = event.title, len(fights)
    for f in fights:
        _wipe_fight(db, f)
    db.execute(delete(EventExpense).where(EventExpense.event_id == event_id))
    db.expire(event)
    db.execute(delete(Event).where(Event.id == event_id))
    db.flush()
    audit.record(db, actor, "dev.purge_event", entity_type="event", entity_id=event_id,
                 before={"title": title, "games": count})
    return title


# ----------------------------------------------------------------------------- number game

def purge_draw(db: Session, actor: User, draw_id: int) -> str:
    _need_admin(actor)
    draw = db.scalar(select(BolaDraw).where(BolaDraw.id == draw_id).with_for_update())
    if draw is None:
        raise NotFound("Draw not found.")
    live = db.scalar(select(func.count()).select_from(BolaTicket)
                     .where(BolaTicket.draw_id == draw_id, BolaTicket.status == TicketStatus.ACTIVE)) or 0
    if live and draw.status not in (DrawStatus.SETTLED, DrawStatus.VOID):
        raise Conflict("This draw still holds live stakes. Void it first so players are refunded.")
    label = f"{draw.draw_date} slot {draw.slot}"
    n_tickets = db.scalar(select(func.count()).select_from(BolaTicket).where(BolaTicket.draw_id == draw_id)) or 0
    db.execute(update(Account).where(Account.owner_draw_id == draw_id).values(owner_draw_id=None))
    db.execute(delete(BolaCommission).where(BolaCommission.draw_id == draw_id))
    db.execute(update(BolaRun).where(BolaRun.draw_id == draw_id).values(reverses_run_id=None))
    # lines / exposure / expenses go with the draw (lines cascade from the ticket)
    from app.db.models import BolaExpense, BolaExposure, BolaLine
    db.execute(delete(BolaLine).where(BolaLine.draw_id == draw_id))
    db.execute(delete(BolaTicket).where(BolaTicket.draw_id == draw_id))
    db.execute(delete(BolaRun).where(BolaRun.draw_id == draw_id))
    db.execute(delete(BolaExposure).where(BolaExposure.draw_id == draw_id))
    db.execute(delete(BolaExpense).where(BolaExpense.draw_id == draw_id))
    db.expire(draw)
    db.execute(delete(BolaDraw).where(BolaDraw.id == draw_id))
    db.flush()
    audit.record(db, actor, "dev.purge_draw", entity_type="bola_draw", entity_id=draw_id,
                 before={"draw": label, "tickets": n_tickets})
    return label
