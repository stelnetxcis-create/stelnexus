"""Events, fights, entry names, locking.

Fixes vs. change_name.php / betlogs.php:
* `$_GET['id']` / `$_GET['gs']` were echoed straight into HTML (reflected XSS) and
  used to build SQL. Here ids are typed integers, names are validated and every
  template escapes by default.
* `date('Y-d-m')` produced an invalid max date -> real ISO dates.
* duplicate DOM ids in the entry-name modal -> unique ids in the template.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core import ttlcache
from app.core.errors import Conflict, Forbidden, LockedState, NotFound, ValidationError
from app.core.rbac import Perm, has_perm
from app.core.settings_registry import SettingsStore
from app.db.models import (
    BetMode,
    Event,
    EventExpense,
    EventStatus,
    Fight,
    FightStatus,
    PayoutModel,
    Result,
    User,
)
from app.services import accounts

NAME_RE = re.compile(r"^[\w\s.'&/#-]{1,80}$", re.UNICODE)


def create_event(
    db: Session, actor: User, *, title: str, event_date: dt.date, settings: SettingsStore,
    fight_count: int | None = None, payout_model: PayoutModel | None = None,
    house_cut_bps: int | None = None, location_id: int | None = None,
) -> Event:
    """Master, Agents, Sub Agents and the Admin create (host) events. The location (if any) supplies defaults for
    whatever the form left blank: games, payout model, tong."""
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
    if not has_perm(actor, Perm.EVENTS_CREATE):
        raise Forbidden("Your account cannot create events.")
    title = title.strip()
    if not title or len(title) > 120:
        raise ValidationError("Event title is required (max 120 characters).")

    from app.db.models import Location
    location = None
    if location_id:
        location = db.get(Location, location_id)
        if location is None or not location.is_active:
            raise ValidationError("Choose an active location.")
    elif settings.get("event.require_location") and db.scalar(
            select(Location.id).where(Location.is_active.is_(True)).limit(1)):
        raise ValidationError("Choose the location this event is held at.")
    if location is not None:
        if not fight_count and location.default_fight_count:
            fight_count = location.default_fight_count
        if payout_model is None and location.default_payout_model:
            payout_model = PayoutModel(location.default_payout_model)
        if house_cut_bps is None and location.default_house_cut_bps is not None:
            house_cut_bps = location.default_house_cut_bps

    count = int(fight_count or settings.get("event.default_fight_count"))
    if not 1 <= count <= 200:
        raise ValidationError("Fight count must be between 1 and 200.")
    if event_date < dt.date.today() - dt.timedelta(days=365):
        raise ValidationError("Event date is too far in the past.")
    if house_cut_bps is not None and not 0 <= house_cut_bps <= 10000:
        raise ValidationError("House cut must be between 0 and 10000 basis points.")

    event = Event(
        title=title, event_date=event_date, fight_count=count,
        status=EventStatus.DRAFT,
        payout_model=payout_model or PayoutModel(str(settings.get("payout.model"))),
        house_cut_bps=house_cut_bps, created_by_id=actor.id,
        location_id=location.id if location else None,
    )
    db.add(event)
    db.flush()
    for number in range(1, count + 1):
        fight = Fight(event_id=event.id, number=number,
                      entry_name_meron="Meron", entry_name_wala="Wala", status=FightStatus.OPEN)
        db.add(fight)
        db.flush()
        accounts.pool_for_fight(db, fight)
    audit.record(db, actor, "event.create", entity_type="event", entity_id=event.id,
                 after={"title": title, "date": event_date, "fights": count,
                        "model": event.payout_model, "house_cut_bps": house_cut_bps,
                        "location": location.name if location else None})
    return event


def list_events(db: Session, *, limit: int = 50, offset: int = 0,
                status: EventStatus | None = None) -> list[Event]:
    stmt = select(Event)
    if status:
        stmt = stmt.where(Event.status == status)
    return list(db.scalars(stmt.order_by(Event.event_date.desc(), Event.id.desc())
                           .limit(clamp_limit(limit, 200)).offset(clamp_offset(offset))).all())


def lobby_stats(db: Session, event_ids: list[int]) -> dict[int, dict]:
    """Per-event numbers for the lobby boxes: how many games are open / locked / done, and which
    game is up next (the lowest-numbered game that is still open or locked)."""
    stats = {i: {"total": 0, "open": 0, "locked": 0, "settled": 0, "void": 0, "live": None, "pct": 0}
             for i in event_ids}
    if not event_ids:
        return stats
    for event_id, status, count in db.execute(
            select(Fight.event_id, Fight.status, func.count(Fight.id))
            .where(Fight.event_id.in_(event_ids)).group_by(Fight.event_id, Fight.status)).all():
        row = stats[event_id]
        row[FightStatus(status).value] += int(count)
        row["total"] += int(count)
    for event_id, number in db.execute(
            select(Fight.event_id, func.min(Fight.number))
            .where(Fight.event_id.in_(event_ids), Fight.status.in_([FightStatus.OPEN, FightStatus.LOCKED]))
            .group_by(Fight.event_id)).all():
        stats[event_id]["live"] = number
    for row in stats.values():
        done = row["settled"] + row["void"]
        row["pct"] = round(done / row["total"] * 100) if row["total"] else 0
    return stats


def get_event(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise NotFound("Event not found.")
    return event


def get_fight(db: Session, fight_id: int) -> Fight:
    fight = db.get(Fight, fight_id)
    if fight is None:
        raise NotFound("Game not found.")
    return fight


def get_fight_by_number(db: Session, event_id: int, number: int) -> Fight:
    fight = db.scalar(select(Fight).where(Fight.event_id == event_id, Fight.number == number))
    if fight is None:
        raise NotFound(f"Fight #{number} does not exist in this event.")
    return fight


def fights_of(db: Session, event_id: int) -> list[Fight]:
    return list(db.scalars(select(Fight).where(Fight.event_id == event_id).order_by(Fight.number)).all())


def set_event_status(db: Session, actor: User, event_id: int, status: EventStatus) -> Event:
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
    event = get_event(db, event_id)
    before = {"status": event.status}
    if status == EventStatus.CANCELED:
        raise ValidationError("Use Cancel event so open tickets are refunded.")
    if event.status == EventStatus.CANCELED:
        raise LockedState("A cancelled event cannot be re-opened. Create a new event instead.")
    if status == EventStatus.CLOSED:
        unsettled = [f.number for f in fights_of(db, event.id)
                     if f.status in (FightStatus.OPEN, FightStatus.LOCKED)
                     and _has_stakes(db, f)]
        if unsettled:
            raise LockedState(f"Fights with unsettled bets: {', '.join(map(str, unsettled[:10]))}.")
    event.status = status
    db.flush()
    audit.record(db, actor, "event.status", entity_type="event", entity_id=event.id,
                 before=before, after={"status": status})
    return event


def current_fight(db: Session, event_id: int) -> Fight | None:
    """The game that is on (or next on) air: the lowest-numbered game that is not settled or void yet."""
    return db.scalar(select(Fight).where(
        Fight.event_id == event_id, Fight.status.in_((FightStatus.OPEN, FightStatus.LOCKED)))
        .order_by(Fight.number).limit(1))


def fight_started(fight: Fight) -> bool:
    """A game has started once staff put it on air (``started_at``)."""
    return fight.started_at is not None


def stream_phase(fight: Fight, current_id: int | None) -> str:
    """Where one game stands for the livestream page. Betting and chat follow ONE rule, game by game:

      open     the current game, not started and still open: early betting and chat are available
      onair    the current game has started (or staff locked it): bets and chat are closed until it is declared
      waiting  a later game: nothing opens until every game before it is done (game 2 opens when 1 is declared,
               game 3 only after game 2 ...)
      ended    settled or void
    """
    if fight.status in (FightStatus.SETTLED, FightStatus.VOID):
        return "ended"
    if current_id != fight.id:
        return "waiting"
    if fight.started_at is not None or fight.status != FightStatus.OPEN:
        return "onair"
    return "open"


def stream_phase_of(db: Session, fight: Fight) -> str:
    cur = current_fight(db, fight.event_id)
    return stream_phase(fight, cur.id if cur else None)


def set_stream_live(db: Session, actor: User, event_id: int, live: bool, fight_id: int | None = None) -> Fight:
    """Put ONE game on air (its early betting / bet panel / chat panel close) or back to pre-show.

    Only the current game (the first one not yet declared) can go on air, and the games after it stay closed
    until it is done (see ``stream_phase``).
    Without ``fight_id`` it targets the current game. ``event.live_started_at`` is kept as a derived
    "something is on air" flag for the lobby and old clients."""
    from app.db.base import utcnow
    event = get_event(db, event_id)
    if event.status != EventStatus.OPEN:
        raise LockedState("Only an open event can go live.")
    fight = get_fight(db, fight_id) if fight_id else current_fight(db, event_id)
    if fight is None or fight.event_id != event.id:
        raise NotFound("There is no game to put on air.")
    if live and fight.status in (FightStatus.SETTLED, FightStatus.VOID):
        raise LockedState(f"Fight #{fight.number} has already ended.")
    if live:
        cur = current_fight(db, event_id)
        if cur is not None and cur.id != fight.id:
            raise LockedState(f"Fight #{cur.number} comes first: finish it before putting fight #{fight.number} on air.")
    before = {"started_at": fight.started_at}
    if live and fight.started_at is None:
        fight.started_at = utcnow()
        fight.auto_close_at = None               # on air: betting is closed anyway, the timer has nothing left to do
        fight.auto_close_by_id = None
    elif not live:
        fight.started_at = None
    db.flush()
    any_on_air = db.scalar(select(func.count(Fight.id)).where(
        Fight.event_id == event.id, Fight.started_at.is_not(None))) or 0
    if any_on_air and event.live_started_at is None:
        event.live_started_at = utcnow()
    elif not any_on_air:
        event.live_started_at = None
    db.flush()
    audit.record(db, actor, "event.stream", entity_type="fight", entity_id=fight.id,
                 before=before, after={"started_at": fight.started_at, "fight": fight.number})
    return fight


def _has_stakes(db: Session, fight: Fight) -> bool:
    from app.db.models import Ticket, TicketStatus
    from sqlalchemy import func
    return bool(db.scalar(select(func.count(Ticket.id)).where(
        Ticket.fight_id == fight.id, Ticket.status == TicketStatus.ACTIVE)))


def rename_entries(db: Session, actor: User, fight_id: int, *, meron: str, wala: str) -> Fight:
    fight = get_fight(db, fight_id)
    meron, wala = meron.strip(), wala.strip()
    for name in (meron, wala):
        if not NAME_RE.match(name):
            raise ValidationError("Entry names may contain letters, numbers, spaces and . ' & / # - (max 80).")
    before = {"meron": fight.entry_name_meron, "wala": fight.entry_name_wala}
    fight.entry_name_meron, fight.entry_name_wala = meron, wala
    db.flush()
    audit.record(db, actor, "fight.rename", entity_type="fight", entity_id=fight.id,
                 before=before, after={"meron": meron, "wala": wala})
    return fight


def set_fixed_odds(db: Session, actor: User, fight_id: int, *, meron_bps: int, wala_bps: int) -> Fight:
    fight = get_fight(db, fight_id)
    if fight.status == FightStatus.SETTLED:
        raise LockedState("This fight is already settled.")
    for value in (meron_bps, wala_bps):
        if not 10000 <= value <= 1000000:
            raise ValidationError("Odds must be between 1.00x and 100.00x (10000-1000000 bps).")
    before = {"meron_bps": fight.fixed_odds_meron_bps, "wala_bps": fight.fixed_odds_wala_bps}
    fight.fixed_odds_meron_bps, fight.fixed_odds_wala_bps = meron_bps, wala_bps
    db.flush()
    audit.record(db, actor, "fight.odds", entity_type="fight", entity_id=fight.id,
                 before=before, after={"meron_bps": meron_bps, "wala_bps": wala_bps})
    return fight


def lock_fight(db: Session, actor: User, fight_id: int, locked: bool = True) -> Fight:
    fight = get_fight(db, fight_id)
    if fight.status == FightStatus.SETTLED:
        raise LockedState("A settled fight cannot be re-opened for betting; use Redeclare.")
    if fight.status == FightStatus.VOID:
        raise LockedState("This game is void.")
    before = {"status": fight.status}
    fight.status = FightStatus.LOCKED if locked else FightStatus.OPEN
    fight.locked_at = dt.datetime.now(dt.timezone.utc) if locked else None
    fight.auto_close_at = None                    # a manual close / re-open replaces any running timer
    fight.auto_close_by_id = None
    db.flush()
    audit.record(db, actor, "fight.lock" if locked else "fight.unlock",
                 entity_type="fight", entity_id=fight.id, before=before, after={"status": fight.status})
    return fight


# ---------------------------------------------------------------------------
# Auto-close timer (set by the Declarator). v1 is a plain countdown. Anything smarter later (pool imbalance,
# stream phase, ticket count) plugs into ``close_reason`` and nothing else has to change.
# ---------------------------------------------------------------------------

AUTO_CLOSE_MIN_SECONDS = 5
AUTO_CLOSE_MAX_SECONDS = 3600


def close_reason(fight: Fight, now: dt.datetime | None = None) -> str | None:
    """Why this OPEN game should be closed right now, or None. The single place that decides auto-close."""
    from app.db.base import utcnow
    now = now or utcnow()
    if fight.status != FightStatus.OPEN:
        return None
    if fight.auto_close_at is not None and fight.auto_close_at <= now:
        return "timer"
    return None


def set_auto_close(db: Session, actor: User, fight_id: int, seconds: int | None) -> Fight:
    """Close betting on this game in ``seconds`` from now. ``None`` / 0 cancels the timer."""
    from app.db.base import utcnow
    fight = get_fight(db, fight_id)
    before = {"auto_close_at": fight.auto_close_at}
    if not seconds:
        if fight.auto_close_at is None:
            return fight
        fight.auto_close_at = None
        fight.auto_close_by_id = None
        db.flush()
        audit.record(db, actor, "fight.auto_close_cancel", entity_type="fight", entity_id=fight.id,
                     before=before, after={"auto_close_at": None})
        return fight
    if fight.status != FightStatus.OPEN:
        raise LockedState("Betting is already closed for this game.")
    if fight.started_at is not None:
        raise LockedState("This game is on air: betting is already closed.")
    seconds = int(seconds)
    if seconds < AUTO_CLOSE_MIN_SECONDS or seconds > AUTO_CLOSE_MAX_SECONDS:
        raise ValidationError(f"Choose between {AUTO_CLOSE_MIN_SECONDS} seconds and {AUTO_CLOSE_MAX_SECONDS // 60} minutes.")
    fight.auto_close_at = utcnow() + dt.timedelta(seconds=seconds)
    fight.auto_close_by_id = actor.id
    db.flush()
    audit.record(db, actor, "fight.auto_close_set", entity_type="fight", entity_id=fight.id,
                 before=before, after={"auto_close_at": fight.auto_close_at, "seconds": seconds})
    return fight


def close_due_fights(db: Session, event_id: int | None = None, now: dt.datetime | None = None) -> int:
    """Lock every OPEN game whose timer has run out. Called by the live polls and the Declarator Desk, so
    it happens within a poll of the deadline even when nobody is betting. Returns how many were closed."""
    from app.db.base import utcnow
    now = now or utcnow()
    q = select(Fight).where(Fight.status == FightStatus.OPEN, Fight.auto_close_at.is_not(None),
                            Fight.auto_close_at <= now)
    if event_id is not None:
        q = q.where(Fight.event_id == event_id)
    closed = 0
    for fight in db.scalars(q.with_for_update().limit(50)).all():
        if close_reason(fight, now) is None:
            continue
        before = {"status": fight.status}
        by = fight.auto_close_by_id
        fight.status = FightStatus.LOCKED
        fight.locked_at = fight.auto_close_at
        fight.auto_close_at = None
        fight.auto_close_by_id = None
        db.flush()
        audit.record(db, None, "fight.auto_close", entity_type="fight", entity_id=fight.id,
                     before=before, after={"status": fight.status, "timer_set_by": by})
        closed += 1
    if closed:
        ttlcache.invalidate_after_commit(db)
    return closed


def house_cut_for(db: Session, event: Event, settings: SettingsStore) -> int:
    return int(event.house_cut_bps if event.house_cut_bps is not None else settings.get("payout.house_cut_bps"))


def platform_cut_for(settings: SettingsStore) -> int:
    """StelNexus's disclosed platform cut (bps of the Meron + Wala pool), on top of the house commission."""
    return int(settings.get("payout.platform_cut_bps") or 0)


