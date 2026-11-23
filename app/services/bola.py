"""Bola: the two-number draw game (Tumbok-Sahod) brought over from the legacy PHP panel.

The game
--------
Numbers 1..``bola.max_number`` (40). A draw produces two **ordered** balls (first, second).
A ticket line is an ordered pair ``(a, b)`` with two separate stakes:

    tumbok stake  pays stake x M  if the draw is exactly (a, b)
    sahod  stake  pays stake x M  if the draw is the reverse, (b, a)

M = ``bola.payout_multiplier_x100`` / 100 (800x by default). Ticket kinds expand into C(n,2)
lines, in entry order, exactly like the old teller screen:

    2 numbers -> 1 line | 3 -> 3 | 4 -> 6 | 5 -> 10      total = (tumbok + sahod) * lines

What is deliberately NOT ported
-------------------------------
* plaintext passwords, string-built SQL, "credit = cumulative cash collected";
* anonymous walk-in tickets with a free-text nickname -- a ticket is always the logged-in account's own bet;
* the dead ``limit_combination_checker`` (it read undefined variables and always computed 0).
  ``check_limits`` below implements the same intent correctly, plus a liability cap the old
  panel never had;
* inserting the parent ticket before validating -- everything here is one transaction.

Money is integer centavos and moves only through the double-entry ledger:
    bet     wallet(account) +stake | pool(draw) -stake   kind "bola.stake"
    cancel  ledger.reverse(stake txn)                        kind "bola.stake.reversal"
Settlement and commissions live in ``bola_settlement`` / ``commissions``.
"""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core.errors import Conflict, Forbidden, InsufficientFunds, LockedState, NotFound, ValidationError
from app.core.money import fmt, payout_of
from app.core.rbac import Perm, has_global_scope, is_top, permissions_of, scope_ids
from app.core.security import random_code
from app.core.settings_registry import SettingsStore
from app.db.base import utcnow
from app.db.models import (
    KIND_LINES,
    KIND_NUMBERS,
    BolaDraw,
    BolaExposure,
    BolaExpense,
    BolaKind,
    BolaLine,
    BolaSchedule,
    BolaTicket,
    DrawStatus,
    Location,
    Role,
    TicketStatus,
    User,
)
from app.services import accounts, ledger, players

Pair = tuple[int, int]


