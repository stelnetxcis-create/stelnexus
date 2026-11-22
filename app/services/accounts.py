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


def _preview_account(db: Session, code: str, kind: AccountKind, label: str) -> Account:
    """DEV 'view as a player': read the real account if there is one, else a zero balance that is never saved
    (a preview must not create rows)."""
    return get_by_code(db, code) or Account(code=code, kind=kind, acct_type=KIND_TYPE[kind], label=label[:120],
                                            allow_negative=False, balance_cents=0)


def wallet_for_user(db: Session, user: User) -> Account:
    preview_pid = getattr(user, "preview_player_id", None)
    if preview_pid is not None:
        return _preview_account(db, f"wallet:player:{preview_pid}", AccountKind.WALLET, f"Credit · {user.username}")
    code = f"wallet:user:{user.id}"
    existing = get_by_code(db, code)
    if existing is not None:
        return existing
    _must_hold_money(user)
    return _create(db, code, AccountKind.WALLET, f"Credit · {user.username}",
                   allow_negative=False, owner_user_id=user.id)


def commission_for_user(db: Session, user: User) -> Account:
    if getattr(user, "preview_player_id", None) is not None:      # a player earns no commission
        return Account(code=f"commission:player:{user.preview_player_id}", kind=AccountKind.COMMISSION_PAYABLE,
                       acct_type=KIND_TYPE[AccountKind.COMMISSION_PAYABLE], label="Commission", allow_negative=True,
                       balance_cents=0)
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


def user_balances(db: Session, users: list[User]) -> dict[int, tuple[int | None, int | None]]:
    """``{user_id: (wallet_cents, commission_cents)}`` for many users in ONE query.

    Replaces calling ``wallet_for_user`` / ``commission_for_user`` once per row on list pages
    (an N+1). A user whose account does not exist yet falls back to the per-user helper, which
    provisions it, so behaviour matches the single-row functions. Users that hold no money get
    ``(None, None)``."""
    wanted = [u for u in users if u.has_wallet]
    out: dict[int, tuple[int | None, int | None]] = {u.id: (None, None) for u in users}
    if not wanted:
        return out
    codes = [f"wallet:user:{u.id}" for u in wanted] + [f"commission:user:{u.id}" for u in wanted]
    found = {code: bal for code, bal in db.execute(
        select(Account.code, Account.balance_cents).where(Account.code.in_(codes)))}
    for u in wanted:
        w = found.get(f"wallet:user:{u.id}")
        c = found.get(f"commission:user:{u.id}")
        if w is None:
            w = wallet_for_user(db, u).balance_cents
        if c is None:
            c = commission_for_user(db, u).balance_cents
        out[u.id] = (int(w), int(c))
    return out


def player_wallets(db: Session, player_ids: list[int]) -> dict[int, int]:
    """``{player_id: balance_cents}`` in one query (players with no wallet yet are omitted)."""
    if not player_ids:
        return {}
    return {pid: int(bal) for pid, bal in db.execute(
        select(Account.owner_player_id, Account.balance_cents)
        .where(Account.kind == AccountKind.WALLET, Account.owner_player_id.in_(player_ids)))}


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