def combined_cut_bps(house_bps: int, platform_bps: int) -> int:
    """The platform cut comes off every bet first, then the house commission off what is left:
    1 - (1 - platform) * (1 - house). 5% + 5% -> 9.75% of the Meron + Wala pool."""
    return 10000 - (10000 - platform_bps) * (10000 - house_bps) // 10000


def pool_cut_for(db: Session, event: Event, settings: SettingsStore) -> int:
    """Everything taken out of the pool before it is shared, as one rate (bps)."""
    return combined_cut_bps(house_cut_for(db, event, settings), platform_cut_for(settings))


# ---------------------------------------------------------------------------
# Per-fight switches: force bet side, Llamado (favourite)
# ---------------------------------------------------------------------------

def set_bet_mode(db: Session, actor: User, fight_id: int, mode: str) -> Fight:
    fight = get_fight(db, fight_id)
    try:
        mode = BetMode(mode).value
    except ValueError as exc:
        raise ValidationError("Choose All, Meron only or Wala only.") from exc
    if fight.status in (FightStatus.SETTLED, FightStatus.VOID):
        raise LockedState("This fight is finished.")
    before = {"bet_mode": fight.bet_mode}
    fight.bet_mode = mode
    db.flush()
    audit.record(db, actor, "fight.bet_mode", entity_type="fight", entity_id=fight.id,
                 before=before, after={"bet_mode": mode})
    return fight