def _manila() -> dt.tzinfo:
    """Asia/Manila. The Philippines has had no DST since 1990, so a fixed +08:00 is a safe
    fallback on a host without the tz database installed."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Manila")
    except Exception:  # noqa: BLE001 - missing tzdata must not break scheduling
        return dt.timezone(dt.timedelta(hours=8), "PHT")


MANILA = _manila()


def local(value: dt.datetime | None) -> dt.datetime | None:
    """A stored UTC instant as Manila wall time (for display and for the schedule form)."""
    return value.astimezone(MANILA) if value is not None else None


def local_instant(day: dt.date, wall_time: dt.time) -> dt.datetime:
    """Manila local date + time -> the UTC instant to store."""
    return dt.datetime.combine(day, wall_time, tzinfo=MANILA).astimezone(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Rules (pure helpers: no database, trivially testable)
# ---------------------------------------------------------------------------

def assert_enabled(settings: SettingsStore) -> None:
    if not settings.get("bola.enabled"):
        raise LockedState("The number game is switched off.")


def preflight(settings: SettingsStore, *, dev_only: bool, live_ack: bool,
              active_schedules: int) -> list[tuple[str, str]]:
    """Pre-deploy checks for the number game: [("ok" | "warn" | "fail", message), ...].

    Three states: OFF (nothing to check), DEV-ONLY (Admin sees it, harmless) and LIVE (every
    role can reach it). LIVE is where a mistake costs money, so it must be acknowledged
    explicitly, have both liability caps set, show the odds to players, and carry no wording
    that implies a government affiliation (plan section 0.1).
    """
    out: list[tuple[str, str]] = []
    enabled = bool(settings.get("bola.enabled"))
    if not enabled:
        out.append(("ok", "Number game is OFF (bola.enabled=false)."))
        if not dev_only:
            out.append(("warn", "BOLA_DEV_ONLY=false: turning bola.enabled on would open the game "
                                "to every role. Keep BOLA_DEV_ONLY=true until you mean to launch."))
        return out
    if settings.get("bola.allow_duplicate_balls"):
        out.append(("fail", "bola.allow_duplicate_balls is on, but a ticket can never hold a pair like 5,5: "
                            "a doubled draw would always favour the house and the shown odds would be wrong. "
                            "Turn it off."))
    header = str(settings.get("ticket.header") or "")
    if any(word in header.upper() for word in ("PCSO", "OFFICIAL", "PAGCOR")):
        out.append(("fail", f"ticket.header {header!r} implies a government affiliation. Use your own name."))
    if active_schedules == 0:
        out.append(("warn", "No active draw times (add them at /admin/bola/schedules): "
                            "bola-generate will create nothing."))
    if dev_only:
        out.append(("ok", "Number game is ON but DEV-ONLY: only the Admin account can reach it."))
        return out
    out.append(("warn", "Number game is LIVE for all roles."))
    if not live_ack:
        out.append(("fail", "BOLA_LIVE_ACK=1 is not set. Confirm you are authorised to run this game "
                            "(plan section 0.1), then set BOLA_LIVE_ACK=1 for this check."))
    if int(settings.get("bola.max_liability_per_outcome_cents")) <= 0:
        out.append(("fail", "bola.max_liability_per_outcome_cents is 0 (no cap): one outcome could owe "
                            "more than the house holds."))
    if int(settings.get("bola.max_liability_per_draw_cents")) <= 0:
        out.append(("fail", "bola.max_liability_per_draw_cents is 0 (no cap) for the worst outcome of a draw."))
    if not settings.get("bola.show_odds_to_players"):
        out.append(("fail", "bola.show_odds_to_players is off: players must be shown the multiplier and odds."))
    return out


def multiplier(settings: SettingsStore) -> int:
    return int(settings.get("bola.payout_multiplier_x100"))


def effective_caps(settings: SettingsStore, location) -> tuple[int, int, int]:
    """(line limit, outcome cap, draw cap) for one area, in cents.

    A cap set on the Location wins (0 = no cap in that area); NULL follows the global
    ``bola.*`` setting.
    """
    def pick(override, key: str) -> int:
        return int(override) if override is not None else int(settings.get(key))
    return (
        pick(location.bola_limit_per_line_cents, "bola.limit_per_line_cents"),
        pick(location.bola_max_liability_per_outcome_cents, "bola.max_liability_per_outcome_cents"),
        pick(location.bola_max_liability_per_draw_cents, "bola.max_liability_per_draw_cents"),
    )


def max_number(settings: SettingsStore) -> int:
    return int(settings.get("bola.max_number"))


def ordered_outcomes(settings: SettingsStore) -> int:
    """How many ordered results are possible: 40 x 39 = 1,560 with distinct balls."""
    n = max_number(settings)
    return n * n if settings.get("bola.allow_duplicate_balls") else n * (n - 1)


def rtp_percent(settings: SettingsStore) -> float:
    """Return to player, in percent. 800 / 1560 = 51.28%. Shown on the slip when disclosed."""
    outcomes = ordered_outcomes(settings)
    return round(multiplier(settings) / 100 / outcomes * 100, 2) if outcomes else 0.0


def expand_lines(kind: BolaKind | str, numbers) -> list[Pair]:
    """C(n,2) combinations in entry order: (1,2) (1,3) (1,4) (2,3) (2,4) (3,4) ...

    This is the exact order the legacy teller inserted rows in, which matters for receipts.
    """
    kind = BolaKind(kind)
    nums = list(numbers or [])
    wanted = KIND_NUMBERS[kind]
    if len(nums) != wanted:
        raise ValidationError(f"{kind.label} needs exactly {wanted} numbers.")
    return [(int(a), int(b)) for a, b in itertools.combinations(nums, 2)]


def validate_numbers(numbers, kind: BolaKind | str, settings: SettingsStore) -> list[int]:
    """Count, integer, range and (unless D1 allows repeats) distinctness."""
    kind = BolaKind(kind)
    wanted = KIND_NUMBERS[kind]
    raw = list(numbers or [])
    if len(raw) != wanted:
        raise ValidationError(f"{kind.label} needs exactly {wanted} numbers.")
    ceiling = max_number(settings)
    out: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValidationError("Numbers must be whole numbers.")
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError("Numbers must be whole numbers.") from exc
        if number < 1 or number > ceiling:
            raise ValidationError(f"Each number must be between 1 and {ceiling}.")
        out.append(number)
    if len(set(out)) != len(out):
        raise ValidationError("The numbers on one ticket must be different.")
    return out


def validate_stakes(tumbok_cents: int, sahod_cents: int, settings: SettingsStore) -> tuple[int, int]:
    tumbok, sahod = int(tumbok_cents), int(sahod_cents)
    if tumbok < 0 or sahod < 0:
        raise ValidationError("Stakes cannot be negative.")
    if tumbok == 0 and sahod == 0:
        raise ValidationError("Put a stake on tumbok, sahod, or both.")
    minimum = int(settings.get("bola.min_line_stake_cents"))
    for label, amount in (("Tumbok", tumbok), ("Sahod", sahod)):
        if amount and amount < minimum:
            raise ValidationError(f"{label} must be at least {fmt(minimum)} per line (or 0).")
    return tumbok, sahod


def ticket_stake(kind: BolaKind | str, tumbok_cents: int, sahod_cents: int) -> int:
    return (int(tumbok_cents) + int(sahod_cents)) * KIND_LINES[BolaKind(kind)]


def line_payouts(tumbok_cents: int, sahod_cents: int, settings: SettingsStore) -> tuple[int, int]:
    rate = multiplier(settings)
    return payout_of(tumbok_cents, rate), payout_of(sahod_cents, rate)


def slip_preview(kind: BolaKind | str, numbers, tumbok_cents: int, sahod_cents: int,
                 settings: SettingsStore) -> dict:
    """Everything the bet slip shows before submitting. No state change, no wallet touch.

    ``max_payout_cents`` is the most this ticket can win: only one ordered outcome can come in,
    so it is the best single line, NOT lines x (tumbok+sahod) x M.
    """
    kind = BolaKind(kind)
    nums = validate_numbers(numbers, kind, settings)
    tumbok, sahod = validate_stakes(tumbok_cents, sahod_cents, settings)
    lines = expand_lines(kind, nums)
    t_pay, s_pay = line_payouts(tumbok, sahod, settings)
    cap = int(settings.get("payout.max_payout_cents")) or None
    best = max(t_pay, s_pay)
    if cap:
        best = min(best, cap)
    return {
        "kind": str(kind), "numbers": nums, "line_count": len(lines),
        "lines": [{"first": a, "second": b, "tumbok_cents": tumbok, "sahod_cents": sahod,
                   "tumbok_payout_cents": t_pay, "sahod_payout_cents": s_pay} for a, b in lines],
        "tumbok_cents": tumbok, "sahod_cents": sahod,
        "per_line_cents": tumbok + sahod,
        "stake_cents": (tumbok + sahod) * len(lines),
        "max_payout_cents": best,
        "multiplier_x100": multiplier(settings),
        "outcomes": ordered_outcomes(settings),
        "rtp_percent": rtp_percent(settings),
    }


# ---------------------------------------------------------------------------
# Schedules (old tbl_area.ball_1 / ball_2 / ball_3)
# ---------------------------------------------------------------------------

def schedules(db: Session, location_id: int | None = None) -> list[BolaSchedule]:
    stmt = select(BolaSchedule)
    if location_id:
        stmt = stmt.where(BolaSchedule.location_id == location_id)
    return list(db.scalars(stmt.order_by(BolaSchedule.location_id, BolaSchedule.slot)).all())


def parse_time(raw: str) -> dt.time:
    text = (raw or "").strip()
    try:
        parts = text.split(":")
        hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return dt.time(hour, minute)
    except (ValueError, IndexError) as exc:
        raise ValidationError("Use a draw time like 11:00 or 21:30 (Manila time).") from exc


def upsert_schedule(db: Session, actor: User, *, location_id: int, slot: int,
                    draw_time: str | dt.time, is_active: bool = True) -> BolaSchedule:
    location = db.get(Location, location_id)
    if location is None:
        raise NotFound("Area not found.")
    slot = int(slot)
    if not 1 <= slot <= 6:
        raise ValidationError("Slot must be between 1 and 6.")
    wall = draw_time if isinstance(draw_time, dt.time) else parse_time(draw_time)
    row = db.scalar(select(BolaSchedule).where(BolaSchedule.location_id == location_id,
                                               BolaSchedule.slot == slot))
    before = {"draw_time": str(row.draw_time), "is_active": row.is_active} if row else None
    if row is None:
        row = BolaSchedule(location_id=location_id, slot=slot, draw_time=wall, is_active=is_active)
        db.add(row)
    else:
        row.draw_time, row.is_active = wall, is_active
    db.flush()
    audit.record(db, actor, "bola.schedule.save", entity_type="bola_schedule", entity_id=row.id,
                 before=before, after={"area": location.name, "slot": slot,
                                       "draw_time": str(wall), "is_active": is_active})
    return row


def delete_schedule(db: Session, actor: User, schedule_id: int) -> None:
    row = db.get(BolaSchedule, schedule_id)
    if row is None:
        raise NotFound("Draw time not found.")
    audit.record(db, actor, "bola.schedule.delete", entity_type="bola_schedule", entity_id=row.id,
                 before={"location_id": row.location_id, "slot": row.slot,
                         "draw_time": str(row.draw_time)})
    db.delete(row)
    db.flush()


# ---------------------------------------------------------------------------
# Draws
# ---------------------------------------------------------------------------

def get_draw(db: Session, draw_id: int) -> BolaDraw:
    draw = db.get(BolaDraw, draw_id)
    if draw is None:
        raise NotFound("Draw not found.")
    return draw


def _lock_draw(db: Session, draw_id: int) -> BolaDraw:
    draw = db.scalar(select(BolaDraw).where(BolaDraw.id == draw_id).with_for_update())
    if draw is None:
        raise NotFound("Draw not found.")
    return draw


def cutoff_for(scheduled_at: dt.datetime, settings: SettingsStore) -> dt.datetime:
    return scheduled_at - dt.timedelta(minutes=int(settings.get("bola.cutoff_minutes")))


def create_draw(db: Session, actor: User, *, location_id: int, draw_date: dt.date, slot: int,
                settings: SettingsStore, draw_time: str | dt.time | None = None,
                scheduled_at: dt.datetime | None = None, note: str | None = None,
                status: DrawStatus = DrawStatus.SCHEDULED) -> BolaDraw:
    """One area x date x slot. Idempotent on that triple (the old panel refused a second event
    outright; here every area gets its own draws)."""
    location = db.get(Location, location_id)
    if location is None:
        raise NotFound("Area not found.")
    slot = int(slot)
    if not 1 <= slot <= 6:
        raise ValidationError("Slot must be between 1 and 6.")
    if scheduled_at is None:
        if draw_time is None:
            row = db.scalar(select(BolaSchedule).where(BolaSchedule.location_id == location_id,
                                                       BolaSchedule.slot == slot))
            if row is None:
                raise ValidationError("Set this area's draw time first, or give a draw time.")
            draw_time = row.draw_time
        wall = draw_time if isinstance(draw_time, dt.time) else parse_time(draw_time)
        scheduled_at = local_instant(draw_date, wall)
    existing = db.scalar(select(BolaDraw).where(BolaDraw.location_id == location_id,
                                                BolaDraw.draw_date == draw_date,
                                                BolaDraw.slot == slot))
    if existing is not None:
        raise Conflict(f"{location.name} already has a slot {slot} draw on {draw_date}.")
    draw = BolaDraw(
        location_id=location_id, draw_date=draw_date, slot=slot, scheduled_at=scheduled_at,
        cutoff_at=cutoff_for(scheduled_at, settings), status=status,
        note=(note or "").strip()[:120] or None,
    )
    db.add(draw)
    db.flush()
    accounts.pool_for_draw(db, draw)
    audit.record(db, actor, "bola.draw.create", entity_type="bola_draw", entity_id=draw.id,
                 after={"area": location.name, "date": draw_date, "slot": slot,
                        "scheduled_at": scheduled_at, "cutoff_at": draw.cutoff_at})
    return draw


def generate_draws(db: Session, actor: User | None, *, settings: SettingsStore,
                   date: dt.date | None = None, days: int = 1) -> dict:
    """One draw per active schedule row per day. Skips 00:00 rows (legacy "no draw") and
    anything that already exists, so running it twice is harmless."""
    start = date or utcnow().astimezone(MANILA).date()
    days = max(1, min(int(days), 31))
    created: list[BolaDraw] = []
    skipped = 0
    rows = [s for s in schedules(db) if s.is_active and s.draw_time != dt.time(0, 0)]
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        for row in rows:
            exists = db.scalar(select(BolaDraw.id).where(BolaDraw.location_id == row.location_id,
                                                         BolaDraw.draw_date == day,
                                                         BolaDraw.slot == row.slot))
            if exists:
                skipped += 1
                continue
            instant = local_instant(day, row.draw_time)
            draw = BolaDraw(
                location_id=row.location_id, draw_date=day, slot=row.slot, scheduled_at=instant,
                cutoff_at=cutoff_for(instant, settings), status=DrawStatus.SCHEDULED,
            )
            db.add(draw)
            db.flush()
            accounts.pool_for_draw(db, draw)
            created.append(draw)
    if created:
        audit.record(db, actor, "bola.draw.generate", entity_type="bola_draw",
                     after={"from": start, "days": days, "created": len(created), "skipped": skipped})
    return {"created": created, "skipped": skipped}


def set_status(db: Session, actor: User, draw_id: int, status: DrawStatus) -> BolaDraw:
    """Manual open / lock (the old opbet / clbet). Settled and void draws are never re-opened."""
    draw = _lock_draw(db, draw_id)
    if status not in (DrawStatus.OPEN, DrawStatus.LOCKED):
        raise ValidationError("A draw can only be opened or locked here.")
    if draw.status in (DrawStatus.SETTLED, DrawStatus.VOID):
        raise LockedState(f"This draw is {draw.status}; it cannot take bets again.")
    if status == DrawStatus.OPEN and utcnow() >= draw.cutoff_at:
        raise LockedState("The cut-off for this draw has passed; it cannot be re-opened.")
    before = {"status": str(draw.status)}
    draw.status = status
    draw.locked_at = utcnow() if status == DrawStatus.LOCKED else None
    db.flush()
    audit.record(db, actor, f"bola.draw.{status.value}", entity_type="bola_draw", entity_id=draw.id,
                 before=before, after={"status": str(status)})
    return draw


def open_draw(db: Session, actor: User, draw_id: int) -> BolaDraw:
    return set_status(db, actor, draw_id, DrawStatus.OPEN)


def lock_draw(db: Session, actor: User, draw_id: int) -> BolaDraw:
    return set_status(db, actor, draw_id, DrawStatus.LOCKED)


def tick(db: Session, settings: SettingsStore, *, now: dt.datetime | None = None,
         actor: User | None = None) -> dict:
    """Display-only status flips for cron (`python -m app.cli bola-tick`).

    Betting never depends on this: `place_bola_bet` checks the clock itself inside the
    transaction, which is what actually stops a bet after the cut-off.
    """
    now = now or utcnow()
    lead = dt.timedelta(hours=int(settings.get("bola.open_hours_before")))
    opened = locked = 0
    rows = db.scalars(select(BolaDraw).where(
        BolaDraw.status.in_([DrawStatus.SCHEDULED, DrawStatus.OPEN])).with_for_update()).all()
    for draw in rows:
        if now >= draw.cutoff_at:
            if draw.status == DrawStatus.OPEN or now >= draw.scheduled_at:
                draw.status = DrawStatus.LOCKED
                draw.locked_at = draw.locked_at or now
                locked += 1
                audit.record(db, actor, "bola.draw.lock", entity_type="bola_draw", entity_id=draw.id,
                             after={"by": "tick", "cutoff_at": draw.cutoff_at})
        elif draw.status == DrawStatus.SCHEDULED and now >= draw.scheduled_at - lead:
            draw.status = DrawStatus.OPEN
            opened += 1
            audit.record(db, actor, "bola.draw.open", entity_type="bola_draw", entity_id=draw.id,
                         after={"by": "tick"})
    db.flush()
    return {"opened": opened, "locked": locked}


def list_draws(db: Session, *, date: dt.date | None = None, location_id: int | None = None,
               status: DrawStatus | None = None, upcoming: bool = False,
               include_legacy: bool = True, limit: int = 200, offset: int = 0) -> list[BolaDraw]:
    stmt = select(BolaDraw)
    if date:
        stmt = stmt.where(BolaDraw.draw_date == date)
    if location_id:
        stmt = stmt.where(BolaDraw.location_id == location_id)
    if status:
        stmt = stmt.where(BolaDraw.status == status)
    if upcoming:
        stmt = stmt.where(BolaDraw.status.in_([DrawStatus.SCHEDULED, DrawStatus.OPEN, DrawStatus.LOCKED]))
    if not include_legacy:
        stmt = stmt.where(BolaDraw.legacy.is_(False))
    order = (BolaDraw.scheduled_at.desc(), BolaDraw.id.desc()) if not upcoming else \
            (BolaDraw.scheduled_at.asc(), BolaDraw.id.asc())
    return list(db.scalars(stmt.order_by(*order).limit(clamp_limit(limit, 500))
                           .offset(clamp_offset(offset))).all())


def results(db: Session, *, location_id: int | None = None, limit: int = 60,
            offset: int = 0) -> list[BolaDraw]:
    stmt = select(BolaDraw).where(BolaDraw.status == DrawStatus.SETTLED)
    if location_id:
        stmt = stmt.where(BolaDraw.location_id == location_id)
    return list(db.scalars(stmt.order_by(BolaDraw.declared_at.desc().nulls_last(), BolaDraw.id.desc())
                           .limit(clamp_limit(limit, 300)).offset(clamp_offset(offset))).all())


def next_draw(db: Session, *, location_id: int | None = None) -> BolaDraw | None:
    stmt = select(BolaDraw).where(BolaDraw.status.in_([DrawStatus.SCHEDULED, DrawStatus.OPEN]))
    if location_id:
        stmt = stmt.where(BolaDraw.location_id == location_id)
    return db.scalar(stmt.order_by(BolaDraw.scheduled_at.asc()).limit(1))


def bettable(draw: BolaDraw, now: dt.datetime | None = None) -> bool:
    now = now or utcnow()
    return draw.status == DrawStatus.OPEN and now < draw.cutoff_at


# ---------------------------------------------------------------------------
# Exposure: the running total per ordered pair, and the limits that use it
# ---------------------------------------------------------------------------

def exposure_rows(db: Session, draw_id: int) -> list[BolaExposure]:
    return list(db.scalars(select(BolaExposure).where(BolaExposure.draw_id == draw_id)).all())


def exposure_map(db: Session, draw_id: int) -> dict[Pair, tuple[int, int]]:
    return {(row.first, row.second): (row.tumbok_cents, row.sahod_cents)
            for row in exposure_rows(db, draw_id)}


def _bump_exposure(db: Session, draw_id: int, deltas: dict[Pair, tuple[int, int]], sign: int) -> None:
    """Add (sign=+1) or subtract (sign=-1) a ticket's lines. Called inside the bet/cancel txn."""
    for (first, second), (tumbok, sahod) in sorted(deltas.items()):
        row = db.get(BolaExposure, {"draw_id": draw_id, "first": first, "second": second})
        if row is None:
            if sign < 0:
                raise Conflict("Exposure row missing for a ticket being reversed.")
            row = BolaExposure(draw_id=draw_id, first=first, second=second,
                               tumbok_cents=0, sahod_cents=0, line_count=0)
            db.add(row)
        row.tumbok_cents += sign * tumbok
        row.sahod_cents += sign * sahod
        row.line_count += sign
        if row.tumbok_cents < 0 or row.sahod_cents < 0 or row.line_count < 0:
            raise Conflict("Exposure would go negative; the ticket does not match this draw.")
    db.flush()


