"""The wallet hub: an account's own balance, who it can send credit to, and its money history.

Every account that holds money (Admin, Master, Cavo, Kubrador) uses the same screen. Declarators
and Pawns have no balance at all: they cannot send, receive, load or cash out.

History is read straight from the double-entry ledger, so what a person sees under "money in" and
"money out" is exactly what moved their balance and always adds up to it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Forbidden, NotFound, ValidationError
from app.core.money import fmt
from app.core.rbac import downline_ids, has_global_scope
from app.core.settings_registry import SettingsStore
from app.db.models import LedgerEntry, LedgerTxn, Notification, Role, Status, User
from app.services import accounts, ledger

# ledger txn kind -> (label, what it is for the person)
KIND_LABELS: dict[str, str] = {
    "credit.transfer": "Transfer",
    "request.in": "Load (cash in)",
    "request.out": "Cash out",
    "commission.convert": "Commission → credit",
    "bet.stake": "Bet placed",
    "fight.settle": "Winnings / refund",
}
PERIODS = {"all": None, "today": 1, "7d": 7, "30d": 30}
FLOWS = ("all", "in", "out")


def label_for(kind: str) -> str:
    if kind.endswith(".reversal"):
        return "Reversal · " + label_for(kind[: -len(".reversal")])
    return KIND_LABELS.get(kind, kind.replace(".", " ").replace("_", " ").title())


def display_name(user: User | None) -> str:
    """The hidden Admin is 'System' on anything another person can read."""
    if user is None:
        return "—"
    return "System" if user.is_hidden else user.username


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

def _upline_ids(db: Session, user: User) -> set[int]:
    out: set[int] = set()
    cursor = user
    while cursor is not None and cursor.parent_id and cursor.parent_id not in out and len(out) < 12:
        out.add(cursor.parent_id)
        cursor = db.get(User, cursor.parent_id)
    return out


def _network_ids(db: Session, user: User) -> set[int]:
    return downline_ids(db, user) | _upline_ids(db, user)


def _eligible(actor: User, candidate: User) -> bool:
    if candidate.id == actor.id or not candidate.has_wallet or candidate.status != Status.ACTIVE:
        return False
    if candidate.role == Role.TELLER:
        return False
    if candidate.is_hidden and actor.role != Role.ADMIN:
        return False                       # the hidden Admin cannot be found or paid by anyone else
    return True


def recipients(db: Session, actor: User, settings: SettingsStore, *, limit: int = 400) -> list[User]:
    """Accounts `actor` may send credit to, for the picker."""
    if not actor.has_wallet:
        return []
    stmt = select(User).where(User.status == Status.ACTIVE, User.role.in_(
        [Role.ADMIN, Role.MASTER, Role.CAVO, Role.KUBRADOR]), User.id != actor.id)
    if actor.role != Role.ADMIN:
        stmt = stmt.where(User.role != Role.ADMIN)
    if settings.get("wallet.transfer_scope") == "network" and not has_global_scope(actor):
        stmt = stmt.where(User.id.in_(_network_ids(db, actor) or {-1}))
    return list(db.scalars(stmt.order_by(User.username).limit(limit)).all())


def resolve_recipient(db: Session, actor: User, settings: SettingsStore, *,
                      target_id: int | None = None, username: str | None = None) -> User:
    target = None
    if target_id:
        target = db.get(User, target_id)
    elif username:
        name = username.strip().lstrip("@").lower()
        if name:
            target = db.scalar(select(User).where(func.lower(User.username) == name))
    # A hidden or unknown account answers exactly like a missing one.
    if target is None or (target.is_hidden and actor.role != Role.ADMIN):
        raise NotFound("No account with that username.")
    if target.id == actor.id:
        raise ValidationError("You cannot send credit to yourself.")
    if not target.has_wallet:
        raise ValidationError(f"{target.username} is a {target.role_label} and does not hold credit.")
    if target.status != Status.ACTIVE:
        raise ValidationError("That account is not active.")
    if settings.get("wallet.transfer_scope") == "network" and not has_global_scope(actor):
        if target.id not in _network_ids(db, actor):
            raise Forbidden("You can only send credit to your own upline and downline.")
    return target


def transfer(
    db: Session, actor: User, *, amount_cents: int, settings: SettingsStore,
    target_id: int | None = None, username: str | None = None,
    note: str | None = None, idempotency_key: str | None = None,
) -> LedgerTxn:
    """Send credit from the actor's wallet to another account's wallet. Atomic, audited, idempotent."""
    if not actor.has_wallet:
        raise Forbidden(f"A {actor.role_label} account has no balance, so it cannot send credit.")
    if actor.status != Status.ACTIVE:
        raise Forbidden("Your account is not active.")
    target = resolve_recipient(db, actor, settings, target_id=target_id, username=username)

    minimum = int(settings.get("wallet.min_transfer_cents"))
    maximum = int(settings.get("wallet.max_transfer_cents"))
    if amount_cents < minimum or amount_cents > maximum:
        raise ValidationError(f"Transfer must be between {fmt(minimum)} and {fmt(maximum)}.")
    note = (note or "").strip()[:200] or None

    sender = accounts.wallet_for_user(db, actor)
    receiver = accounts.wallet_for_user(db, target)
    txn = ledger.post(
        db, kind="credit.transfer",
        entries=[(sender, +amount_cents), (receiver, -amount_cents)],
        actor=actor, memo=f"{display_name(actor)} → {display_name(target)}",
        ref_type="user", ref_id=target.id, idempotency_key=idempotency_key, reason=note,
        meta={"from_user_id": actor.id, "to_user_id": target.id},
    )
    audit.record(db, actor, "credit.transfer", entity_type="user", entity_id=target.id,
                 after={"amount_cents": amount_cents, "to": target.username, "txn": txn.uuid}, reason=note)
    db.add(Notification(user_id=target.id, level="info", link="/wallet?t=history",
                        message=f"{display_name(actor)} sent you {fmt(amount_cents)}"))
    db.flush()
    return txn


# ---------------------------------------------------------------------------
# Money history
# ---------------------------------------------------------------------------

@dataclass
class MoneyRow:
    when: dt.datetime
    label: str
    memo: str
    note: str | None
    direction: str            # "in" | "out"
    amount_cents: int         # always positive
    balance_after_cents: int
    txn_uuid: str


def _base(wallet_id: int, period: str):
    stmt = (select(LedgerEntry, LedgerTxn).join(LedgerTxn, LedgerTxn.id == LedgerEntry.txn_id)
            .where(LedgerEntry.account_id == wallet_id))
    days = PERIODS.get(period)
    if days:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        if days == 1:
            since = since.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = stmt.where(LedgerEntry.created_at >= since)
    return stmt


def history(db: Session, user: User, *, flow: str = "all", period: str = "all",
            limit: int = 40, offset: int = 0) -> list[MoneyRow]:
    if not user.has_wallet:
        return []
    wallet = accounts.wallet_for_user(db, user)
    stmt = _base(wallet.id, period if period in PERIODS else "all")
    if flow == "in":
        stmt = stmt.where(LedgerEntry.amount_cents < 0)       # wallet is a liability: credit = money in
    elif flow == "out":
        stmt = stmt.where(LedgerEntry.amount_cents > 0)
    rows = db.execute(stmt.order_by(LedgerEntry.id.desc()).limit(min(limit, 200)).offset(max(offset, 0))).all()
    out = []
    for entry, txn in rows:
        out.append(MoneyRow(
            when=entry.created_at, label=label_for(txn.kind), memo=txn.memo or "", note=txn.reason,
            direction="in" if entry.amount_cents < 0 else "out", amount_cents=abs(entry.amount_cents),
            balance_after_cents=entry.balance_after_cents, txn_uuid=txn.uuid))
    return out


def summary(db: Session, user: User, *, period: str = "all") -> dict:
    """Totals and a per-category breakdown of where the money came from and went."""
    if not user.has_wallet:
        return {"in": 0, "out": 0, "net": 0, "categories": []}
    wallet = accounts.wallet_for_user(db, user)
    stmt = (select(LedgerTxn.kind,
                   func.coalesce(func.sum(case((LedgerEntry.amount_cents < 0, -LedgerEntry.amount_cents), else_=0)), 0),
                   func.coalesce(func.sum(case((LedgerEntry.amount_cents > 0, LedgerEntry.amount_cents), else_=0)), 0))
            .join(LedgerTxn, LedgerTxn.id == LedgerEntry.txn_id)
            .where(LedgerEntry.account_id == wallet.id))
    days = PERIODS.get(period if period in PERIODS else "all")
    if days:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        if days == 1:
            since = since.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = stmt.where(LedgerEntry.created_at >= since)
    cats: dict[str, dict] = {}
    total_in = total_out = 0
    for kind, money_in, money_out in db.execute(stmt.group_by(LedgerTxn.kind)).all():
        label = label_for(kind)
        slot = cats.setdefault(label, {"label": label, "in": 0, "out": 0})
        slot["in"] += int(money_in)
        slot["out"] += int(money_out)
        total_in += int(money_in)
        total_out += int(money_out)
    categories = sorted((c for c in cats.values() if c["in"] or c["out"]),
                        key=lambda c: -(c["in"] + c["out"]))
    return {"in": total_in, "out": total_out, "net": total_in - total_out, "categories": categories}
