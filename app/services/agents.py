"""Agent network: registration, hierarchy, credit transfer, suspension.

Fixes vs. the original:
* role of the created account is validated server-side against the creator's
  role (the old form merely offered a dropdown);
* passwords are Argon2id-hashed and never echoed back;
* credit transfer is a balanced ledger transaction under row locks, scoped to
  the downline, idempotent, and cannot overdraw the sender;
* referral codes come from a CSPRNG, not PHP's rand().
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import assert_user_in_scope, can_create_role, downline_ids, scope_ids
from app.core.security import USERNAME_RE, hash_password, password_problems, random_code
from app.core.settings_registry import SettingsStore
from app.db.models import Role, Status, User
from app.services import accounts, ledger


def _unique_username(db: Session, username: str) -> None:
    if not USERNAME_RE.match(username):
        raise ValidationError("Username must be 3-32 characters: letters, digits, dot, dash, underscore.")
    if db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
        raise Conflict("That username is already taken.")


def _unique_ref(db: Session) -> str:
    for _ in range(12):
        code = random_code(10)
        if not db.scalar(select(User.id).where(User.ref_code == code)):
            return code
    raise Conflict("Could not allocate a referral code, please retry.")


def create_agent(
    db: Session,
    actor: User,
    *,
    username: str,
    password: str,
    role: Role,
    full_name: str,
    contact: str | None = None,
    parent_id: int | None = None,
    settings: SettingsStore | None = None,
) -> User:
    username = username.strip()
    full_name = full_name.strip()
    if not full_name or len(full_name) > 120:
        raise ValidationError("Full name is required (max 120 characters).")
    if not can_create_role(actor, role):
        raise Forbidden(f"A {actor.role_label} cannot create a {role.label}.")
    _unique_username(db, username)

    min_len = int(settings.get("security.password_min_length")) if settings else 12
    problems = password_problems(password, username)
    if len(password) < min_len:
        problems.append(f"Use at least {min_len} characters.")
    if problems:
        raise ValidationError(" ".join(dict.fromkeys(problems)))

    if role == Role.ADMIN:
        parent = None
    elif actor.role == Role.ADMIN and role == Role.MASTER:
        parent = db.get(User, parent_id) if parent_id else actor
    else:
        parent = actor
    if parent is not None and parent.status != Status.ACTIVE:
        raise ValidationError("The upline account is not active.")

    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        full_name=full_name,
        contact=(contact or "").strip()[:32] or None,
        parent_id=parent.id if parent else None,
        ref_code=_unique_ref(db),
        status=Status.ACTIVE,
        must_change_password=True,
        created_by_id=actor.id,
    )
    db.add(user)
    db.flush()
    accounts.wallet_for_user(db, user)
    accounts.commission_for_user(db, user)
    audit.record(db, actor, "agent.create", entity_type="user", entity_id=user.id,
                 after={"username": user.username, "role": role.value, "parent_id": user.parent_id,
                        "full_name": full_name, "contact": contact})
    return user


def update_agent(
    db: Session, actor: User, target_id: int, *, full_name: str | None = None,
    contact: str | None = None, status: Status | None = None, reason: str | None = None,
) -> User:
    target = assert_user_in_scope(db, actor, target_id)
    if target.id == actor.id and status is not None and status != Status.ACTIVE:
        raise ValidationError("You cannot suspend your own account.")
    if target.role == Role.ADMIN and actor.role != Role.ADMIN:
        raise Forbidden("Only an admin may modify an admin.")
    before = {"full_name": target.full_name, "contact": target.contact, "status": target.status}
    if full_name is not None:
        full_name = full_name.strip()
        if not full_name or len(full_name) > 120:
            raise ValidationError("Full name is required (max 120 characters).")
        target.full_name = full_name
    if contact is not None:
        target.contact = contact.strip()[:32] or None
    if status is not None:
        if status == Status.SUSPENDED and not reason:
            raise ValidationError("A reason is required to suspend an account.")
        target.status = status
        if status != Status.ACTIVE:
            revoke_sessions(db, target)
    db.flush()
    audit.record(db, actor, "agent.update", entity_type="user", entity_id=target.id,
                 before=before, after={"full_name": target.full_name, "contact": target.contact,
                                       "status": target.status}, reason=reason)
    return target


def reset_password(db: Session, actor: User, target_id: int, new_password: str,
                   settings: SettingsStore | None = None) -> User:
    target = assert_user_in_scope(db, actor, target_id)
    problems = password_problems(new_password, target.username)
    min_len = int(settings.get("security.password_min_length")) if settings else 12
    if len(new_password) < min_len:
        problems.append(f"Use at least {min_len} characters.")
    if problems:
        raise ValidationError(" ".join(dict.fromkeys(problems)))
    target.password_hash = hash_password(new_password)
    target.must_change_password = True
    target.password_changed_at = dt.datetime.now(dt.timezone.utc)
    target.failed_attempts = 0
    target.locked_until = None
    revoke_sessions(db, target)
    db.flush()
    audit.record(db, actor, "agent.password_reset", entity_type="user", entity_id=target.id)
    return target


def revoke_sessions(db: Session, user: User) -> None:
    from app.db.models import Session as DbSession
    now = dt.datetime.now(dt.timezone.utc)
    for row in db.scalars(select(DbSession).where(DbSession.user_id == user.id, DbSession.revoked_at.is_(None))):
        row.revoked_at = now
    db.flush()


def transfer_credit(
    db: Session, actor: User, *, target_id: int, amount_cents: int,
    settings: SettingsStore, note: str | None = None, idempotency_key: str | None = None,
) -> ledger.LedgerTxn:
    """Move wallet credit down (or back up) the tree. Atomic and audited."""
    target = assert_user_in_scope(db, actor, target_id)
    if target.id == actor.id:
        raise ValidationError("You cannot transfer load credit to yourself.")
    if target.status != Status.ACTIVE:
        raise ValidationError("Target account is not active.")
    if actor.role != Role.ADMIN and target.id not in downline_ids(db, actor):
        raise Forbidden("You may only transfer load credit to your own downline.")
    if target.role == Role.TELLER:
        raise ValidationError("Station accounts are funded through the cash desk, not load credit transfer.")

    minimum = int(settings.get("wallet.min_transfer_cents"))
    maximum = int(settings.get("wallet.max_transfer_cents"))
    if amount_cents < minimum or amount_cents > maximum:
        from app.core.money import fmt
        raise ValidationError(f"Transfer must be between {fmt(minimum)} and {fmt(maximum)}.")

    sender = accounts.wallet_for_user(db, actor)
    receiver = accounts.wallet_for_user(db, target)
    txn = ledger.post(
        db, kind="credit.transfer",
        entries=[(sender, +amount_cents), (receiver, -amount_cents)],
        actor=actor, memo=f"Credit transfer to {target.username}",
        ref_type="user", ref_id=target.id, idempotency_key=idempotency_key,
        reason=note,
    )
    audit.record(db, actor, "credit.transfer", entity_type="user", entity_id=target.id,
                 after={"amount_cents": amount_cents, "to": target.username, "txn": txn.uuid}, reason=note)
    return txn


def list_agents(db: Session, actor: User, *, search: str | None = None,
                role: Role | None = None, limit: int = 200, offset: int = 0) -> list[User]:
    stmt = select(User)
    if actor.role != Role.ADMIN:
        stmt = stmt.where(User.id.in_(scope_ids(db, actor) - {actor.id}))
    else:
        stmt = stmt.where(User.role != Role.TELLER)
    if role:
        stmt = stmt.where(User.role == role)
    if search:
        term = f"%{search.strip()[:60]}%"
        stmt = stmt.where(or_(User.username.ilike(term), User.full_name.ilike(term), User.ref_code.ilike(term)))
    stmt = stmt.order_by(User.id.desc()).limit(min(limit, 500)).offset(max(offset, 0))
    return list(db.scalars(stmt).all())


def get_agent(db: Session, actor: User, agent_id: int) -> User:
    user = db.get(User, agent_id)
    if user is None:
        raise NotFound("Agent not found.")
    assert_user_in_scope(db, actor, agent_id)
    return user


def wallet_balance(db: Session, user: User) -> int:
    return accounts.wallet_for_user(db, user).balance_cents


def commission_balance(db: Session, user: User) -> int:
    return accounts.commission_for_user(db, user).balance_cents


def tree(db: Session, actor: User) -> list[dict]:
    """Nested downline for the dashboard tree widget."""
    ids = scope_ids(db, actor)
    users = list(db.scalars(select(User).where(User.id.in_(ids)).order_by(User.id)))
    by_parent: dict[int | None, list[User]] = {}
    for user in users:
        by_parent.setdefault(user.parent_id, []).append(user)

    def build(parent_id: int | None, depth: int = 0) -> list[dict]:
        if depth > 6:
            return []
        return [
            {"user": u, "wallet": wallet_balance(db, u), "children": build(u.id, depth + 1)}
            for u in by_parent.get(parent_id, [])
        ]

    return build(actor.id) if actor.role != Role.ADMIN else build(None)