def _deltas(lines: list[Pair], tumbok_cents: int, sahod_cents: int) -> dict[Pair, tuple[int, int]]:
    out: dict[Pair, tuple[int, int]] = {}
    for pair in lines:
        tumbok, sahod = out.get(pair, (0, 0))
        out[pair] = (tumbok + tumbok_cents, sahod + sahod_cents)
    return out


def outcome_liability(view: dict[Pair, tuple[int, int]], first: int, second: int, rate: int) -> int:
    """What outcome (first, second) would owe: M x (tumbok on (x,y) + sahod on (y,x)).

    Both halves are the same draw result, which is exactly what the legacy checker ignored.
    """
    tumbok = view.get((first, second), (0, 0))[0]
    sahod = view.get((second, first), (0, 0))[1]
    return payout_of(tumbok + sahod, rate)


def worst_outcome(db: Session, draw_id: int, settings: SettingsStore) -> dict:
    return worst_of(exposure_map(db, draw_id), multiplier(settings))


def worst_of(view: dict[Pair, tuple[int, int]], rate: int) -> dict:
    best = {"first": None, "second": None, "liability_cents": 0, "staked_cents": 0}
    for (first, second), (tumbok, sahod) in view.items():
        for x, y in (((first, second), (second, first)) if tumbok or sahod else ()):
            liability = outcome_liability(view, x, y, rate)
            if liability > best["liability_cents"]:
                staked = view.get((x, y), (0, 0))[0] + view.get((y, x), (0, 0))[1]
                best = {"first": x, "second": y, "liability_cents": liability, "staked_cents": staked}
    return best