def set_llamado(db: Session, actor: User, fight_id: int, side: str | None) -> Fight:
    """Llamado = the favourite. The other side is Dehado (underdog)."""
    fight = get_fight(db, fight_id)
    side = (side or "").strip().lower() or None
    if side not in (None, "meron", "wala"):
        raise ValidationError("Llamado must be Meron, Wala or unset.")
    before = {"llamado": fight.llamado_side}
    fight.llamado_side = side
    db.flush()
    audit.record(db, actor, "fight.llamado", entity_type="fight", entity_id=fight.id,
                 before=before, after={"llamado": side})
    return fight


def resolve_result(fight: Fight, raw: str) -> Result:
    """Accepts meron | wala | draw | canceled | llamado | dehado."""
    raw = (raw or "").strip().lower()
    if raw in ("llamado", "dehado"):
        if fight.llamado_side not in ("meron", "wala"):
            raise ValidationError("Set which side is Llamado before declaring Llamado / Dehado.")
        other = "wala" if fight.llamado_side == "meron" else "meron"
        return Result(fight.llamado_side if raw == "llamado" else other)
    try:
        return Result(raw)
    except ValueError as exc:
        raise ValidationError("Choose Meron, Wala, Draw, Canceled, Llamado or Dehado.") from exc


def role_of(fight: Fight, side: str | None) -> str | None:
    """'Llamado' / 'Dehado' label for a side of this fight, if the favourite was set."""
    if side not in ("meron", "wala") or fight.llamado_side not in ("meron", "wala"):
        return None
    return "Llamado" if side == fight.llamado_side else "Dehado"


