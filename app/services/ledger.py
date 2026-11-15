"""Double-entry ledger engine.

Rules enforced here (and by DB constraints/triggers):

1. Every transaction's signed entries sum to exactly zero.
2. Entries are append-only. A mistake is fixed by a *reversal*, never an edit.
3. Balances are derived and cached under a row lock, with a version counter.
4. Wallet-type accounts cannot go negative unless explicitly allowed.
5. Every money movement can carry an idempotency key: replaying the same key
   returns the original transaction instead of moving money twice.

Sign convention: `amount_cents` is **debit-positive**.
  * asset/expense accounts increase on a debit  (+)
  * liability/revenue accounts increase on a credit (-)
`balance_cents` is always stored in the account's natural (positive) direction.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, InsufficientFunds, ValidationError
from app.db.models import Account, LedgerEntry, LedgerTxn, User

Entry = tuple[Account, int]


class LedgerImbalance(ValidationError):
    pass


def _lock_accounts(db: Session, accounts: Iterable[Account]) -> list[Account]:
    """Lock in a stable order (ascending id) so concurrent transfers cannot deadlock."""
    ids = sorted({a.id for a in accounts})
    if not ids:
        return []
    stmt = (
        select(Account)
        .where(Account.id.in_(ids))
        .order_by(Account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(db.scalars(stmt).all())


def find_by_key(db: Session, key: str | None) -> LedgerTxn | None:
    if not key:
        return None
    return db.scalar(select(LedgerTxn).where(LedgerTxn.idempotency_key == key))


def post(
    db: Session,
    *,
    kind: str,
    entries: Sequence[Entry],
    actor: User | None = None,
    memo: str = "",
    ref_type: str | None = None,
    ref_id: int | None = None,
    idempotency_key: str | None = None,
    reason: str | None = None,
    meta: dict | None = None,
    force: bool = False,
) -> LedgerTxn:
    """Post a balanced transaction.

    `force` permits a liability balance to go negative -- used only by audited
    claw-backs when a settled fight is redeclared after winnings were spent.
    The resulting negative balance is a recorded debt, never a silent loss.
    """
    existing = find_by_key(db, idempotency_key)
    if existing is not None:
        return existing

    if len(entries) < 2:
        raise LedgerImbalance("A transaction needs at least two entries.")
    if any(amount == 0 for _, amount in entries):
        raise LedgerImbalance("Zero-amount entries are not allowed.")
    if sum(amount for _, amount in entries) != 0:
        raise LedgerImbalance("Transaction does not balance.")

    # Merge duplicate accounts so a txn cannot touch the same balance twice.
    merged: dict[int, int] = {}
    by_id: dict[int, Account] = {}
    for account, amount in entries:
        if account.id is None:
            raise ValidationError("Account must be persisted before posting.")
        merged[account.id] = merged.get(account.id, 0) + amount
        by_id[account.id] = account
    merged = {k: v for k, v in merged.items() if v != 0}
    if len(merged) < 2:
        raise LedgerImbalance("Transaction collapses to fewer than two entries.")

    locked = {a.id: a for a in _lock_accounts(db, by_id.values())}

    txn = LedgerTxn(
        uuid=audit_uuid(),
        kind=kind,
        memo=memo[:255],
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        actor_id=actor.id if actor else None,
        reason=reason[:255] if reason else None,
        meta=meta,
    )
    db.add(txn)
    db.flush()

    for account_id in sorted(merged):
        account = locked[account_id]
        amount = merged[account_id]
        delta = amount if account.is_debit_normal else -amount
        new_balance = account.balance_cents + delta
        if new_balance < 0 and not account.allow_negative and not force:
            raise InsufficientFunds(f"{account.label} has insufficient balance.")
        account.balance_cents = new_balance
        account.version += 1
        db.add(LedgerEntry(
            txn_id=txn.id, account_id=account.id,
            amount_cents=amount, balance_after_cents=new_balance,
        ))
    db.flush()
    return txn


def reverse(
    db: Session,
    txn: LedgerTxn,
    *,
    actor: User | None = None,
    reason: str,
    idempotency_key: str | None = None,
    memo: str | None = None,
    force: bool = False,
) -> LedgerTxn:
    """Mirror every entry. Used by ticket cancel, redeclare and recompute."""
    existing = find_by_key(db, idempotency_key)
    if existing is not None:
        return existing
    if txn.reversed_by_txn_id is not None:
        raise Conflict("That transaction has already been reversed.")
    if not reason or not reason.strip():
        raise ValidationError("A reason is required to reverse a transaction.")

    mirrored: list[Entry] = [(e.account, -e.amount_cents) for e in txn.entries]
    reversal = post(
        db,
        kind=f"{txn.kind}.reversal",
        entries=mirrored,
        actor=actor,
        memo=memo or f"Reversal of {txn.uuid}",
        ref_type=txn.ref_type,
        ref_id=txn.ref_id,
        idempotency_key=idempotency_key,
        reason=reason,
        meta={"reverses": txn.uuid, "forced": force},
        force=force,
    )
    txn.reversed_by_txn_id = reversal.id
    reversal.reverses_txn_id = txn.id
    db.flush()
    return reversal


def audit_uuid() -> str:
    from app.db.base import new_uuid
    return new_uuid()


# ---------------------------------------------------------------------------
# Reporting / integrity
# ---------------------------------------------------------------------------

def balance_of(db: Session, account: Account) -> int:
    return int(account.balance_cents)


def history(db: Session, account_id: int, limit: int = 50, offset: int = 0) -> list[LedgerEntry]:
    return list(db.scalars(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.id.desc()).limit(limit).offset(offset)
    ).all())


def verify_integrity(db: Session) -> dict:
    """Reconciliation job. Returns a report; `ok` must always be True."""
    unbalanced = db.execute(
        select(LedgerEntry.txn_id, func.sum(LedgerEntry.amount_cents).label("net"))
        .group_by(LedgerEntry.txn_id)
        .having(func.sum(LedgerEntry.amount_cents) != 0)
    ).all()

    accounts = list(db.scalars(select(Account)).all())
    mismatches = []
    trial = {"asset": 0, "liability": 0, "revenue": 0, "expense": 0}
    for account in accounts:
        net = db.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
            .where(LedgerEntry.account_id == account.id)
        ) or 0
        expected = net if account.is_debit_normal else -net
        if expected != account.balance_cents:
            mismatches.append({"account": account.code, "cached": account.balance_cents, "ledger": expected})
        trial[str(account.acct_type)] += expected

    trial_ok = (trial["asset"] + trial["expense"]) == (trial["liability"] + trial["revenue"])
    return {
        "ok": not unbalanced and not mismatches and trial_ok,
        "unbalanced_txns": [row[0] for row in unbalanced],
        "balance_mismatches": mismatches,
        "trial_balance": trial,
        "accounts": len(accounts),
    }