def check_limits(db: Session, draw: BolaDraw, lines: list[Pair], tumbok_cents: int,
                 sahod_cents: int, settings: SettingsStore) -> None:
    """Two independent checks, both under the draw lock, both against BolaExposure.

    A) legacy per-combination limit: the sum of every stake on the same ordered pair.
    B) liability cap: what one drawn outcome could owe, per outcome and for the whole draw.

    A cap is the maximum allowed: reaching it exactly is fine, going past it is refused.
    """
    line_limit, outcome_cap, draw_cap = effective_caps(settings, draw.location)
    if not (line_limit or outcome_cap or draw_cap):
        return
    current = exposure_map(db, draw.id)
    deltas = _deltas(lines, tumbok_cents, sahod_cents)

    if line_limit:
        for (first, second), (add_t, add_s) in sorted(deltas.items()):
            held_t, held_s = current.get((first, second), (0, 0))
            total = held_t + held_s + add_t + add_s
            if total > line_limit:
                raise LockedState(
                    f"Puno na ang taya sa kombinasyong {first}–{second} para sa draw na ito "
                    f"({fmt(total)} > {fmt(line_limit)} limit).")

    if outcome_cap or draw_cap:
        rate = multiplier(settings)
        after = dict(current)
        for pair, (add_t, add_s) in deltas.items():
            held_t, held_s = after.get(pair, (0, 0))
            after[pair] = (held_t + add_t, held_s + add_s)
        touched: set[Pair] = set()
        for first, second in deltas:
            touched.add((first, second))
            touched.add((second, first))
        if outcome_cap:
            for x, y in sorted(touched):
                liability = outcome_liability(after, x, y, rate)
                if liability > outcome_cap:
                    raise LockedState(
                        f"Alpas sa liability cap ang resultang {x}–{y}: {fmt(liability)} "
                        f"(cap {fmt(outcome_cap)}). Bawasan ang taya.")
        if draw_cap:
            worst = worst_of(after, rate)
            if worst["liability_cents"] > draw_cap:
                raise LockedState(
                    f"Ang pinakamalaking puwedeng bayaran sa draw na ito ay "
                    f"{fmt(worst['liability_cents'])} ({worst['first']}–{worst['second']}), "
                    f"lampas sa {fmt(draw_cap)} cap.")