# ---------------------------------------------------------------------------
# Cancel a whole event
# ---------------------------------------------------------------------------

def cancel_event(db: Session, actor: User, event_id: int, *, reason: str,
                 settings: SettingsStore) -> dict:
    """Call off the entire event. Every unsettled fight with stakes is settled as
    CANCELED (full refund, no tong, no commission); empty fights are voided.
    Fights that were already settled are left alone -- correct those individually."""
    from app.services import settlement

    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("A written reason of at least 5 characters is required.")
    event = db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if event is None:
        raise NotFound("Event not found.")
    if event.status == EventStatus.CANCELED:
        raise Conflict("This event is already cancelled.")

    refunded = voided = skipped = 0
    for fight in fights_of(db, event.id):
        if fight.status in (FightStatus.SETTLED, FightStatus.VOID):
            skipped += 1
            continue
        if _has_stakes(db, fight):
            settlement.declare(db, actor, fight_id=fight.id, result=Result.CANCELED,
                               settings=settings, reason=f"Event cancelled: {reason}"[:255])
            refunded += 1
        else:
            fight.status = FightStatus.VOID
            fight.locked_at = dt.datetime.now(dt.timezone.utc)
            voided += 1
    before = {"status": event.status}
    event.status = EventStatus.CANCELED
    db.flush()
    audit.record(db, actor, "event.cancel", entity_type="event", entity_id=event.id,
                 before=before, after={"status": event.status, "fights_refunded": refunded,
                                       "fights_voided": voided, "fights_already_settled": skipped},
                 reason=reason)
    return {"refunded": refunded, "voided": voided, "skipped": skipped}


