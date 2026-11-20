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
    Time,
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
    master     Top of the visible network ("Management" in the old reports). Shown as "Master Agent".
               Keeps the remainder of every commission pool (default 70%).
    agent      Invited by a Master Agent. Earns the Volume % (default 30% of the pool). Recruits
               Sub Agents and players. (Replaces the old Cavo.)
    sub_agent  Invited by an Agent. Earns the SOP % taken out of its Agent's cut (default 30% of the
               Agent's 30%). Recruits players. (Replaces the old Kubrador.)
    cavo       RETIRED. Merged into agent by the migration; kept only so old rows still parse.
    kubrador   RETIRED. Merged into sub_agent by the migration; kept only so old rows still parse.
    teller     Wallet desk. Admin creates it, directly under the Admin. Approves or rejects load / cash-out
               requests and sees the Users list; nothing else. No balance: it moves money through its own
               GCash account, so a GCash number is mandatory. (Legacy teller rows without one stay closed.)
    declarator Declares results only; never sees totals (was hard-coded id 78). No balance,
               cannot bet or move money: a spectator/helper for the event.
    pawn       Created by the Admin only. A spectator like the declarator (no balance, no betting)
               whose reach into the Admin Panel is set per account by the Admin (``User.grants``).

    Normal accounts (players) are not users: an Agent's or Sub Agent's referral code creates a Player.
    """
    ADMIN = "admin"      # legacy UTYPE '0'
    MASTER = "master"
    AGENT = "agent"
    SUB_AGENT = "sub_agent"
    CAVO = "cavo"
    KUBRADOR = "kubrador"
    TELLER = "teller"
    DECLARATOR = "declarator"
    PAWN = "pawn"

    @property
    def label(self) -> str:
        return {
            "admin": "Admin", "master": "Master Agent", "agent": "Agent", "sub_agent": "Sub Agent",
            "cavo": "Cavo", "kubrador": "Kubrador",
            "teller": "Teller", "declarator": "Declarator", "pawn": "Pawn",
        }[self.value]


AGENT_ROLES = (Role.ADMIN, Role.MASTER, Role.AGENT, Role.SUB_AGENT)
# Every visible account that sells for players and earns commission (no Admin: it is hidden).
NETWORK_ROLES = (Role.MASTER, Role.AGENT, Role.SUB_AGENT)
# Retired tiers: still valid stored values (old rows, old ledger accounts) but nobody can be given them.
RETIRED_ROLES = (Role.CAVO, Role.KUBRADOR)
# Spectator / helper accounts: no balance, no bets, no money movement, nobody's upline.
NO_WALLET_ROLES = (Role.DECLARATOR, Role.PAWN, Role.TELLER)
TOP_ROLES = (Role.ADMIN, Role.MASTER)          # the operator tier: see everything visible

# The tree: Master Agent -> Agent -> Sub Agent -> Player. A role's children are the roles it may
# recruit, the first one being what its referral code makes by default. An Agent may also recruit
# players directly (it then keeps the whole Agent cut, there being no Sub Agent to share with).
# A player is not a user row (separate table).
CHILD_ROLES: dict[Role, tuple[Role, ...]] = {
    Role.ADMIN: (Role.MASTER,),
    Role.MASTER: (Role.AGENT,),
    Role.AGENT: (Role.SUB_AGENT,),
    Role.SUB_AGENT: (),
    Role.CAVO: (),
    Role.KUBRADOR: (),
    Role.TELLER: (),
    Role.DECLARATOR: (),
    Role.PAWN: (),
}
# Referral codes: what each role may put on a code ("player" is not a user role). First = default.
INVITE_ROLES: dict[Role, tuple[str, ...]] = {
    Role.ADMIN: ("master", "agent", "sub_agent", "player"),
    Role.MASTER: ("agent",),
    Role.AGENT: ("sub_agent", "player"),
    Role.SUB_AGENT: ("player",),
    Role.CAVO: (),
    Role.KUBRADOR: (),
    Role.TELLER: (),
    Role.DECLARATOR: (),
    Role.PAWN: (),
}
# Who an account of this role may report to (Admin / Pawn creation picks one). First = the usual
# upline, the only one auto-picked when exactly one such account exists.
UPLINE_ROLES: dict[str, tuple[Role, ...]] = {
    "agent": (Role.MASTER,),
    "sub_agent": (Role.AGENT,),
    "player": (Role.SUB_AGENT, Role.AGENT),
}
# Kept for older callers: the first (default) child / invite role of each role.
CHILD_ROLE: dict[Role, Role | None] = {r: (c[0] if c else None) for r, c in CHILD_ROLES.items()}
INVITE_ROLE: dict[Role, str | None] = {r: (c[0] if c else None) for r, c in INVITE_ROLES.items()}
# Agent and Sub Agent hold a cut. The Master Agent keeps what is left of the pool.
CUT_ROLES = (Role.AGENT, Role.SUB_AGENT)


class Status(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class AccountKind(StrEnum):
    WALLET = "wallet"                       # agent / player spendable balance
    COMMISSION_PAYABLE = "commission_payable"
    STATION_CASH = "station_cash"           # RETIRED: historical ledger accounts only
    FIGHT_POOL = "fight_pool"               # escrow holding stakes of one fight
    BOLA_POOL = "bola_pool"                 # escrow holding stakes of one number-game draw
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
    AccountKind.BOLA_POOL: AccountType.LIABILITY,
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
    #   agent:     cut_bps = Volume %  (share of the commission pool the Agent's network keeps)
    #              sop_bps = SOP %     (share of that cut passed to each of its Sub Agents)
    #   sub_agent: cut_bps = personal SOP % override (NULL = use the Agent's sop_bps)
    cut_bps: Mapped[int | None] = mapped_column(Integer)
    sop_bps: Mapped[int | None] = mapped_column(Integer)

    # Teller only: the GCash number (encrypted at rest) it sends and receives money with.
    gcash_enc: Mapped[str | None] = mapped_column(Text)

    # Pawn only: the list of grant keys the Admin switched on (see app.core.rbac.GRANTS).
    grants: Mapped[list | None] = mapped_column(JsonType)

    parent: Mapped["User | None"] = relationship(
        remote_side="User.id", back_populates="children", foreign_keys="User.parent_id")
    children: Mapped[list["User"]] = relationship(
        back_populates="parent", foreign_keys="User.parent_id")

    __table_args__ = (
        CheckConstraint("role in ('admin','master','cavo','kubrador','teller','declarator','pawn','agent','sub_agent')", name="role_valid"),
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

class IpBan(TimestampMixin, Base):
    """A blocked address (or CIDR range). Requests from it get a 403 before anything else runs.
    Rows are deleted on unban; the audit trail keeps who banned what and why."""
    __tablename__ = "ip_bans"

    id: Mapped[int] = mapped_column(primary_key=True)
    network: Mapped[str] = mapped_column(String(64), unique=True, index=True)   # "203.0.113.9" or "203.0.113.0/24"
    reason: Mapped[str] = mapped_column(String(255))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())       # NULL = until lifted


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
    owner_draw_id: Mapped[int | None] = mapped_column(ForeignKey("bola_draws.id", ondelete="RESTRICT"))
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

    __table_args__ = (
        CheckConstraint("amount_cents <> 0", name="nonzero"),
        # wallet history: WHERE account_id = ? ORDER BY id DESC LIMIT n reads the index in order, no sort
        Index("ix_ledger_entries_account_id_id", "account_id", "id"),
    )


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
    # Bola caps for this area. NULL = follow the global bola.* setting, 0 = no cap here.
    bola_limit_per_line_cents: Mapped[int | None] = mapped_column(Money, nullable=True)
    bola_max_liability_per_outcome_cents: Mapped[int | None] = mapped_column(Money, nullable=True)
    bola_max_liability_per_draw_cents: Mapped[int | None] = mapped_column(Money, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint("default_fight_count is null or default_fight_count between 1 and 200", name="loc_fights_range"),
        CheckConstraint("default_house_cut_bps is null or default_house_cut_bps between 0 and 10000", name="loc_cut_range"),
    )

    @property
    def image_url(self) -> str | None:
        return f"/static/img/venues/{self.image}" if self.image else None


class Team(TimestampMixin, Base):
    """A team in a sports catalog (the NBA first). Registry only: no game, odds or bet points at a team yet,
    so registering or switching one off changes nothing that already exists. Rows are seeded by
    services/teams.py and never overwritten afterwards, so an Admin's on/off choice survives a restart."""
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(16), index=True)          # "nba"
    code: Mapped[str] = mapped_column(String(8))                        # abbreviation, e.g. LAL
    ext_id: Mapped[int | None] = mapped_column(BigInteger)              # the league's own team id (NBA: 1610612747)
    city: Mapped[str] = mapped_column(String(40))                       # "Los Angeles"
    name: Mapped[str] = mapped_column(String(40))                       # "Lakers"
    conference: Mapped[str | None] = mapped_column(String(16))          # "West"
    division: Mapped[str | None] = mapped_column(String(24))            # "Pacific"
    logo: Mapped[str | None] = mapped_column(String(40))                # file in app/static/img/teams/<sport>/
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("sport", "code", name="uq_team_sport_code"),)

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.name}"

    @property
    def logo_url(self) -> str | None:
        return f"/static/img/teams/{self.sport}/{self.logo}" if self.logo else None


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
    # When the livestream went live. Empty = pre-show: early bets may be placed from the livestream page.
    live_started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())

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
    # The pooled GCash number this request was routed to (picked automatically by services.gcash).
    # NULL = bank request, or no pool configured yet (the legacy behaviour).
    gcash_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gcash_accounts.id", ondelete="SET NULL"), index=True)

    player: Mapped[Player | None] = relationship()
    agent: Mapped[User | None] = relationship(foreign_keys=[agent_id])
    requester: Mapped[User] = relationship(foreign_keys=[requested_by_id])
    gcash_account: Mapped["GcashAccount | None"] = relationship(foreign_keys=[gcash_account_id])

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="positive_amount"),
        CheckConstraint(
            "(player_id is not null and agent_id is null) or (player_id is null and agent_id is not null)",
            name="one_subject",
        ),
    )


