"""Players: cashless tap-card holders and online (GCash) accounts.

Fixes vs. the original clp.php / cashin_out.php:
* card numbers are unique and validated, PII is encrypted at rest;
* age verification and responsible-gaming limits are enforced server-side;
* loading and cashing out go through the approved GCash/bank request flow only
  (no counter cash);
* every operation is idempotent, so a retried mobile request cannot double-load.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import assert_player_in_scope, has_global_scope, is_top, scope_ids
from app.core.security import USERNAME_RE, hash_password, mask, password_problems, pii
from app.core.settings_registry import SettingsStore
from app.db.models import (
    Account,
    BolaTicket,
    Player,
    PlayerKind,
    Status,
    Ticket,
    TicketStatus,
    User,
)
from app.services import accounts, ledger

CARD_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")
GCASH_RE = re.compile(r"^09\d{9}$")


def _age(birth: dt.date, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _check_age(birth: dt.date | None, settings: SettingsStore) -> None:
    minimum = int(settings.get("player.min_age"))
    if birth is None:
        raise ValidationError("Date of birth is required for age verification.")
    if birth > dt.date.today():
        raise ValidationError("Date of birth cannot be in the future.")
    if _age(birth) < minimum:
        raise ValidationError(f"Players must be at least {minimum} years old.")


def register_card_player(
    db: Session, actor: User, *, card_number: str, full_name: str, birth_date: dt.date,
    settings: SettingsStore, address: str | None = None, gender: str | None = None,
    agent_id: int | None = None, daily_limit_cents: int | None = None,
) -> Player:
    card_number = card_number.strip()
    full_name = full_name.strip()
    if not CARD_RE.match(card_number):
        raise ValidationError("Card number must be 4-64 letters, digits or dashes.")
    if not full_name or len(full_name) > 120:
        raise ValidationError("Full name is required (max 120 characters).")
    _check_age(birth_date, settings)
    if db.scalar(select(Player.id).where(Player.card_number == card_number)):
        raise Conflict("That card is already registered.")

    owner = agent_id or actor.id
    player = Player(
        kind=PlayerKind.CARD, card_number=card_number, full_name=full_name,
        birth_date=birth_date, gender=(gender or None), address_enc=pii.encrypt(address),
        agent_id=owner, status=Status.ACTIVE, created_by_id=actor.id,
        age_verified_at=dt.datetime.now(dt.timezone.utc),
        daily_bet_limit_cents=daily_limit_cents,
    )
    db.add(player)
    db.flush()
    accounts.wallet_for_player(db, player)
    audit.record(db, actor, "player.register_card", entity_type="player", entity_id=player.id,
                 after={"card_number": card_number, "full_name": full_name, "agent_id": owner})
    return player


def register_online_player(
    db: Session, actor: User, *, username: str, password: str, full_name: str,
    birth_date: dt.date, gcash_number: str, settings: SettingsStore,
    address: str | None = None, agent_id: int | None = None,
) -> Player:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValidationError("Username must be 3-32 characters: letters, digits, dot, dash, underscore.")
    if db.scalar(select(Player.id).where(func.lower(Player.username) == username.lower())):
        raise Conflict("That username is already taken.")
    gcash_number = gcash_number.strip()
    if not GCASH_RE.match(gcash_number):
        raise ValidationError("GCash number must be 11 digits starting with 09.")
    _check_age(birth_date, settings)
    problems = password_problems(password, username)
    if problems:
        raise ValidationError(" ".join(problems))

    player = Player(
        kind=PlayerKind.ONLINE, username=username, password_hash=hash_password(password),
        full_name=full_name.strip()[:120], birth_date=birth_date,
        gcash_enc=pii.encrypt(gcash_number), address_enc=pii.encrypt(address),
        agent_id=agent_id or actor.id, status=Status.ACTIVE, created_by_id=actor.id,
        age_verified_at=dt.datetime.now(dt.timezone.utc),
        kyc_verified=not bool(settings.get("player.require_kyc_for_online")),
    )
    db.add(player)
    db.flush()
    accounts.wallet_for_player(db, player)
    audit.record(db, actor, "player.register_online", entity_type="player", entity_id=player.id,
                 after={"username": username, "full_name": full_name, "gcash": mask(gcash_number)})
    return player


def get_player(db: Session, actor: User, player_id: int) -> Player:
    player = db.get(Player, player_id)
    if player is None:
        raise NotFound("Player not found.")
    return assert_player_in_scope(db, actor, player)


def find_by_card(db: Session, card_number: str) -> Player:
    player = db.scalar(select(Player).where(Player.card_number == card_number.strip()))
    if player is None:
        raise NotFound("No player is registered to that card.")
    return player


def list_players(db: Session, actor: User, *, kind: PlayerKind | None = None,
                 search: str | None = None, limit: int = 200, offset: int = 0) -> list[Player]:
    stmt = select(Player)
    if not has_global_scope(actor):
        stmt = stmt.where(Player.agent_id.in_(scope_ids(db, actor)))
    if kind:
        stmt = stmt.where(Player.kind == kind)
    if search:
        term = f"%{search.strip()[:60]}%"
        stmt = stmt.where(or_(Player.full_name.ilike(term), Player.card_number.ilike(term),
                              Player.username.ilike(term)))
    stmt = stmt.order_by(Player.id.desc()).limit(clamp_limit(limit, 500)).offset(clamp_offset(offset))
    return list(db.scalars(stmt).all())


def wallet(db: Session, player: Player) -> Account:
    return accounts.wallet_for_player(db, player)


def staked_today(db: Session, player: Player) -> int:
    """Today's stakes across **both** games, so the daily limit cannot be dodged by switching
    from sabong to the number game (the legacy panel had no limit at all)."""
    start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    live = [TicketStatus.ACTIVE, TicketStatus.SETTLED]
    sabong = int(db.scalar(
        select(func.coalesce(func.sum(Ticket.stake_cents), 0))
        .where(Ticket.player_id == player.id, Ticket.placed_at >= start,
               Ticket.status.in_(live))
    ) or 0)
    bola = int(db.scalar(
        select(func.coalesce(func.sum(BolaTicket.stake_cents), 0))
        .where(BolaTicket.player_id == player.id, BolaTicket.placed_at >= start,
               BolaTicket.status.in_(live))
    ) or 0)
    return sabong + bola


def assert_can_bet(db: Session, player: Player, stake_cents: int, settings: SettingsStore) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    if player.status != Status.ACTIVE:
        raise Forbidden("This player account is not active.")
    if player.self_excluded_until and player.self_excluded_until > now:
        raise Forbidden("This player is self-excluded and cannot bet right now.")
    if player.kind == PlayerKind.ONLINE and settings.get("player.require_kyc_for_online") and not player.kyc_verified:
        raise Forbidden("This player has not completed KYC verification.")
    limit = player.daily_bet_limit_cents
    if limit is None:
        default_limit = int(settings.get("player.daily_bet_limit_cents"))
        limit = default_limit or None
    if limit is not None and staked_today(db, player) + stake_cents > limit:
        from app.core.money import fmt
        raise Forbidden(f"Daily betting limit of {fmt(limit)} would be exceeded.")


def set_self_exclusion(db: Session, actor: User, player_id: int, days: int, settings: SettingsStore) -> Player:
    player = get_player(db, actor, player_id)
    days = max(1, min(int(days), 3650))
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    before = {"self_excluded_until": player.self_excluded_until}
    player.self_excluded_until = until
    db.flush()
    audit.record(db, actor, "player.self_exclusion", entity_type="player", entity_id=player.id,
                 before=before, after={"self_excluded_until": until})
    return player


def update_player(db: Session, actor: User, player_id: int, *, status: Status | None = None,
                  kyc_verified: bool | None = None, daily_limit_cents: int | None = None,
                  reason: str | None = None) -> Player:
    player = get_player(db, actor, player_id)
    before = {"status": player.status, "kyc_verified": player.kyc_verified,
              "daily_bet_limit_cents": player.daily_bet_limit_cents}
    if status is not None:
        player.status = status
    if kyc_verified is not None:
        if not is_top(actor):
            raise Forbidden("Only Master or Admin can change KYC status.")
        player.kyc_verified = kyc_verified
    if daily_limit_cents is not None:
        player.daily_bet_limit_cents = max(0, daily_limit_cents) or None
    db.flush()
    audit.record(db, actor, "player.update", entity_type="player", entity_id=player.id,
                 before=before, after={"status": player.status, "kyc_verified": player.kyc_verified,
                                       "daily_bet_limit_cents": player.daily_bet_limit_cents}, reason=reason)
    return player


def decrypt_field(value: str | None) -> str | None:
    return pii.decrypt(value)
