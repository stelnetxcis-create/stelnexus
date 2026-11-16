"""StelNexus data model (PostgreSQL).

Design notes / fixes vs. the original `online_bukis` schema:

* **Money is a double-entry ledger.** `tbl_users.credit`, `tbl_bettor.credit_load`
  and `cashless_cash_inventory` were three independent, mutually-unreconcilable
  numbers. Here every centavo moves through balanced, append-only ledger entries,
  so "wallet", "on-hand cash" and "remit" are *derived* and always agree.
* **Immutable audit.** `audit_logs`, `ledger_entries` and `ledger_txns` are guarded
  by database triggers (see migrations) -- not even the app user can rewrite them.
* **Idempotency keys** on every money-moving operation, so a double-tapped
  "Cash In" button on a slow mobile connection cannot double-credit.
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    BigInteger,
    Enum as SAEnum,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDMixin, UtcDateTime, utcnow

JsonType = JSON().with_variant(JSONB(), "postgresql")
Money = BigInteger  # centavos


def EnumStr(enum_cls, length: int):
    """Portable enum: stored as VARCHAR + CHECK, always bound as its .value."""
    return SAEnum(
        enum_cls, native_enum=False, length=length,
        values_callable=lambda e: [m.value for m in e], validate_strings=True,
    )


class StrEnum(str, enum.Enum):
    """str-valued enum whose str() is the value (safe in f-strings and JSON)."""

    def __str__(self) -> str:
        return self.value


class Role(StrEnum):
    """Who is who.

    admin      Hidden developer account. Same powers as Master plus the hidden layer
               (Settings, Audit Trail, ledger integrity, System/Hidden Over). It never
               appears in any list, count, tree or name shown to anyone else.
    master     Top of the visible network ("Management" in the old reports).
    cavo       Invited by a Master. Earns the Volume % cut.
    kubrador   Invited by a Cavo. Earns the SOP % taken out of the Cavo's cut.
    teller     RETIRED. The platform is online-only; old rows are closed by the migration.
    declarator Declares results only; never sees totals (was hard-coded id 78). No balance,
               cannot bet or move money: a spectator/helper for the event.
    pawn       Created by the Admin only. A spectator like the declarator (no balance, no betting)
               whose reach into the Admin Panel is set per account by the Admin (``User.grants``).

    Normal accounts (players) are not users: a Kubrador's referral code creates a Player.
    """
    ADMIN = "admin"      # legacy UTYPE '0'
    MASTER = "master"
    CAVO = "cavo"
    KUBRADOR = "kubrador"
    TELLER = "teller"
    DECLARATOR = "declarator"
    PAWN = "pawn"

    @property
    def label(self) -> str:
        return {
            "admin": "Admin", "master": "Master", "cavo": "Cavo", "kubrador": "Kubrador",
            "teller": "Teller", "declarator": "Declarator", "pawn": "Pawn",
        }[self.value]


AGENT_ROLES = (Role.ADMIN, Role.MASTER, Role.CAVO, Role.KUBRADOR)
# Spectator / helper accounts: no balance, no bets, no money movement, nobody's upline.
NO_WALLET_ROLES = (Role.DECLARATOR, Role.PAWN)
TOP_ROLES = (Role.ADMIN, Role.MASTER)          # the operator tier: see everything visible
# Strict downline tree. A Kubrador's children are Players (a separate table).
CHILD_ROLE: dict[Role, Role | None] = {
    Role.ADMIN: Role.MASTER,
    Role.MASTER: Role.CAVO,
    Role.CAVO: Role.KUBRADOR,
    Role.KUBRADOR: None,
    Role.TELLER: None,
    Role.DECLARATOR: None,
    Role.PAWN: None,
}
# What a referral code issued by this role turns the invitee into.
INVITE_ROLE: dict[Role, str | None] = {
    Role.ADMIN: "master",
    Role.MASTER: "cavo",
    Role.CAVO: "kubrador",
    Role.KUBRADOR: "player",
    Role.TELLER: None,
    Role.DECLARATOR: None,
    Role.PAWN: None,
}
CUT_ROLES = (Role.CAVO, Role.KUBRADOR)


class Status(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class AccountKind(StrEnum):
    WALLET = "wallet"                       # agent / player spendable balance
    COMMISSION_PAYABLE = "commission_payable"
    STATION_CASH = "station_cash"           # RETIRED: historical ledger accounts only
    FIGHT_POOL = "fight_pool"               # escrow holding stakes of one fight
    HOUSE_REVENUE = "house_revenue"         # plasada
    COMMISSION_EXPENSE = "commission_expense"
    ROUNDING = "rounding"                   # integer-division remainder
    CASH_ON_HAND = "cash_on_hand"           # RETIRED: legacy cash, kept so old ledger rows load
    GCASH_CLEARING = "gcash_clearing"


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    REVENUE = "revenue"
    EXPENSE = "expense"


KIND_TYPE: dict[AccountKind, AccountType] = {
    AccountKind.WALLET: AccountType.LIABILITY,
    AccountKind.COMMISSION_PAYABLE: AccountType.LIABILITY,
    AccountKind.STATION_CASH: AccountType.ASSET,
    AccountKind.FIGHT_POOL: AccountType.LIABILITY,
    AccountKind.HOUSE_REVENUE: AccountType.REVENUE,
    AccountKind.COMMISSION_EXPENSE: AccountType.EXPENSE,
    AccountKind.ROUNDING: AccountType.REVENUE,
    AccountKind.CASH_ON_HAND: AccountType.ASSET,
    AccountKind.GCASH_CLEARING: AccountType.ASSET,
}
DEBIT_NORMAL = {AccountType.ASSET, AccountType.EXPENSE}


class Side(StrEnum):
    MERON = "meron"
    WALA = "wala"
    DRAW = "draw"        # side bet with its own fixed payout (setting payout.draw_odds_bps)


class BetMode(StrEnum):
    """Which sides a fight accepts. Admin can force one side open (old 'force bet')."""
    ALL = "all"
    MERON = "meron"
    WALA = "wala"


class Result(StrEnum):
    MERON = "meron"
    WALA = "wala"
    DRAW = "draw"
    CANCELED = "canceled"


class FightStatus(StrEnum):
    OPEN = "open"
    LOCKED = "locked"
    SETTLED = "settled"
    VOID = "void"


class EventStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"   # whole event called off; every open stake refunded


class PayoutModel(StrEnum):
    POOL = "pool"        # pari-mutuel: winners split the net pool (default)
    FIXED = "fixed"      # admin-set multiplier per side


class TicketStatus(StrEnum):
    ACTIVE = "active"
    CANCELED = "canceled"
    SETTLED = "settled"
    REFUNDED = "refunded"


class CommissionStatus(StrEnum):
    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    REVERSED = "reversed"


class RequestDirection(StrEnum):
    IN = "in"
    OUT = "out"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    REVERSED = "reversed"


class PlayerKind(StrEnum):
    CARD = "card"        # cashless tap-card player (clp.php)
    ONLINE = "online"    # GCash-funded online player (cashin_out.php)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(EnumStr(Role, 16), index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    contact: Mapped[str | None] = mapped_column(String(32))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    ref_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    status: Mapped[Status] = mapped_column(EnumStr(Status, 16), default=Status.ACTIVE, index=True)

    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    password_changed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    # Cut system (basis points; NULL = inherit the default from Settings -> Commissions).
    #   cavo:      cut_bps = Volume %  (share of the commission pool the Cavo's network keeps)
    #              sop_bps = SOP %     (share of that cut passed to each of its Kubradors)
    #   kubrador:  cut_bps = personal SOP % override (NULL = use the Cavo's sop_bps)
    cut_bps: Mapped[int | None] = mapped_column(Integer)
    sop_bps: Mapped[int | None] = mapped_column(Integer)

    # Pawn only: the list of grant keys the Admin switched on (see app.core.rbac.GRANTS).
    grants: Mapped[list | None] = mapped_column(JsonType)

    parent: Mapped["User | None"] = relationship(
        remote_side="User.id", back_populates="children", foreign_keys="User.parent_id")
    children: Mapped[list["User"]] = relationship(
        back_populates="parent", foreign_keys="User.parent_id")

    __table_args__ = (
        CheckConstraint("role in ('admin','master','cavo','kubrador','teller','declarator','pawn')", name="role_valid"),
        CheckConstraint("id <> parent_id", name="no_self_parent"),
        CheckConstraint("cut_bps is null or cut_bps between 0 and 10000", name="cut_range"),
        CheckConstraint("sop_bps is null or sop_bps between 0 and 10000", name="sop_range"),
        Index("ix_users_parent_status", "parent_id", "status"),
    )

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_hidden(self) -> bool:
        """Hidden accounts exist in the database like any other row but are never shown."""
        return self.role == Role.ADMIN

    @property
    def is_top(self) -> bool:
        """Admin or Master: the operator tier."""
        return self.role in TOP_ROLES

    @property
    def role_label(self) -> str:
        return Role(self.role).label

    @property
    def has_wallet(self) -> bool:
        """Declarators and Pawns never hold, send or receive money."""
        return self.role not in NO_WALLET_ROLES

    @property
    def grant_set(self) -> set[str]:
        return set(self.grants or []) if self.role == Role.PAWN else set()


class Player(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[PlayerKind] = mapped_column(EnumStr(PlayerKind, 8), index=True)
    card_number: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(120))
    birth_date: Mapped[dt.date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(16))
    address_enc: Mapped[str | None] = mapped_column(Text)     # encrypted at rest
    gcash_enc: Mapped[str | None] = mapped_column(Text)       # encrypted at rest
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    status: Mapped[Status] = mapped_column(EnumStr(Status, 16), default=Status.ACTIVE, index=True)
    kyc_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    age_verified_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    self_excluded_until: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    daily_bet_limit_cents: Mapped[int | None] = mapped_column(Money)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)

    agent: Mapped[User | None] = relationship(foreign_keys=[agent_id])

    __table_args__ = (
        CheckConstraint("kind in ('card','online')", name="kind_valid"),
        CheckConstraint("daily_bet_limit_cents is null or daily_bet_limit_cents >= 0", name="limit_nonneg"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)
    absolute_expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime())
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())

    user: Mapped[User] = relationship()


class ApiToken(TimestampMixin, Base):
    """Bearer tokens for the mobile client (/api/v1)."""
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime())
    last_used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())

    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), index=True)
    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class Account(TimestampMixin, Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[AccountKind] = mapped_column(EnumStr(AccountKind, 24), index=True)
    acct_type: Mapped[AccountType] = mapped_column(EnumStr(AccountType, 12))
    label: Mapped[str] = mapped_column(String(120))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    owner_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"), index=True)
    owner_fight_id: Mapped[int | None] = mapped_column(ForeignKey("fights.id", ondelete="RESTRICT"))
    balance_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)
    allow_negative: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint("allow_negative or balance_cents >= 0", name="balance_nonneg"),
    )

    @property
    def is_debit_normal(self) -> bool:
        return AccountType(self.acct_type) in DEBIT_NORMAL


class LedgerTxn(Base):
    """A balanced set of entries. Append-only; corrections are reversals."""
    __tablename__ = "ledger_txns"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    memo: Mapped[str] = mapped_column(String(255), default="")
    ref_type: Mapped[str | None] = mapped_column(String(32), index=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    reverses_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    reversed_by_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(String(255))
    meta: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)

    entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="txn", cascade="all, delete-orphan")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    # Signed, debit-positive. Sum over a txn must be exactly 0.
    amount_cents: Mapped[int] = mapped_column(Money, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(Money, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)

    txn: Mapped[LedgerTxn] = relationship(back_populates="entries")
    account: Mapped[Account] = relationship()

    __table_args__ = (CheckConstraint("amount_cents <> 0", name="nonzero"),)


# ---------------------------------------------------------------------------
# Events / fights
# ---------------------------------------------------------------------------

class Location(TimestampMixin, Base):
    """A registered venue. Fully admin-controlled: name, address, free-form custom fields and the
    defaults that pre-fill a new event held there. Archived locations stay on old events."""
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    address: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(String(500))
    # File name of the venue picture (one of app/static/img/venues/*); it is the background of this
    # venue's event boxes in the Games lobby. NULL = plain box.
    image: Mapped[str | None] = mapped_column(String(80))
    # Admin-defined extras, e.g. [{"label": "Capacity", "value": "800"}, {"label": "Contact", "value": "..."}]
    custom_fields: Mapped[list | None] = mapped_column(JsonType)
    # Defaults offered when creating an event here (NULL = fall back to the global setting).
    default_fight_count: Mapped[int | None] = mapped_column(Integer)
    default_payout_model: Mapped[str | None] = mapped_column(String(8))
    default_house_cut_bps: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint("default_fight_count is null or default_fight_count between 1 and 200", name="loc_fights_range"),
        CheckConstraint("default_house_cut_bps is null or default_house_cut_bps between 0 and 10000", name="loc_cut_range"),
    )

    @property
    def image_url(self) -> str | None:
        return f"/static/img/venues/{self.image}" if self.image else None


class Event(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    event_date: Mapped[dt.date] = mapped_column(Date, index=True)
    fight_count: Mapped[int] = mapped_column(Integer, default=20)
    status: Mapped[EventStatus] = mapped_column(EnumStr(EventStatus, 12), default=EventStatus.DRAFT, index=True)
    payout_model: Mapped[PayoutModel] = mapped_column(EnumStr(PayoutModel, 8), default=PayoutModel.POOL)
    house_cut_bps: Mapped[int | None] = mapped_column(Integer)  # null -> use global setting
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"), index=True)

    location: Mapped["Location | None"] = relationship(foreign_keys=[location_id])
    fights: Mapped[list["Fight"]] = relationship(back_populates="event", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("fight_count between 1 and 200", name="fight_count_range"),
        CheckConstraint("house_cut_bps is null or house_cut_bps between 0 and 10000", name="cut_range"),
    )


class Fight(TimestampMixin, Base):
    __tablename__ = "fights"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    entry_name_meron: Mapped[str] = mapped_column(String(80), default="Meron")
    entry_name_wala: Mapped[str] = mapped_column(String(80), default="Wala")
    status: Mapped[FightStatus] = mapped_column(EnumStr(FightStatus, 12), default=FightStatus.OPEN, index=True)
    result: Mapped[Result | None] = mapped_column(EnumStr(Result, 12))
    fixed_odds_meron_bps: Mapped[int | None] = mapped_column(Integer)  # 19000 = 1.90x
    fixed_odds_wala_bps: Mapped[int | None] = mapped_column(Integer)
    llamado_side: Mapped[str | None] = mapped_column(String(8))     # favourite: meron | wala | null
    bet_mode: Mapped[str] = mapped_column(String(8), default="all", server_default="all", nullable=False)
    settlement_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pool_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    locked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    declared_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    declared_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Picture of the result (stored outside /static, served only to signed-in staff).
    proof_file: Mapped[str | None] = mapped_column(String(120))
    proof_mime: Mapped[str | None] = mapped_column(String(24))
    proof_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    proof_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())

    event: Mapped[Event] = relationship(back_populates="fights")
    proof_by: Mapped["User | None"] = relationship(foreign_keys=[proof_by_id])

    __table_args__ = (
        UniqueConstraint("event_id", "number", name="uq_fight_number"),
        CheckConstraint("number >= 1", name="positive_number"),
    )


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

class Ticket(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"), index=True)
    fight_id: Mapped[int] = mapped_column(ForeignKey("fights.id", ondelete="RESTRICT"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)

    side: Mapped[Side] = mapped_column(EnumStr(Side, 8))
    stake_cents: Mapped[int] = mapped_column(Money)
    funded_by: Mapped[str] = mapped_column(String(8), default="wallet")  # always "wallet"; "cash" only on pre-online rows
    est_payout_cents: Mapped[int] = mapped_column(Money, default=0)
    payout_cents: Mapped[int] = mapped_column(Money, default=0)
    status: Mapped[TicketStatus] = mapped_column(EnumStr(TicketStatus, 12), default=TicketStatus.ACTIVE, index=True)
    is_diy: Mapped[bool] = mapped_column(Boolean, default=False)  # self-service bet
    placed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)
    canceled_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    canceled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    settled_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    settlement_version: Mapped[int | None] = mapped_column(Integer)
    stake_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    payout_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    placed_ip: Mapped[str | None] = mapped_column(String(64))

    # Legacy number-game columns kept for backwards compatibility with betlogs.php
    numbers: Mapped[str | None] = mapped_column(String(32))
    kubrador: Mapped[str | None] = mapped_column(String(80))
    cavo: Mapped[str | None] = mapped_column(String(80))

    fight: Mapped[Fight] = relationship()
    event: Mapped[Event] = relationship()
    player: Mapped[Player | None] = relationship()

    __table_args__ = (
        CheckConstraint("stake_cents > 0", name="stake_positive"),
        CheckConstraint("payout_cents >= 0", name="payout_nonneg"),
        CheckConstraint("side in ('meron','wala','draw')", name="side_valid"),
        CheckConstraint("funded_by in ('wallet','cash')", name="funding_valid"),
        Index("ix_tickets_fight_status", "fight_id", "status"),
        Index("ix_tickets_player_placed", "player_id", "placed_at"),
    )


class SettlementRun(TimestampMixin, Base):
    """Every declare / redeclare / recompute is a versioned, auditable run."""
    __tablename__ = "settlement_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    fight_id: Mapped[int] = mapped_column(ForeignKey("fights.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))  # declare | redeclare | recompute
    result: Mapped[Result] = mapped_column(EnumStr(Result, 12))
    status: Mapped[RunStatus] = mapped_column(EnumStr(RunStatus, 12), default=RunStatus.PENDING, index=True)
    reason: Mapped[str] = mapped_column(String(255))
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    totals: Mapped[dict | None] = mapped_column(JsonType)
    reverses_run_id: Mapped[int | None] = mapped_column(ForeignKey("settlement_runs.id", ondelete="RESTRICT"))
    payout_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    commission_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))

    fight: Mapped[Fight] = relationship()
    __table_args__ = (UniqueConstraint("fight_id", "version", name="uq_run_version"),)


class Commission(TimestampMixin, Base):
    __tablename__ = "commissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="SET NULL"), index=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="RESTRICT"), index=True)
    fight_id: Mapped[int] = mapped_column(ForeignKey("fights.id", ondelete="RESTRICT"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("settlement_runs.id", ondelete="RESTRICT"), index=True)
    tier: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(String(16))
    rate_bps: Mapped[int] = mapped_column(Integer)
    base_cents: Mapped[int] = mapped_column(Money)
    amount_cents: Mapped[int] = mapped_column(Money)
    # Snapshot of the formula inputs/outputs for this line (pool, cuts, chain). Admin-only audit view.
    calc: Mapped[dict | None] = mapped_column(JsonType)
    status: Mapped[CommissionStatus] = mapped_column(EnumStr(CommissionStatus, 12), default=CommissionStatus.UNCLAIMED, index=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    claim_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))

    agent: Mapped[User] = relationship()
    ticket: Mapped[Ticket] = relationship()

    __table_args__ = (
        UniqueConstraint("ticket_id", "agent_id", "run_id", name="uq_commission_per_run"),
        CheckConstraint("amount_cents >= 0", name="amount_nonneg"),
        Index("ix_commissions_agent_status", "agent_id", "status"),
    )


class CashRequest(UUIDMixin, TimestampMixin, Base):
    """GCash load / cash-out requests (cashin_out.php) with a real approval flow."""
    __tablename__ = "cash_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    direction: Mapped[RequestDirection] = mapped_column(EnumStr(RequestDirection, 4), index=True)
    method: Mapped[str] = mapped_column(String(12), default="gcash")
    amount_cents: Mapped[int] = mapped_column(Money)
    # For an agent subject: which balance the request moves. 'commission' (the original behaviour:
    # a commission withdrawal) or 'wallet' (the agent's own credit: load / cash-out from the Wallet).
    target: Mapped[str] = mapped_column(String(12), default="commission", server_default="commission", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[RequestStatus] = mapped_column(EnumStr(RequestStatus, 12), default=RequestStatus.PENDING, index=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    review_note: Mapped[str | None] = mapped_column(String(255))
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)

    player: Mapped[Player | None] = relationship()
    agent: Mapped[User | None] = relationship(foreign_keys=[agent_id])
    requester: Mapped[User] = relationship(foreign_keys=[requested_by_id])

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="positive_amount"),
        CheckConstraint(
            "(player_id is not null and agent_id is null) or (player_id is null and agent_id is not null)",
            name="one_subject",
        ),
    )


class EventExpense(TimestampMixin, Base):
    """Manual per-event expense (e.g. "system fee"). Informational: it feeds the event's
    net income but does not move ledger money."""
    __tablename__ = "event_expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(80))
    amount_cents: Mapped[int] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(String(255))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    event: Mapped[Event] = relationship()
    created_by: Mapped[User | None] = relationship()

    __table_args__ = (CheckConstraint("amount_cents > 0", name="expense_positive"),)


class ChatMessage(Base):
    """Staff chat box (admin <-> master)."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)

    user: Mapped[User] = relationship()