class GcashAccount(TimestampMixin, Base):
    """One GCash number in the teller pool.

    A teller may register as many as they like. The cash desk routes each new GCash request to
    whichever number still has room under its daily / monthly limit (see ``services.gcash``), so
    nobody has to juggle numbers by hand. Usage is never stored: it is always summed from the
    requests routed to the number (plus manual ``GcashAdjustment`` rows), so it cannot drift.
    """
    __tablename__ = "gcash_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    label: Mapped[str] = mapped_column(String(60))
    number_enc: Mapped[str] = mapped_column(Text)                       # encrypted at rest
    number_hash: Mapped[str] = mapped_column(String(64), unique=True)   # duplicate guard (SHA-256)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), index=True)
    # NULL = follow the global limit (Admin Panel -> Variables -> GCash).
    daily_limit_cents: Mapped[int | None] = mapped_column(Money)
    monthly_limit_cents: Mapped[int | None] = mapped_column(Money)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    note: Mapped[str | None] = mapped_column(String(255))

    owner: Mapped["User | None"] = relationship(foreign_keys=[owner_id])


class GcashAdjustment(TimestampMixin, Base):
    """Usage that happened outside the system (the teller used the number personally, a transfer
    made by hand). It counts toward the day / month exactly like a routed request."""
    __tablename__ = "gcash_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("gcash_accounts.id", ondelete="CASCADE"), index=True)
    amount_cents: Mapped[int] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(String(255))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (CheckConstraint("amount_cents > 0", name="positive_amount"),)


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
    """Livestream chat (experimental) message. Stored and signed, never anonymous."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(String(300))
    # NULL = the staff operations room (/chat). Set = that game's livestream chat in the fight view.
    fight_id: Mapped[int | None] = mapped_column(ForeignKey("fights.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)

    user: Mapped[User] = relationship()


class ChatPass(Base):
    """Battle Pass: unlocks livestream chat on every game for a fixed window (30 days by default).
    One row per purchase; buying again while a pass is running extends it from its end."""
    __tablename__ = "chat_passes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), index=True)
    price_cents: Mapped[int] = mapped_column(Money)
    txn_uuid: Mapped[str | None] = mapped_column(String(32))
    # "battle" = Battle Pass, "lux" = Lux VIP Battle Pass (same perks, flashy highlighted chat)
    tier: Mapped[str] = mapped_column(String(10), default="battle", server_default="battle")
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)

    user: Mapped[User] = relationship()


class ChatToken(Base):
    """Consumable chat ticket. One token unlocks chat for ONE game; once spent it is tied to that game."""
    __tablename__ = "chat_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    price_cents: Mapped[int] = mapped_column(Money)
    txn_uuid: Mapped[str | None] = mapped_column(String(32))
    used_fight_id: Mapped[int | None] = mapped_column(ForeignKey("fights.id", ondelete="SET NULL"), index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)

    user: Mapped[User] = relationship()


class AliasToken(Base):
    """Consumable: one-time alias change. Spending it sets the chat alias, then it is gone."""
    __tablename__ = "alias_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    price_cents: Mapped[int] = mapped_column(Money)
    txn_uuid: Mapped[str | None] = mapped_column(String(32))
    used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)

    user: Mapped[User] = relationship()


class ChatAlias(Base):
    """The name shown in livestream chat instead of the username. One per user; the account behind
    it stays on record (staff can see it). ``alias_key`` is the lowercase, punctuation-free form used
    to keep aliases unique and to stop look-alikes of existing names."""
    __tablename__ = "chat_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    alias: Mapped[str] = mapped_column(String(24))
    alias_key: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow)

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


# ---------------------------------------------------------------------------
# Bola: the two-number draw game (Tumbok-Sahod), ported from the legacy panel
# ---------------------------------------------------------------------------
# A parallel module, deliberately NOT bolted onto Event / Fight / Ticket:
#   * tickets.side has CHECK (meron|wala|draw) and fight_id is NOT NULL;
#   * commissions.ticket_id / fight_id / run_id are all NOT NULL FKs;
#   * SQLite cannot relax a NOT NULL or a CHECK without a hand-written table rebuild.
# New tables need no migration at all (Base.metadata.create_all runs on every start), so the
# sabong schema, its data and its tests are untouched. Everything shared -- wallet, ledger,
# agent tree, audit, settings, RBAC, player safeguards -- is reused as-is.


class BolaKind(StrEnum):
    """Ticket type = how many pair lines the ticket expands into (old tbl_bet.type)."""
    PAIR = "pair"          # 2 numbers -> 1 line   (old type '2', button "Tumbok Sahod")
    CASAS_3 = "casas3"     # 3 numbers -> 3 lines  (old type '3')
    CASAS_6 = "casas6"     # 4 numbers -> 6 lines  (old type '6')
    CASAS_10 = "casas10"   # 5 numbers -> 10 lines (old type '10')

    @property
    def label(self) -> str:
        return {"pair": "Tumbok–Sahod", "casas3": "3 Casas", "casas6": "6 Casas",
                "casas10": "10 Casas"}[self.value]


# How many numbers each kind takes. Lines are C(n, 2): 1, 3, 6, 10.
KIND_NUMBERS: dict[BolaKind, int] = {
    BolaKind.PAIR: 2, BolaKind.CASAS_3: 3, BolaKind.CASAS_6: 4, BolaKind.CASAS_10: 5,
}
KIND_LINES: dict[BolaKind, int] = {k: n * (n - 1) // 2 for k, n in KIND_NUMBERS.items()}
# Legacy DB type code -> kind, for the optional archive import.
LEGACY_TYPE_KIND: dict[str, BolaKind] = {
    "2": BolaKind.PAIR, "3": BolaKind.CASAS_3, "6": BolaKind.CASAS_6, "10": BolaKind.CASAS_10,
}
BOLA_MAX_BALL = 90   # hard ceiling of the CHECK; the working maximum is bola.max_number (40)


class DrawStatus(StrEnum):
    SCHEDULED = "scheduled"   # exists, not yet open for bets
    OPEN = "open"
    LOCKED = "locked"         # past cut-off or closed by hand
    SETTLED = "settled"
    VOID = "void"             # called off, every stake refunded


class BolaSchedule(TimestampMixin, Base):
    """One draw time of one area (old tbl_area.ball_1 / ball_2 / ball_3).

    ``draw_time`` is **Asia/Manila local wall time**; a draw's actual instant is stored in UTC on
    BolaDraw.scheduled_at. 00:00 in the legacy dump meant "no draw" -- those rows are simply not
    created (or left inactive).
    """
    __tablename__ = "bola_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"), index=True)
    slot: Mapped[int] = mapped_column(Integer)
    draw_time: Mapped[dt.time] = mapped_column(Time)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)

    location: Mapped["Location"] = relationship()

    __table_args__ = (
        UniqueConstraint("location_id", "slot", name="uq_bola_schedule_slot"),
        CheckConstraint("slot between 1 and 6", name="bola_slot_range"),
    )


class BolaDraw(UUIDMixin, TimestampMixin, Base):
    """One draw = one area x date x slot (old tbl_event, but scheduled instead of hand-typed)."""
    __tablename__ = "bola_draws"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), index=True)
    draw_date: Mapped[dt.date] = mapped_column(Date, index=True)
    slot: Mapped[int] = mapped_column(Integer, default=1)
    scheduled_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), index=True)
    cutoff_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), index=True)
    status: Mapped[DrawStatus] = mapped_column(EnumStr(DrawStatus, 12), default=DrawStatus.SCHEDULED, index=True)
    ball_first: Mapped[int | None] = mapped_column(Integer)
    ball_second: Mapped[int | None] = mapped_column(Integer)
    settlement_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pool_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    locked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    declared_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    declared_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Same four attributes as Fight, so services.proofs works for a draw unchanged.
    proof_file: Mapped[str | None] = mapped_column(String(120))
    proof_mime: Mapped[str | None] = mapped_column(String(40))
    proof_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    proof_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    note: Mapped[str | None] = mapped_column(String(120))
    legacy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    location: Mapped["Location"] = relationship()
    declared_by: Mapped["User | None"] = relationship(foreign_keys=[declared_by_id])
    proof_by: Mapped["User | None"] = relationship(foreign_keys=[proof_by_id])

    __table_args__ = (
        UniqueConstraint("location_id", "draw_date", "slot", name="uq_bola_draw_slot"),
        CheckConstraint("slot between 1 and 6", name="bola_draw_slot_range"),
        CheckConstraint(f"ball_first is null or ball_first between 1 and {BOLA_MAX_BALL}", name="bola_first_range"),
        CheckConstraint(f"ball_second is null or ball_second between 1 and {BOLA_MAX_BALL}", name="bola_second_range"),
        Index("ix_bola_draws_date_status", "draw_date", "status"),
    )

    @property
    def title(self) -> str:
        return f"{self.location.name if self.location else 'Area'} · slot {self.slot}"

    @property
    def result_label(self) -> str | None:
        if self.ball_first is None or self.ball_second is None:
            return None
        return f"{self.ball_first} / {self.ball_second}"


class BolaTicket(UUIDMixin, TimestampMixin, Base):
    """One sold ticket (old tbl_bet). Always belongs to a verified player: no walk-ins."""
    __tablename__ = "bola_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    draw_id: Mapped[int] = mapped_column(ForeignKey("bola_draws.id", ondelete="RESTRICT"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[BolaKind] = mapped_column(EnumStr(BolaKind, 12), index=True)
    numbers: Mapped[list] = mapped_column(JsonType)          # in the order typed
    tumbok_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)   # per line
    sahod_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)    # per line
    line_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stake_cents: Mapped[int] = mapped_column(Money, nullable=False)               # (t+s) * lines
    status: Mapped[TicketStatus] = mapped_column(EnumStr(TicketStatus, 12), default=TicketStatus.ACTIVE, index=True)
    payout_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)
    est_max_payout_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)
    stake_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    payout_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    settlement_version: Mapped[int | None] = mapped_column(Integer)
    settled_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    canceled_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    canceled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    placed_ip: Mapped[str | None] = mapped_column(String(64))
    placed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)
    # Snapshot of the selling network, so the bet log stays correct if the tree changes later.
    kubrador: Mapped[str | None] = mapped_column(String(32))
    cavo: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(String(120))
    legacy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    draw: Mapped[BolaDraw] = relationship()
    player: Mapped["Player"] = relationship()
    agent: Mapped["User | None"] = relationship(foreign_keys=[agent_id])
    lines: Mapped[list["BolaLine"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="BolaLine.id")

    __table_args__ = (
        CheckConstraint("stake_cents > 0", name="bola_stake_positive"),
        CheckConstraint("tumbok_cents >= 0 and sahod_cents >= 0", name="bola_sides_nonneg"),
        CheckConstraint("tumbok_cents + sahod_cents > 0", name="bola_one_side_staked"),
        CheckConstraint("line_count between 1 and 10", name="bola_line_count_range"),
        Index("ix_bola_tickets_draw_status", "draw_id", "status"),
    )

    @property
    def numbers_label(self) -> str:
        return ", ".join(str(n) for n in (self.numbers or []))

    @property
    def per_line_cents(self) -> int:
        return self.tumbok_cents + self.sahod_cents


class BolaLine(Base):
    """One ordered pair of a ticket (old tbl_bet_combination)."""
    __tablename__ = "bola_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("bola_tickets.id", ondelete="CASCADE"), index=True)
    draw_id: Mapped[int] = mapped_column(ForeignKey("bola_draws.id", ondelete="RESTRICT"), index=True)
    first: Mapped[int] = mapped_column(Integer)
    second: Mapped[int] = mapped_column(Integer)
    tumbok_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)
    sahod_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)

    ticket: Mapped[BolaTicket] = relationship(back_populates="lines")

    __table_args__ = (
        CheckConstraint(f"first between 1 and {BOLA_MAX_BALL} and second between 1 and {BOLA_MAX_BALL}",
                        name="bola_line_range"),
        Index("ix_bola_lines_pair", "draw_id", "first", "second"),
    )

    @property
    def amount_cents(self) -> int:
        return self.tumbok_cents + self.sahod_cents


class BolaExposure(Base):
    """Running stake total per ordered pair of one draw, maintained inside the bet transaction.

    At most max_number * (max_number - 1) rows (1,560 at 40 numbers). This is what makes the
    per-combination limit and the liability cap actually work -- the legacy checker read
    undefined variables and always computed zero.
    """
    __tablename__ = "bola_exposure"

    draw_id: Mapped[int] = mapped_column(ForeignKey("bola_draws.id", ondelete="CASCADE"), primary_key=True)
    first: Mapped[int] = mapped_column(Integer, primary_key=True)
    second: Mapped[int] = mapped_column(Integer, primary_key=True)
    tumbok_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)
    sahod_cents: Mapped[int] = mapped_column(Money, default=0, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint("tumbok_cents >= 0 and sahod_cents >= 0 and line_count >= 0",
                        name="bola_exposure_nonneg"),
    )

    @property
    def amount_cents(self) -> int:
        return self.tumbok_cents + self.sahod_cents


class BolaRun(TimestampMixin, Base):
    """Versioned settlement run: declare / redeclare / recompute / void (same pattern as fights)."""
    __tablename__ = "bola_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    draw_id: Mapped[int] = mapped_column(ForeignKey("bola_draws.id", ondelete="RESTRICT"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(12))
    ball_first: Mapped[int | None] = mapped_column(Integer)
    ball_second: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[RunStatus] = mapped_column(EnumStr(RunStatus, 12), default=RunStatus.PENDING, index=True)
    reason: Mapped[str] = mapped_column(String(255))
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    totals: Mapped[dict | None] = mapped_column(JsonType)
    reverses_run_id: Mapped[int | None] = mapped_column(ForeignKey("bola_runs.id", ondelete="RESTRICT"))
    payout_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))
    commission_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))

    draw: Mapped[BolaDraw] = relationship()
    requester: Mapped["User"] = relationship(foreign_keys=[requested_by_id])

    __table_args__ = (UniqueConstraint("draw_id", "version", name="uq_bola_run_version"),)


class BolaCommission(TimestampMixin, Base):
    """Mirror of Commission with its own FKs (commissions.ticket_id/fight_id/run_id are NOT NULL)."""
    __tablename__ = "bola_commissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="SET NULL"), index=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("bola_tickets.id", ondelete="RESTRICT"), index=True)
    draw_id: Mapped[int] = mapped_column(ForeignKey("bola_draws.id", ondelete="RESTRICT"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("bola_runs.id", ondelete="RESTRICT"), index=True)
    tier: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(String(16))
    rate_bps: Mapped[int] = mapped_column(Integer)
    base_cents: Mapped[int] = mapped_column(Money)
    amount_cents: Mapped[int] = mapped_column(Money)
    calc: Mapped[dict | None] = mapped_column(JsonType)
    status: Mapped[CommissionStatus] = mapped_column(EnumStr(CommissionStatus, 12),
                                                   default=CommissionStatus.UNCLAIMED, index=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime())
    claim_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_txns.id", ondelete="RESTRICT"))

    agent: Mapped["User"] = relationship()
    ticket: Mapped[BolaTicket] = relationship()
    draw: Mapped[BolaDraw] = relationship()

    __table_args__ = (
        UniqueConstraint("ticket_id", "agent_id", "run_id", name="uq_bola_commission_per_run"),
        CheckConstraint("amount_cents >= 0", name="bola_commission_nonneg"),
        Index("ix_bola_commissions_agent_status", "agent_id", "status"),
    )


class BolaExpense(TimestampMixin, Base):
    """Manual per-draw expense. Informational: feeds the draw's net income, moves no ledger money."""
    __tablename__ = "bola_expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    draw_id: Mapped[int] = mapped_column(ForeignKey("bola_draws.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(80))
    amount_cents: Mapped[int] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(String(255))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    draw: Mapped[BolaDraw] = relationship()

    __table_args__ = (CheckConstraint("amount_cents > 0", name="bola_expense_positive"),)


__all__ = [n for n in dir() if isinstance(getattr(n, "__mro__", None), tuple)]