def exposure_summary(db: Session, draw: BolaDraw, settings: SettingsStore) -> dict:
    """The Master/Admin risk widget: worst outcome, its liability, stakes and headroom."""
    view = exposure_map(db, draw.id)
    rate = multiplier(settings)
    worst = worst_of(view, rate)
    staked = sum(t + s for t, s in view.values())
    lines = sum(row.line_count for row in exposure_rows(db, draw.id))
    _, outcome_cap, draw_cap = effective_caps(settings, draw.location)
    caps = [c for c in (outcome_cap, draw_cap) if c]
    ceiling = min(caps) if caps else 0
    return {
        "staked_cents": staked, "line_count": lines, "pairs": len(view),
        "worst": worst, "multiplier_x100": rate,
        "outcome_cap_cents": outcome_cap, "draw_cap_cents": draw_cap,
        "headroom_cents": max(0, ceiling - worst["liability_cents"]) if ceiling else None,
        "outcomes": ordered_outcomes(settings),
    }


def heatmap(db: Session, draw: BolaDraw, settings: SettingsStore) -> dict:
    """40x40 grid: stake per ordered pair and the liability of each outcome. The Master's
    main risk screen (replaces the old "combination list")."""
    view = exposure_map(db, draw.id)
    rate = multiplier(settings)
    size = max_number(settings)
    top = max((t + s for t, s in view.values()), default=0)
    cells = []
    for first in range(1, size + 1):
        row = []
        for second in range(1, size + 1):
            tumbok, sahod = view.get((first, second), (0, 0))
            row.append({"first": first, "second": second, "tumbok": tumbok, "sahod": sahod,
                        "stake": tumbok + sahod,
                        "liability": outcome_liability(view, first, second, rate) if first != second else 0,
                        "heat": round((tumbok + sahod) / top * 100) if top else 0})
        cells.append(row)
    return {"size": size, "cells": cells, "top_stake": top,
            "worst": worst_of(view, rate), "multiplier_x100": rate}


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def _ticket_code(db: Session) -> str:
    for _ in range(20):
        code = random_code(10)
        if not db.scalar(select(BolaTicket.id).where(BolaTicket.code == code)):
            return code
    raise Conflict("Could not allocate a ticket code, please retry.")


