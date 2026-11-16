"""One-time referral codes: the only way to self-register an account.

* A code is always single-use and has an expiry timer chosen by its issuer.
* The role of the invitee is decided by who issued the code, never by the person redeeming it:
      Master's code -> Cavo      Cavo's code -> Kubrador      Kubrador's code -> normal (player) account
  The new account is attached directly under the issuer.
* The Admin is the exception: an Admin code can make a Master, Cavo, Kubrador or Player, and for
  the last three the Admin picks who the new account reports to (stored on the code).
* Redemption claims the code with a conditional UPDATE, so two simultaneous
  sign-ups can never both succeed.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Forbidden, NotFound, StelError, ValidationError
from app.core.rbac import scope_ids
from app.core.security import random_code
from app.core.settings_registry import SettingsStore
from app.db.models import INVITE_ROLE, Player, ReferralCode, Role, User
from app.services import agents, players

MAX_TTL_MINUTES = 43200  # 30 days
_BAD_CODE = "That referral code is invalid, already used, or expired."


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _norm(code: str) -> str:
    return "".join((code or "").split()).upper()[:16]


def invite_role(actor: User) -> str | None:
    """'cavo' for a Master, 'kubrador' for a Cavo, 'player' for a Kubrador, 'master' for Admin."""
    return INVITE_ROLE.get(Role(actor.role))


# What the Admin may choose when issuing a code (Declarator and Pawn are created directly).
ADMIN_INVITE_ROLES = ("master", "cavo", "kubrador", "player")
_UPLINE_OF = {"cavo": Role.MASTER, "kubrador": Role.CAVO, "player": Role.KUBRADOR}


def invite_roles(actor: User) -> list[str]:
    """Every role this account may put on a code (the Admin has four, everyone else exactly one)."""
    if actor.status != "active":
        return []
    if actor.role == Role.ADMIN:
        return list(ADMIN_INVITE_ROLES)
    own = invite_role(actor)
    return [own] if own else []


def role_label(role: str) -> str:
    return "Player" if role == "player" else Role(role).label


def _pick_upline(db: Session, role: str, parent_id: int | None) -> int | None:
    """Admin codes only: the active account the new Cavo / Kubrador / Player will report to."""
    need = _UPLINE_OF.get(role)
    if need is None:                      # master: sits directly under the Admin
        return None
    if parent_id:
        parent = db.get(User, parent_id)
    else:
        found = db.scalars(select(User).where(User.role == need, User.status == "active")
                           .order_by(User.id).limit(2)).all()
        parent = found[0] if len(found) == 1 else None
    if parent is None or parent.role != need or parent.status != "active":
        if role == "player" and not parent_id:
            return None                   # a player with no Kubrador named sits under the Admin
        raise ValidationError(f"Choose the active {need.label} this {role_label(role)} reports to.")
    return parent.id


def create_code(db: Session, actor: User, *, ttl_minutes: int, note: str | None = None,
                role: str | None = None, parent_id: int | None = None) -> ReferralCode:
    allowed = invite_roles(actor)
    if not allowed:
        raise Forbidden(f"A {actor.role_label} cannot invite anyone.")
    role = role or allowed[0]
    if role not in allowed:
        raise Forbidden(f"A {actor.role_label} cannot invite a {role_label(role)}.")
    upline_id = _pick_upline(db, role, parent_id) if actor.role == Role.ADMIN else None
    if ttl_minutes < 1 or ttl_minutes > MAX_TTL_MINUTES:
        raise ValidationError("Timer must be between 1 minute and 30 days.")
    for _ in range(12):
        code = random_code(10)
        if not db.scalar(select(ReferralCode.id).where(ReferralCode.code == code)):
            break
    else:
        raise ValidationError("Could not allocate a code, please retry.")
    row = ReferralCode(code=code, role=role, issuer_id=actor.id, parent_id=upline_id,
                       note=(note or "").strip()[:120] or None,
                       expires_at=_now() + dt.timedelta(minutes=ttl_minutes))
    db.add(row)
    db.flush()
    audit.record(db, actor, "referral.create", entity_type="referral", entity_id=row.id,
                 after={"role": role, "expires_at": row.expires_at.isoformat(),
                        "parent_id": upline_id, "note": row.note})
    return row


def lookup_valid(db: Session, code: str) -> ReferralCode:
    """Return the code row if it can still be used, else a deliberately vague error."""
    row = db.scalar(select(ReferralCode).where(ReferralCode.code == _norm(code)))
    if row is None or row.status() != "active":
        raise ValidationError(_BAD_CODE)
    issuer = db.get(User, row.issuer_id)
    if issuer is None or issuer.status != "active":
        raise ValidationError(_BAD_CODE)
    return row


def redeem(db: Session, code: str, *, username: str, password: str, full_name: str,
           contact: str | None, settings: SettingsStore, birth_date: dt.date | None = None,
           gcash_number: str | None = None) -> User | Player:
    """Consume a code and create the invited account. Returns a User (Master/Cavo/Kubrador) or,
    for a Kubrador's code, a Player."""
    row = lookup_valid(db, code)
    claimed = db.execute(
        update(ReferralCode)
        .where(ReferralCode.id == row.id, ReferralCode.used_at.is_(None),
               ReferralCode.revoked_at.is_(None), ReferralCode.expires_at > _now())
        .values(used_at=_now())
    )
    if claimed.rowcount != 1:
        raise ValidationError(_BAD_CODE)
    issuer = db.get(User, row.issuer_id)
    try:
        if row.role == "player":
            if birth_date is None:
                raise ValidationError("Date of birth is required.")
            if not (gcash_number or "").strip():
                raise ValidationError("A GCash number is required.")
            account = players.register_online_player(
                db, issuer, username=username, password=password, full_name=full_name,
                birth_date=birth_date, gcash_number=gcash_number or "", settings=settings,
                agent_id=row.parent_id or issuer.id)
        else:
            account = agents.create_agent(db, issuer, username=username, password=password,
                                          role=Role(row.role), full_name=full_name, contact=contact,
                                          parent_id=row.parent_id, settings=settings)
            account.must_change_password = False  # they just chose their own password
    except StelError:
        # Bad username / weak password must not burn the code.
        db.execute(update(ReferralCode).where(ReferralCode.id == row.id).values(used_at=None))
        raise
    if row.role != "player":
        row.used_by_id = account.id         # a Player is not a user row; the audit entry names it
    row.used_at = _now()
    audit.record(db, account if row.role != "player" else issuer, "referral.redeem",
                 entity_type="referral", entity_id=row.id,
                 after={"issuer": issuer.username, "role": row.role, "new_account": account.username})
    return account


def list_codes(db: Session, actor: User, limit: int = 200) -> list[ReferralCode]:
    stmt = select(ReferralCode).order_by(ReferralCode.id.desc()).limit(limit)
    if actor.role != Role.ADMIN:       # Admin sees every code; everyone else only their own network's
        stmt = stmt.where(ReferralCode.issuer_id.in_(scope_ids(db, actor)))
    return list(db.scalars(stmt).all())


def revoke(db: Session, actor: User, code_id: int) -> ReferralCode:
    row = db.get(ReferralCode, code_id)
    if row is None:
        raise NotFound("Referral code not found.")
    if actor.role != Role.ADMIN and row.issuer_id not in scope_ids(db, actor):
        raise Forbidden("That code is not in your network.")
    if row.status() != "active":
        raise ValidationError("Only an active code can be revoked.")
    row.revoked_at = _now()
    audit.record(db, actor, "referral.revoke", entity_type="referral", entity_id=row.id)
    return row
