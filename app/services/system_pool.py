"""The system pool: the house reserve.

Every settled fight and Bola draw sets aside a share of the game's cut (``commission.reserve_bps`` /
``bola.commission.reserve_bps``, 30% out of the box) before the Master / Agent / Sub Agent split. That share is posted to
the ``sys:pool`` ledger account in the same transaction as the commissions, so a redeclare reverses it with them.

Nothing here moves money: the pool only ever fills from settlement and is shown, read-only, on the Teller page to the
Admin, Bishops, Tellers and any Pawn the Admin granted ``systempool:view``.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import LedgerEntry, LedgerTxn
from app.services import accounts

RECENT = 12
GAME_OF_REF = {"fight": "sabong", "bola_draw": "bola"}


def summary(db: Session, limit: int = RECENT) -> dict:
    """The balance, what each game put in, and the latest additions (reversals show as negative lines)."""
    account = accounts.get_by_code(db, "sys:pool")
    out = {"balance": 0, "sabong": 0, "bola": 0, "recent": []}
    if account is None:
        return out
    out["balance"] = int(account.balance_cents)
    for ref_type, total in db.execute(
            select(LedgerTxn.ref_type, func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
            .join(LedgerTxn, LedgerTxn.id == LedgerEntry.txn_id)
            .where(LedgerEntry.account_id == account.id).group_by(LedgerTxn.ref_type)).all():
        game = GAME_OF_REF.get(ref_type)
        if game:
            out[game] += -int(total)                       # a credit to the pool is a negative, debit-positive entry
    rows = db.execute(
        select(LedgerEntry, LedgerTxn).join(LedgerTxn, LedgerTxn.id == LedgerEntry.txn_id)
        .where(LedgerEntry.account_id == account.id).order_by(LedgerEntry.id.desc()).limit(limit)).all()
    out["recent"] = [{"at": e.created_at, "memo": t.memo, "amount": -int(e.amount_cents),
                      "game": GAME_OF_REF.get(t.ref_type, ""), "reversal": t.kind.endswith(".reversal")} for e, t in rows]
    return out
