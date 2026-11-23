"""The GCash pool: many numbers, one limit tracker, automatic rotation.

Real GCash wallets have daily and monthly limits. Instead of one number per teller, tellers register
as many as they like and the cash desk routes every GCash request to the number that still has room:

* **Tracker.** Usage is never stored, it is summed from the requests routed to a number (pending
  ones count as a reservation, rejected ones do not) plus manual adjustments, per Manila day and
  Manila month. It cannot drift from the ledger.
* **Rotation.** ``pick`` skips a number that the next transaction would push past the limit, and
  also steps away from it *before* it gets there: once the transaction would take it beyond
  ``gcash.rotate_at_pct`` of its day or month limit, any number with more room is preferred.
* **Strategy.** ``fill`` (default) uses numbers in list order and moves on when one is near its
  limit. ``balance`` always uses the number with the most room left today.
* **No pool, no change.** While no number is registered, requests behave exactly as before
  (nothing is assigned or enforced), so an existing install keeps working until tellers add numbers.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.config import get_settings
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.money import fmt
from app.core.security import mask, pii
from app.core.settings_registry import SettingsStore
from app.db.models import (
    CashRequest, GcashAccount, GcashAdjustment, Notification, RequestDirection, RequestStatus,
    Role, Status, User,
)
from app.services.bola import MANILA

GCASH_RE = re.compile(r"^09\d{9}$")
STRATEGIES = ("fill", "balance")


# --------------------------------------------------------------------------- numbers

def normalize_number(raw: str | None) -> str:
    """Accept 0917 123 4567, 0917-123-4567 and +63 917 123 4567; store 09171234567."""
    digits = re.sub(r"[\s\-().]", "", (raw or "").strip())
    if digits.startswith("+63"):
        digits = "0" + digits[3:]
    elif digits.startswith("63") and len(digits) == 12:
        digits = "0" + digits[2:]
    if not GCASH_RE.match(digits):
        raise ValidationError("GCash number must be 11 digits starting with 09.")
    return digits


def _hash(number: str) -> str:
    key = get_settings().SECRET_KEY.encode()
    return hmac.new(key, number.encode(), hashlib.sha256).hexdigest()


def number_of(account: GcashAccount) -> str:
    return pii.decrypt(account.number_enc) or ""


def masked(account: GcashAccount) -> str:
    return mask(number_of(account))


# --------------------------------------------------------------------------- time windows

def day_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    local = now.astimezone(MANILA)
    start = dt.datetime.combine(local.date(), dt.time.min, tzinfo=MANILA)
    return start.astimezone(dt.timezone.utc), (start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)


def month_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    local = now.astimezone(MANILA)
    start = dt.datetime(local.year, local.month, 1, tzinfo=MANILA)
    nxt = dt.datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=MANILA)
    return start.astimezone(dt.timezone.utc), nxt.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- tracker

@dataclass(frozen=True)
class Usage:
    account: GcashAccount
    day_used: int
    month_used: int
    day_limit: int
    month_limit: int
    rotate_pct: int

    @property
    def day_left(self) -> int:
        return max(self.day_limit - self.day_used, 0)

    @property
    def month_left(self) -> int:
        return max(self.month_limit - self.month_used, 0)

    @property
    def room(self) -> int:
        """The most this number can still take right now: the tighter of day and month."""
        return min(self.day_left, self.month_left)

    @property
    def day_pct(self) -> int:
        return min(100, round(self.day_used * 100 / self.day_limit)) if self.day_limit else 100

    @property
    def month_pct(self) -> int:
        return min(100, round(self.month_used * 100 / self.month_limit)) if self.month_limit else 100

    def fits(self, amount_cents: int) -> bool:
        return amount_cents <= self.room

    def comfortable(self, amount_cents: int) -> bool:
        """After this transaction the number would still be under the rotate threshold."""
        cap = self.rotate_pct / 100
        return ((self.day_used + amount_cents) <= self.day_limit * cap
                and (self.month_used + amount_cents) <= self.month_limit * cap)

    @property
    def state(self) -> str:
        if not self.account.is_active:
            return "paused"
        if self.room <= 0:
            return "full"
        if max(self.day_pct, self.month_pct) >= self.rotate_pct:
            return "near"
        return "ok"


def limits_for(account: GcashAccount, settings: SettingsStore) -> tuple[int, int]:
    day = account.daily_limit_cents or int(settings.get("gcash.daily_limit_cents"))
    month = account.monthly_limit_cents or int(settings.get("gcash.monthly_limit_cents"))
    return int(day), int(month)


def _counted_requests(settings: SettingsStore, now: dt.datetime):
    """The WHERE clause for requests that count against a number: approved ones, and pending ones
    still inside the hold window (an abandoned request must not eat the limit for ever)."""
    clauses = [CashRequest.status == RequestStatus.APPROVED]
    hold = int(settings.get("gcash.hold_minutes"))
    pending = CashRequest.status == RequestStatus.PENDING
    if hold > 0:
        pending = and_(pending, CashRequest.created_at >= now - dt.timedelta(minutes=hold))
    clauses.append(pending)
    cond = or_(*clauses)
    if not settings.get("gcash.count_cashout"):
        cond = and_(cond, CashRequest.direction == RequestDirection.IN)
    return cond


def _sum_by_account(db: Session, settings: SettingsStore, now: dt.datetime,
                    start: dt.datetime, end: dt.datetime) -> dict[int, int]:
    out: dict[int, int] = {}
    rows = db.execute(
        select(CashRequest.gcash_account_id, func.coalesce(func.sum(CashRequest.amount_cents), 0))
        .where(CashRequest.gcash_account_id.is_not(None), CashRequest.created_at >= start,
               CashRequest.created_at < end, _counted_requests(settings, now))
        .group_by(CashRequest.gcash_account_id)).all()
    for account_id, total in rows:
        out[account_id] = int(total)
    adj = db.execute(
        select(GcashAdjustment.account_id, func.coalesce(func.sum(GcashAdjustment.amount_cents), 0))
        .where(GcashAdjustment.created_at >= start, GcashAdjustment.created_at < end)
        .group_by(GcashAdjustment.account_id)).all()
    for account_id, total in adj:
        out[account_id] = out.get(account_id, 0) + int(total)
    return out


def usage_for(db: Session, accounts: list[GcashAccount], settings: SettingsStore,
              now: dt.datetime | None = None) -> list[Usage]:
    now = now or dt.datetime.now(dt.timezone.utc)
    day = _sum_by_account(db, settings, now, *day_window(now))
    month = _sum_by_account(db, settings, now, *month_window(now))
    pct = int(settings.get("gcash.rotate_at_pct"))
    result = []
    for a in accounts:
        d_lim, m_lim = limits_for(a, settings)
        result.append(Usage(a, day.get(a.id, 0), month.get(a.id, 0), d_lim, m_lim, pct))
    return result


# --------------------------------------------------------------------------- queries

def pool_exists(db: Session) -> bool:
    return db.scalar(select(GcashAccount.id).limit(1)) is not None


def list_accounts(db: Session, *, owner_id: int | None = None,
                  include_inactive: bool = True) -> list[GcashAccount]:
    stmt = select(GcashAccount)
    if owner_id is not None:
        stmt = stmt.where(GcashAccount.owner_id == owner_id)
    if not include_inactive:
        stmt = stmt.where(GcashAccount.is_active.is_(True))
    return list(db.scalars(stmt.order_by(GcashAccount.sort_order, GcashAccount.id)).all())


def has_active_number(db: Session, user: User) -> bool:
    """A teller may only review requests while at least one of its numbers is live."""
    if db.scalar(select(GcashAccount.id).where(GcashAccount.owner_id == user.id,
                                               GcashAccount.is_active.is_(True)).limit(1)) is not None:
        return True
    return bool(user.gcash_enc) and not db.scalar(
        select(GcashAccount.id).where(GcashAccount.owner_id == user.id).limit(1))


# --------------------------------------------------------------------------- smart pick

def _eligible(db: Session, *, lock: bool) -> list[GcashAccount]:
    stmt = (select(GcashAccount).outerjoin(User, GcashAccount.owner_id == User.id)
            .where(GcashAccount.is_active.is_(True),
                   or_(GcashAccount.owner_id.is_(None), User.status == Status.ACTIVE))
            .order_by(GcashAccount.sort_order, GcashAccount.id))
    if lock:
        stmt = stmt.with_for_update(of=GcashAccount)   # serialise concurrent picks on PostgreSQL
    return list(db.scalars(stmt).all())


def pick(db: Session, settings: SettingsStore, amount_cents: int, *,
         now: dt.datetime | None = None, lock: bool = False) -> Usage | None:
    """The number that should take `amount_cents` right now, or None when none can.

    1. Drop numbers that are paused, owned by a closed teller, or would go over a limit.
    2. Prefer numbers that stay under the rotate threshold after this transaction.
    3. Among those: ``fill`` = first in list order, ``balance`` = most room left today.
    4. If every number that fits would end up past the threshold, still use one (the one with the
       most room) rather than refuse the player.
    """
    pool = _eligible(db, lock=lock)
    if not pool:
        return None
    usages = [u for u in usage_for(db, pool, settings, now) if u.fits(amount_cents)]
    if not usages:
        return None
    comfortable = [u for u in usages if u.comfortable(amount_cents)]
    if comfortable:
        if settings.get("gcash.strategy") == "balance":
            return max(comfortable, key=lambda u: (u.day_left, -u.account.sort_order))
        return comfortable[0]
    return max(usages, key=lambda u: u.room)


def peek(db: Session, settings: SettingsStore, amount_cents: int | None = None) -> Usage | None:
    """What a request made right now would be routed to (display only; nothing is reserved)."""
    return pick(db, settings, amount_cents or int(settings.get("wallet.min_cash_cents")))


# --------------------------------------------------------------------------- routing a request

def route_request(db: Session, request: CashRequest, settings: SettingsStore) -> GcashAccount | None:
    """Called by the cash desk when a GCash request is created. No pool = nothing to do."""
    if request.method != "gcash" or not pool_exists(db):
        return None
    chosen = pick(db, settings, request.amount_cents, lock=True)
    if chosen is None:
        raise ValidationError(
            f"No GCash number can take {fmt(request.amount_cents)} right now: every number is at or "
            "near its daily / monthly limit. Try a smaller amount, use bank transfer, or try again "
            "after midnight (Manila time).")
    request.gcash_account_id = chosen.account.id
    db.flush()
    after = usage_for(db, [chosen.account], settings)[0]
    if chosen.state != "near" and after.state in ("near", "full"):
        _warn_owner(db, after, settings)
    return chosen.account


def _warn_owner(db: Session, usage: Usage, settings: SettingsStore) -> None:
    """One heads-up when a number crosses the rotate threshold (the next request moves on)."""
    a = usage.account
    msg = (f"GCash {a.label} ({masked(a)}) is at {max(usage.day_pct, usage.month_pct)}% of its limit. "
           "New requests now go to the next number.")
    targets = set(db.scalars(select(User.id).where(User.role.in_([Role.ADMIN, Role.BISHOP]))).all())
    if a.owner_id:
        targets.add(a.owner_id)
    for user_id in targets:
        db.add(Notification(user_id=user_id, level="warning", message=msg[:255], link="/staff/teller"))
    db.flush()


def visible_to(stmt, actor: User):
    """Narrow a CashRequest query for a Teller: requests routed to its own numbers, and requests
    nobody was routed to (bank, or made before the pool existed). Everyone else sees everything
    their role already allowed."""
    if actor.role != Role.TELLER:
        return stmt
    mine = select(GcashAccount.id).where(GcashAccount.owner_id == actor.id)
    return stmt.where(or_(CashRequest.gcash_account_id.is_(None), CashRequest.gcash_account_id.in_(mine)))


def assert_can_review(db: Session, actor: User, request: CashRequest) -> None:
    if actor.role != Role.TELLER:
        return
    if request.gcash_account_id is not None:
        owner = db.scalar(select(GcashAccount.owner_id).where(GcashAccount.id == request.gcash_account_id))
        if owner is not None and owner != actor.id:
            raise Forbidden("That request was routed to another teller's GCash number.")


# --------------------------------------------------------------------------- managing numbers

def _can_manage(actor: User, account: GcashAccount | None = None) -> None:
    if actor.is_admin_tier:
        return
    if actor.role == Role.TELLER and (account is None or account.owner_id == actor.id):
        return
    raise Forbidden("You can only manage your own GCash numbers.")


def _clean_label(label: str | None) -> str:
    label = (label or "").strip()
    if not label or len(label) > 60:
        raise ValidationError("Give the number a short name (up to 60 characters).")
    return label


def _clean_limit(raw: str | int | None, what: str) -> int | None:
    """Blank = follow the global limit. Otherwise whole centavos (callers pass cents)."""
    if raw in (None, ""):
        return None
    value = int(raw)
    if value < 100_00 or value > 10**11:
        raise ValidationError(f"{what} must be between ₱100 and ₱1,000,000,000, or blank to follow the default.")
    return value


def add_account(db: Session, actor: User, *, number: str, label: str, owner_id: int | None = None,
                daily_limit_cents: int | None = None, monthly_limit_cents: int | None = None,
                note: str | None = None) -> GcashAccount:
    _can_manage(actor)
    if actor.role == Role.TELLER:
        owner_id = actor.id
    elif owner_id is not None:
        owner = db.get(User, owner_id)
        if owner is None or owner.role != Role.TELLER:
            raise ValidationError("A GCash number can only belong to a Teller.")
    clean = normalize_number(number)
    digest = _hash(clean)
    if db.scalar(select(GcashAccount.id).where(GcashAccount.number_hash == digest)):
        raise Conflict("That GCash number is already in the pool.")
    last = db.scalar(select(func.max(GcashAccount.sort_order))) or 0
    account = GcashAccount(owner_id=owner_id, label=_clean_label(label), number_enc=pii.encrypt(clean),
                           number_hash=digest, is_active=True, sort_order=int(last) + 1,
                           daily_limit_cents=_clean_limit(daily_limit_cents, "Daily limit"),
                           monthly_limit_cents=_clean_limit(monthly_limit_cents, "Monthly limit"),
                           note=(note or "").strip()[:255] or None)
    db.add(account)
    db.flush()
    audit.record(db, actor, "gcash.account.add", entity_type="gcash_account", entity_id=account.id,
                 after={"label": account.label, "gcash": clean, "owner_id": owner_id,
                        "daily_limit_cents": account.daily_limit_cents,
                        "monthly_limit_cents": account.monthly_limit_cents})
    return account


def get_account(db: Session, actor: User, account_id: int) -> GcashAccount:
    account = db.get(GcashAccount, account_id)
    if account is None:
        raise NotFound("GCash number not found.")
    _can_manage(actor, account)
    return account


def update_account(db: Session, actor: User, account_id: int, *, label: str, note: str | None,
                   daily_limit_cents: int | None, monthly_limit_cents: int | None) -> GcashAccount:
    account = get_account(db, actor, account_id)
    before = {"label": account.label, "daily_limit_cents": account.daily_limit_cents,
              "monthly_limit_cents": account.monthly_limit_cents}
    account.label = _clean_label(label)
    account.note = (note or "").strip()[:255] or None
    account.daily_limit_cents = _clean_limit(daily_limit_cents, "Daily limit")
    account.monthly_limit_cents = _clean_limit(monthly_limit_cents, "Monthly limit")
    db.flush()
    audit.record(db, actor, "gcash.account.update", entity_type="gcash_account", entity_id=account.id,
                 before=before, after={"label": account.label,
                                       "daily_limit_cents": account.daily_limit_cents,
                                       "monthly_limit_cents": account.monthly_limit_cents})
    return account


def set_active(db: Session, actor: User, account_id: int, active: bool) -> GcashAccount:
    account = get_account(db, actor, account_id)
    if account.is_active != active:
        account.is_active = active
        db.flush()
        audit.record(db, actor, "gcash.account.resume" if active else "gcash.account.pause",
                     entity_type="gcash_account", entity_id=account.id)
    return account


def move(db: Session, actor: User, account_id: int, direction: int) -> None:
    """Swap list position with the neighbour (rotation order under the 'fill' strategy)."""
    account = get_account(db, actor, account_id)
    peers = list_accounts(db)
    for n, row in enumerate(peers):          # tidy: legacy rows may share the same sort_order
        row.sort_order = n
    i = peers.index(account)
    j = i + (-1 if direction < 0 else 1)
    if 0 <= j < len(peers):
        peers[i].sort_order, peers[j].sort_order = peers[j].sort_order, peers[i].sort_order
    db.flush()


def remove_account(db: Session, actor: User, account_id: int) -> str:
    """Delete a number that was never used; otherwise just pause it (history keeps pointing at it)."""
    account = get_account(db, actor, account_id)
    used = db.scalar(select(CashRequest.id).where(CashRequest.gcash_account_id == account.id).limit(1))
    adjusted = db.scalar(select(GcashAdjustment.id).where(GcashAdjustment.account_id == account.id).limit(1))
    label = account.label
    if used or adjusted:
        account.is_active = False
        db.flush()
        audit.record(db, actor, "gcash.account.pause", entity_type="gcash_account", entity_id=account.id,
                     reason="removed (has history)")
        return f"{label} has history, so it was paused instead of deleted."
    db.delete(account)
    db.flush()
    audit.record(db, actor, "gcash.account.delete", entity_type="gcash_account", entity_id=account_id,
                 before={"label": label})
    return f"{label} was removed."


def log_outside_usage(db: Session, actor: User, account_id: int, *, amount_cents: int,
                      note: str | None) -> GcashAdjustment:
    account = get_account(db, actor, account_id)
    if amount_cents <= 0:
        raise ValidationError("Enter an amount above zero.")
    row = GcashAdjustment(account_id=account.id, amount_cents=amount_cents,
                          note=(note or "").strip()[:255] or None, created_by_id=actor.id)
    db.add(row)
    db.flush()
    audit.record(db, actor, "gcash.account.adjust", entity_type="gcash_account", entity_id=account.id,
                 after={"amount_cents": amount_cents, "note": row.note})
    return row


def seed_from_teller(db: Session, teller: User, number: str) -> GcashAccount | None:
    """A Teller created with a GCash number gets it as their first pool entry."""
    clean = normalize_number(number)
    digest = _hash(clean)
    if db.scalar(select(GcashAccount.id).where(GcashAccount.number_hash == digest)):
        return None
    last = db.scalar(select(func.max(GcashAccount.sort_order))) or 0
    account = GcashAccount(owner_id=teller.id, label=f"{teller.full_name} · main", number_enc=pii.encrypt(clean),
                           number_hash=digest, is_active=True, sort_order=int(last) + 1)
    db.add(account)
    db.flush()
    return account


# --------------------------------------------------------------------------- recent activity

def recent_for(db: Session, account_ids: list[int], limit: int = 12) -> list[CashRequest]:
    if not account_ids:
        return []
    return list(db.scalars(select(CashRequest).where(CashRequest.gcash_account_id.in_(account_ids))
                           .order_by(CashRequest.id.desc()).limit(limit)).all())