def network_names(db: Session, agent_id: int | None) -> tuple[str | None, str | None]:
    """(sub agent, agent) usernames from the real upline chain, snapshotted on the ticket (legacy
    ``kubrador`` / ``cavo`` columns now hold the Sub Agent / Agent)."""
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


def _replay(existing: BolaTicket, *, draw_id, kind, numbers, tumbok_cents, sahod_cents,
            user_id) -> BolaTicket:
    """An idempotency-key hit is only a "double tap" when it is the same bet. A reused key on a
    different bet must fail loudly: silently returning the old ticket would tell the agent the
    new bet was placed when it was not (and nothing was charged for it)."""
    try:
        same = (existing.draw_id == int(draw_id)
                and existing.user_id == user_id
                and existing.kind == BolaKind(kind)
                and list(existing.numbers) == [int(n) for n in numbers]
                and existing.tumbok_cents == int(tumbok_cents)
                and existing.sahod_cents == int(sahod_cents))
    except (TypeError, ValueError):
        same = False
    if not same:
        raise Conflict("That idempotency key was already used for a different bet. "
                       "Reload the slip and try again.")
    return existing


def place_bola_bet(
    db: Session, actor: User, *, draw_id: int, kind: BolaKind | str, numbers,
    tumbok_cents: int, sahod_cents: int, settings: SettingsStore,
    idempotency_key: str | None = None, note: str | None = None, ip: str | None = None,
) -> BolaTicket:
    """One atomic unit: validate -> limits -> ledger -> ticket + lines + exposure, or nothing.

    The logged-in account always bets for itself, from its own credit (same as the fight game's live bet panel).
    Unlike the legacy ``gbet`` there is never a half-created ticket to delete afterwards.
    """
    if idempotency_key:
        existing = db.scalar(select(BolaTicket).where(BolaTicket.idempotency_key == idempotency_key))
        if existing is not None:
            return _replay(existing, draw_id=draw_id, kind=kind, numbers=numbers,
                           tumbok_cents=tumbok_cents, sahod_cents=sahod_cents, user_id=actor.id)

    assert_enabled(settings)
    draw = _lock_draw(db, draw_id)
    if idempotency_key:
        # A duplicate that was waiting on the draw lock: the first request has committed by now,
        # so hand back its ticket instead of tripping the unique key (a double tap is not an error).
        existing = db.scalar(select(BolaTicket).where(BolaTicket.idempotency_key == idempotency_key))
        if existing is not None:
            return _replay(existing, draw_id=draw_id, kind=kind, numbers=numbers,
                           tumbok_cents=tumbok_cents, sahod_cents=sahod_cents, user_id=actor.id)
    now = utcnow()
    if draw.status != DrawStatus.OPEN:
        raise LockedState(f"Betting for this draw is {draw.status}.")
    if now >= draw.cutoff_at:
        raise LockedState("Tapos na ang tayaan: the cut-off for this draw has passed.")

    kind = BolaKind(kind)
    nums = validate_numbers(numbers, kind, settings)
    tumbok, sahod = validate_stakes(tumbok_cents, sahod_cents, settings)
    lines = expand_lines(kind, nums)
    stake = (tumbok + sahod) * len(lines)
    ceiling = int(settings.get("payout.max_stake_cents"))
    if stake > ceiling:
        raise ValidationError(f"One ticket may not be more than {fmt(ceiling)} "
                              f"(this one is {fmt(stake)}).")

    if not actor.has_wallet:
        raise Forbidden(f"A {actor.role_label} account has no balance and cannot place bets.")

    check_limits(db, draw, lines, tumbok, sahod, settings)

    players.assert_account_can_bet(db, actor, stake, settings)
    source = accounts.wallet_for_user(db, actor)
    if source.balance_cents < stake:
        raise InsufficientFunds(f"Not enough credit: this ticket costs {fmt(stake)} and you have "
                                f"{fmt(source.balance_cents)}.")
    pool = accounts.pool_for_draw(db, draw)
    code = _ticket_code(db)
    txn = ledger.post(
        db, kind="bola.stake",
        entries=[(source, +stake), (pool, -stake)],
        actor=actor, memo=f"Bola {code} · {draw.draw_date} slot {draw.slot} · {kind.value}",
        ref_type="bola_draw", ref_id=draw.id,
        idempotency_key=f"bola-bet:{idempotency_key}" if idempotency_key else None,
    )

    chain_kubrador, chain_cavo = network_names(db, actor.id)
    t_pay, s_pay = line_payouts(tumbok, sahod, settings)
    best = max(t_pay, s_pay)
    payout_cap = int(settings.get("payout.max_payout_cents")) or None
    ticket = BolaTicket(
        code=code, draw_id=draw.id, user_id=actor.id, agent_id=actor.id, kind=kind,
        numbers=nums, tumbok_cents=tumbok, sahod_cents=sahod, line_count=len(lines),
        stake_cents=stake, status=TicketStatus.ACTIVE,
        est_max_payout_cents=min(best, payout_cap) if payout_cap else best,
        stake_txn_id=txn.id, idempotency_key=idempotency_key, placed_ip=ip, placed_at=now,
        kubrador=chain_kubrador, cavo=chain_cavo, note=(note or "").strip()[:120] or None,
    )
    db.add(ticket)
    db.flush()
    for first, second in lines:
        db.add(BolaLine(ticket_id=ticket.id, draw_id=draw.id, first=first, second=second,
                        tumbok_cents=tumbok, sahod_cents=sahod))
    db.flush()
    _bump_exposure(db, draw.id, _deltas(lines, tumbok, sahod), +1)

    audit.record(db, actor, "bola.bet.place", entity_type="bola_ticket", entity_id=ticket.id,
                 after={"code": code, "draw_id": draw.id, "kind": str(kind), "numbers": nums,
                        "tumbok_cents": tumbok, "sahod_cents": sahod, "stake_cents": stake,
                        "lines": len(lines)})
    return ticket