class AuditLog(Base):
    """Append-only. Who did what, to which record, from where, and why."""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    actor_username: Mapped[str | None] = mapped_column(String(32))
    actor_role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(48), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(48))
    before: Mapped[dict | None] = mapped_column(JsonType)
    after: Mapped[dict | None] = mapped_column(JsonType)
    reason: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)


class AppSetting(Base):
    """Every tunable variable lives here and is editable in the admin dashboard."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(12), default="info")
    message: Mapped[str] = mapped_column(String(255))
    link: Mapped[str | None] = mapped_column(String(255))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ReferralCode(Base):
    """One-time sign-up invite. Always single use; expires at ``expires_at``.

    The account created from it gets ``role`` and is attached under ``issuer_id``.
    """
    __tablename__ = "referral_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16))
    issuer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Only for codes the Admin issues: who the new account reports to (Cavo -> a Master,
    # Kubrador -> a Cavo, Player -> a Kubrador). Empty = directly under the issuer.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    used_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())

    issuer: Mapped["User"] = relationship(foreign_keys=[issuer_id])
    used_by: Mapped["User | None"] = relationship(foreign_keys=[used_by_id])
    parent: Mapped["User | None"] = relationship(foreign_keys=[parent_id])

    def status(self, now: dt.datetime | None = None) -> str:
        now = now or utcnow()
        if self.used_at:
            return "used"
        if self.revoked_at:
            return "revoked"
        return "expired" if self.expires_at <= now else "active"


__all__ = [n for n in dir() if isinstance(getattr(n, "__mro__", None), tuple)]