# ---------------------------------------------------------------------------
# Manual per-event expenses ("system fee", rentals ...)
# ---------------------------------------------------------------------------

def list_expenses(db: Session, event_id: int) -> list[EventExpense]:
    return list(db.scalars(select(EventExpense).where(EventExpense.event_id == event_id)
                           .order_by(EventExpense.id.desc())).all())


def add_expense(db: Session, actor: User, event_id: int, *, label: str, amount_cents: int,
                note: str | None = None) -> EventExpense:
    event = get_event(db, event_id)
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise ValidationError("Expense label is required (max 80 characters).")
    if amount_cents <= 0:
        raise ValidationError("Expense amount must be greater than zero.")
    row = EventExpense(event_id=event.id, label=label, amount_cents=amount_cents,
                       note=(note or "").strip()[:255] or None, created_by_id=actor.id)
    db.add(row)
    db.flush()
    audit.record(db, actor, "event.expense.add", entity_type="event", entity_id=event.id,
                 after={"label": label, "amount_cents": amount_cents})
    return row


def delete_expense(db: Session, actor: User, expense_id: int) -> EventExpense:
    row = db.get(EventExpense, expense_id)
    if row is None:
        raise NotFound("Expense not found.")
    audit.record(db, actor, "event.expense.delete", entity_type="event", entity_id=row.event_id,
                 before={"label": row.label, "amount_cents": row.amount_cents})
    db.delete(row)
    db.flush()
    return row