def get_ticket(db: Session, ticket_id: int) -> BolaTicket:
    ticket = db.get(BolaTicket, ticket_id)
    if ticket is None:
        raise NotFound("Ticket not found.")
    return ticket


def get_ticket_by_code(db: Session, code: str) -> BolaTicket:
    ticket = db.scalar(select(BolaTicket).where(BolaTicket.code == (code or "").strip().upper()))
    if ticket is None:
        raise NotFound("No ticket with that code.")
    return ticket


def assert_ticket_visible(db: Session, actor: User, ticket: BolaTicket) -> BolaTicket:
    preview_pid = getattr(actor, "preview_player_id", None)
    if preview_pid is not None:
        if ticket.player_id != preview_pid:
            raise Forbidden("That ticket is not yours.")
        return ticket
    if has_global_scope(actor):
        return ticket
    if ticket.agent_id is None or ticket.agent_id not in scope_ids(db, actor):
        raise Forbidden("That ticket is outside your network.")
    return ticket


def ticket_lines(db: Session, ticket: BolaTicket) -> list[BolaLine]:
    return list(db.scalars(select(BolaLine).where(BolaLine.ticket_id == ticket.id)
                           .order_by(BolaLine.id)).all())


def cancel_bola_ticket(db: Session, actor: User, *, ticket_id: int, reason: str,
                       settings: SettingsStore) -> BolaTicket:
    """Cancel = a ledger reversal, never a delete. Frees the exposure headroom too.

    The legacy panel had no working cancel path for this game at all.
    """
    # Undoing a placed bet is dev-only (the routes check this too; this keeps a future route from forgetting it).
    if Perm.BOLA_CANCEL not in permissions_of(actor):
        raise Forbidden("Only the developer account can cancel a placed bet.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationError("A cancellation reason is required (min 3 characters).")
    # Lock order is draw -> ticket -> accounts, the same as place_bola_bet and settlement.
    # Taking the ticket first let a cancel and a declare wait on each other (deadlock on PostgreSQL).
    peek = db.get(BolaTicket, ticket_id)
    if peek is None:
        raise NotFound("Ticket not found.")
    assert_ticket_visible(db, actor, peek)           # before any lock: outsiders must not stall a draw
    draw = _lock_draw(db, peek.draw_id)
    ticket = db.scalar(select(BolaTicket).where(BolaTicket.id == ticket_id).with_for_update()
                       .execution_options(populate_existing=True))
    if ticket is None:
        raise NotFound("Ticket not found.")
    if ticket.status != TicketStatus.ACTIVE:
        raise LockedState(f"Ticket {ticket.code} is already {ticket.status}.")
    if draw.status != DrawStatus.OPEN:
        raise LockedState("Betting for this draw is closed; tickets can no longer be cancelled.")
    now = utcnow()
    if now >= draw.cutoff_at:
        raise LockedState("The cut-off has passed; this ticket can no longer be cancelled.")
    if not is_top(actor):
        window = int(settings.get("bola.cancel_window_seconds"))
        if window and (now - ticket.placed_at).total_seconds() > window:
            raise LockedState(f"The {window}-second cancellation window has passed. Ask a Master.")

    if ticket.stake_txn_id:
        txn = db.get(ledger.LedgerTxn, ticket.stake_txn_id)
        if txn is not None and txn.reversed_by_txn_id is None:
            ledger.reverse(db, txn, actor=actor, reason=reason,
                           idempotency_key=f"bola-cancel:{ticket.id}",
                           memo=f"Cancel bola ticket {ticket.code}")
    deltas = _deltas([(line.first, line.second) for line in ticket_lines(db, ticket)],
                     ticket.tumbok_cents, ticket.sahod_cents)
    _bump_exposure(db, draw.id, deltas, -1)
    ticket.status = TicketStatus.CANCELED
    ticket.canceled_at = now
    ticket.canceled_by_id = actor.id
    ticket.cancel_reason = reason[:255]
    db.flush()
    audit.record(db, actor, "bola.bet.cancel", entity_type="bola_ticket", entity_id=ticket.id,
                 before={"status": "active"},
                 after={"code": ticket.code, "stake_cents": ticket.stake_cents}, reason=reason)
    return ticket


def list_tickets(db: Session, actor: User, *, draw_id: int | None = None,
                 status: TicketStatus | None = None, agent_id: int | None = None,
                 player_id: int | None = None, search: str | None = None,
                 canceled_only: bool = False, include_legacy: bool = True,
                 limit: int = 200, offset: int = 0, before_id: int | None = None,
                 with_player: bool = False, since: dt.datetime | None = None,
                 until: dt.datetime | None = None) -> list[BolaTicket]:
    """Newest first. ``before_id`` is keyset paging (rows with a smaller id): unlike a deep OFFSET
    its cost does not grow with how far you have read, which is what exports use.
    ``with_player`` loads the players in one extra query instead of one per ticket."""
    stmt = select(BolaTicket)
    if before_id is not None:
        stmt = stmt.where(BolaTicket.id < before_id)
    if with_player:
        stmt = stmt.options(selectinload(BolaTicket.player))
    preview_pid = getattr(actor, "preview_player_id", None)
    if preview_pid is not None:                 # DEV preview of a player: their own tickets only
        stmt = stmt.where(BolaTicket.player_id == preview_pid)
    elif not has_global_scope(actor):
        stmt = stmt.where(BolaTicket.agent_id.in_(scope_ids(db, actor)))
    if draw_id:
        stmt = stmt.where(BolaTicket.draw_id == draw_id)
    if status:
        stmt = stmt.where(BolaTicket.status == status)
    if canceled_only:
        stmt = stmt.where(BolaTicket.status == TicketStatus.CANCELED)
    if since is not None:
        stmt = stmt.where(BolaTicket.placed_at >= since)
    if until is not None:
        stmt = stmt.where(BolaTicket.placed_at < until)
    if agent_id:
        stmt = stmt.where(BolaTicket.agent_id == agent_id)
    if player_id:
        stmt = stmt.where(BolaTicket.player_id == player_id)
    if search:
        term = f"%{search.strip()[:40]}%"
        stmt = stmt.where(or_(BolaTicket.code.ilike(term), BolaTicket.kubrador.ilike(term),
                              BolaTicket.cavo.ilike(term), BolaTicket.note.ilike(term)))
    if not include_legacy:
        stmt = stmt.where(BolaTicket.legacy.is_(False))
    return list(db.scalars(stmt.order_by(BolaTicket.id.desc())
                           .limit(clamp_limit(limit, 1000)).offset(clamp_offset(offset))).all())


def active_tickets(db: Session, draw: BolaDraw) -> list[BolaTicket]:
    return list(db.scalars(select(BolaTicket).where(
        BolaTicket.draw_id == draw.id, BolaTicket.status == TicketStatus.ACTIVE
    ).order_by(BolaTicket.id)).all())


def staked_on(db: Session, draw_id: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(BolaTicket.stake_cents), 0)).where(
        BolaTicket.draw_id == draw_id, BolaTicket.status == TicketStatus.ACTIVE)) or 0)


