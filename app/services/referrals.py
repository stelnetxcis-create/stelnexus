"""One-time referral codes: the only way to self-register an account.

* A code is always single-use and has an expiry timer chosen by its issuer.
* It fixes the role of the new account and attaches it under the issuer.
* Redemption claims the code with a conditional UPDATE, so two simultaneous
  sign-ups can never both succeed.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Forbidden, NotFound, StelError, ValidationError
from app.core.rbac import can_create_role, scope_ids
from app.core.security import random_code
from app.core.settings_registry import SettingsStore
from app.db.models import ReferralCode, Role, User
from app.services import agents

MAX_TTL_MINUTES = 43200  # 30 days
_BAD_CODE = "That referral code is invalid, already used, or expired."


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _norm(code: str) -> str:
    return "".join((code or "").split()).upper()[:16]


def allowed_roles(actor: User) -> list[Role]:
    return [r for r in (Role.MASTER, Role.GOLD, Role.SUB, Role.TELLER, Role.ADMIN)
            if can_create_role(actor, r)]


def create_code(db: Session, actor: User, *, role: Role, ttl_minutes: int,
                note: str | None = None) -> ReferralCode:
    if not can_create_role(actor, role):
        raise Forbidden(f"A {actor.role_label} cannot invite a {role.label}.")
    if ttl_minutes < 1 or ttl_minutes > MAX_TTL_MINUTES:
        raise ValidationError("Timer must be between 1 minute and 30 days.")
    for _ in range(12):
        code = random_code(10)
        if not db.scalar(select(ReferralCode.id).where(ReferralCode.code == code)):
            break
    else:
        raise ValidationError("Could not allocate a code, please retry.")
    row = ReferralCode(code=code, role=role.value, issuer_id=actor.id,
                       note=(note or "").strip()[:120] or None,
                       expires_at=_now() + dt.timedelta(minutes=ttl_minutes))
    db.add(row)
    db.flush()
    audit.record(db, actor, "referral.create", entity_type="referral", entity_id=row.id,
                 after={"role": role.value, "expires_at": row.expires_at.isoformat(),
                        "note": row.note})
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
           contact: str | None, settings: SettingsStore) -> User:
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
        user = agents.create_agent(db, issuer, username=username, password=password,
                                   role=Role(row.role), full_name=full_name, contact=contact,
                                   settings=settings)
    except StelError:
        # Bad username / weak password must not burn the code.
        db.execute(update(ReferralCode).where(ReferralCode.id == row.id).values(used_at=None))
        raise
    user.must_change_password = False  # they just chose their own password
    row.used_by_id = user.id
    row.used_at = _now()
    audit.record(db, user, "referral.redeem", entity_type="referral", entity_id=row.id,
                 after={"issuer": issuer.username, "role": row.role, "new_user": user.username})
    return user


def list_codes(db: Session, actor: User, limit: int = 200) -> list[ReferralCode]:
    stmt = select(ReferralCode).order_by(ReferralCode.id.desc()).limit(limit)
    if actor.role != Role.ADMIN:
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
