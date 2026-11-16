"""Account provisioning. One wallet per actor, one escrow per fight, and a small
set of system accounts. No hard-coded user ids anywhere (the old cashflow.php
summed `tbl_users.credit` for ids 167-171)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Forbidden, NotFound
from app.db.models import (
    KIND_TYPE,
    Account,
    AccountKind,
    BolaDraw,
    Fight,
    Player,
    User,
)

SYSTEM_ACCOUNTS: tuple[tuple[str, AccountKind, str, bool], ...] = (
    ("sys:house", AccountKind.HOUSE_REVENUE, "House Revenue (Tong)", True),
    ("sys:rounding", AccountKind.ROUNDING, "Rounding Remainder", True),
    ("sys:commission", AccountKind.COMMISSION_EXPENSE, "Commission Expense", True),
    ("sys:cash", AccountKind.CASH_ON_HAND, "Legacy Cash (retired, pre-online)", True),
    ("sys:gcash", AccountKind.GCASH_CLEARING, "Online Payments Clearing (GCash / Bank)", True),
)


def get_by_code(db: Session, code: str) -> Account | None:
    return db.scalar(select(Account).where(Account.code == code))


def require_by_code(db: Session, code: str) -> Account:
    account = get_by_code(db, code)
    if account is None:
        raise NotFound(f"Account {code} is missing. Run the database bootstrap.")
    return account


def _create(db: Session, code: str, kind: AccountKind, label: str, *, allow_negative: bool = False, **owners) -> Account:
    account = Account(
        code=code, kind=kind, acct_type=KIND_TYPE[kind], label=label[:120],
        allow_negative=allow_negative, balance_cents=0, **owners,
    )
    db.add(account)
    db.flush()
    return account


def ensure_system_accounts(db: Session) -> None:
    for code, kind, label, allow_negative in SYSTEM_ACCOUNTS:
        if get_by_code(db, code) is None:
            _create(db, code, kind, label, allow_negative=allow_negative)


def _must_hold_money(user: User) -> None:
    if not user.has_wallet:
        raise Forbidden(f"A {user.role_label} account has no balance and cannot hold, send or receive credit.")


def wallet_for_user(db: Session, user: User) -> Account:
    code = f"wallet:user:{user.id}"
    existing = get_by_code(db, code)
    if existing is not None:
        return existing
    _must_hold_money(user)
    return _create(db, code, AccountKind.WALLET, f"Credit · {user.username}",
                   allow_negative=False, owner_user_id=user.id)


def commission_for_user(db: Session, user: User) -> Account:
    code = f"commission:user:{user.id}"
    existing = get_by_code(db, code)
    if existing is not None:
        return existing
    _must_hold_money(user)
    return _create(
        db, code, AccountKind.COMMISSION_PAYABLE, f"Commission · {user.username}",
        allow_negative=True,  # a redeclare claw-back can leave an agent owing
        owner_user_id=user.id,
    )


def wallet_for_player(db: Session, player: Player) -> Account:
    code = f"wallet:player:{player.id}"
    return get_by_code(db, code) or _create(
        db, code, AccountKind.WALLET, f"Card/Player · {player.full_name}",
        owner_player_id=player.id,
    )


def pool_for_fight(db: Session, fight: Fight) -> Account:
    code = f"pool:fight:{fight.id}"
    account = get_by_code(db, code)
    if account is None:
        account = _create(
            db, code, AccountKind.FIGHT_POOL,
            f"Pool · Game #{fight.number}", owner_fight_id=fight.id,
        )
    if fight.pool_account_id != account.id:
        fight.pool_account_id = account.id
        db.flush()
    return account


def pool_for_draw(db: Session, draw: BolaDraw) -> Account:
    """Escrow holding the stakes of one number-game draw. Ordinary liability account: the ledger's
    integrity check covers it with no special casing."""
    code = f"pool:draw:{draw.id}"
    account = get_by_code(db, code)
    if account is None:
        account = _create(
            db, code, AccountKind.BOLA_POOL,
            f"Bola pool · {draw.draw_date} slot {draw.slot}", owner_draw_id=draw.id,
        )
    if draw.pool_account_id != account.id:
        draw.pool_account_id = account.id
        db.flush()
    return account


def house(db: Session) -> Account:
    return require_by_code(db, "sys:house")


def rounding(db: Session) -> Account:
    return require_by_code(db, "sys:rounding")


def commission_expense(db: Session) -> Account:
    return require_by_code(db, "sys:commission")


def central_cash(db: Session) -> Account:
    return require_by_code(db, "sys:cash")


def gcash_clearing(db: Session) -> Account:
    return require_by_code(db, "sys:gcash")