# ---------------------------------------------------------------------------
# Per-draw expenses (old tbl_expences)
# ---------------------------------------------------------------------------

def add_expense(db: Session, actor: User, draw_id: int, *, label: str, amount_cents: int,
                note: str | None = None) -> BolaExpense:
    draw = get_draw(db, draw_id)
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise ValidationError("Expense label is required (max 80 characters).")
    if amount_cents <= 0:
        raise ValidationError("Expense amount must be greater than zero.")
    row = BolaExpense(draw_id=draw.id, label=label, amount_cents=int(amount_cents),
                      note=(note or "").strip()[:255] or None, created_by_id=actor.id)
    db.add(row)
    db.flush()
    audit.record(db, actor, "bola.expense.add", entity_type="bola_draw", entity_id=draw.id,
                 after={"label": label, "amount_cents": int(amount_cents)})
    return row


def list_expenses(db: Session, draw_id: int) -> list[BolaExpense]:
    return list(db.scalars(select(BolaExpense).where(BolaExpense.draw_id == draw_id)
                           .order_by(BolaExpense.id)).all())


def delete_expense(db: Session, actor: User, expense_id: int) -> BolaExpense:
    row = db.get(BolaExpense, expense_id)
    if row is None:
        raise NotFound("Expense not found.")
    audit.record(db, actor, "bola.expense.delete", entity_type="bola_draw", entity_id=row.draw_id,
                 before={"label": row.label, "amount_cents": row.amount_cents})
    db.delete(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# Lobby helpers
# ---------------------------------------------------------------------------

@dataclass
class AreaBoard:
    location: Location
    draws: list[BolaDraw]
    last: BolaDraw | None
    upcoming: BolaDraw | None


def lobby(db: Session, *, day: dt.date | None = None) -> dict:
    """Today's draws grouped by area, plus each area's last result and next draw."""
    day = day or utcnow().astimezone(MANILA).date()
    draws = list_draws(db, date=day, limit=500)
    areas: dict[int, AreaBoard] = {}
    for draw in draws:
        board = areas.get(draw.location_id)
        if board is None:
            board = areas[draw.location_id] = AreaBoard(draw.location, [], None, None)
        board.draws.append(draw)
    settled = results(db, limit=200)
    for draw in settled:
        board = areas.get(draw.location_id)
        if board is None:
            board = areas[draw.location_id] = AreaBoard(draw.location, [], None, None)
        if board.last is None:
            board.last = draw
    for board in areas.values():
        board.draws.sort(key=lambda d: (d.scheduled_at, d.slot))
        board.upcoming = next((d for d in board.draws if d.status in
                               (DrawStatus.SCHEDULED, DrawStatus.OPEN, DrawStatus.LOCKED)), None)
    return {
        "day": day,
        "areas": sorted(areas.values(), key=lambda b: b.location.name),
        "open": sum(1 for d in draws if bettable(d)),
        "total": len(draws),
        "settled": sum(1 for d in draws if d.status == DrawStatus.SETTLED),
        "next": next_draw(db),
    }
