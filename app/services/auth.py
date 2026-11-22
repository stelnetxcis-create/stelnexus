"""Authentication & session lifecycle.

Fixes vs. the original login/session handling:
* Argon2id verification with per-user lockout and IP+username rate limiting;
* generic error messages (no username enumeration);
* opaque, server-side sessions -- only a SHA-256 digest is stored, and the
  password is never placed in the session (the old code kept `SESPASS`);
* idle + absolute expiry, rotation on login, revocation on password change;
* separate bearer tokens for the mobile API, hashed at rest and expiring.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit, ratelimit
from app.core.errors import AuthError, Forbidden, RateLimited, ValidationError
from app.core.security import (
    hash_password,
    needs_rehash,
    new_csrf_token,
    new_token,
    password_problems,
    token_hash,
    verify_password,
)
from app.core.settings_registry import SettingsStore
from app.db.models import ApiToken, LoginAttempt, Role, Status, User
from app.db.models import Session as DbSession


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


_LOCK_MSG = "Too many failed sign-ins. Wait a few minutes and try again."


def _recent_failures(db: Session, username: str, ip: str | None, minutes: int) -> int:
    """Failed sign-ins for this username *from this address* since the last success."""
    ident = [LoginAttempt.username == username[:32],
             (LoginAttempt.ip == ip) if ip else LoginAttempt.ip.is_(None)]
    since = _now() - dt.timedelta(minutes=minutes)
    last_ok = db.scalar(select(func.max(LoginAttempt.created_at)).where(*ident, LoginAttempt.success.is_(True)))
    if last_ok is not None and last_ok > since:
        since = last_ok
    return int(db.scalar(select(func.count(LoginAttempt.id)).where(
        *ident, LoginAttempt.success.is_(False), LoginAttempt.created_at > since)) or 0)


def authenticate(db: Session, *, username: str, password: str, ip: str | None,
                 settings: SettingsStore) -> User:
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("Invalid username or password.")
    # One address cannot spray many usernames, nor hammer one.
    ratelimit.check(f"loginip:{ip or 'unknown'}", limit=30, window_seconds=300)
    ratelimit.check(ratelimit.login_key(ip, username), limit=10, window_seconds=300)

    user = db.scalar(select(User).where(User.username == username))
    max_attempts = int(settings.get("security.max_login_attempts"))
    lockout_minutes = int(settings.get("security.lockout_minutes"))

    # Lockout is scoped to (username, IP) and answers identically whether or not the account
    # exists: strangers cannot lock a real user out, and cannot learn which usernames are real.
    if _recent_failures(db, username, ip, lockout_minutes) >= max_attempts:
        raise AuthError(_LOCK_MSG)
    # Account-wide lock only after sustained, distributed guessing (4x the threshold).
    if user is not None and user.locked_until and user.locked_until > _now():
        raise AuthError(_LOCK_MSG)

    ok = verify_password(password, user.password_hash if user else None)
    db.add(LoginAttempt(username=username[:32], ip=ip, success=bool(ok and user)))

    if not ok or user is None:
        if user is not None:
            user.failed_attempts += 1
            if user.failed_attempts >= max_attempts * 4:
                user.locked_until = _now() + dt.timedelta(minutes=lockout_minutes)
                user.failed_attempts = 0
                audit.record(db, user, "auth.lockout", entity_type="user", entity_id=user.id)
        # Commit now: callers unwind the transaction on AuthError (the API path did), which
        # used to roll these counters back and made lockout ineffective there.
        db.commit()
        raise AuthError("Invalid username or password.")

    if user.status != Status.ACTIVE:
        db.flush()
        raise Forbidden("This account is not active. Contact your upline.")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = _now()
    user.last_login_ip = (ip or None)
    db.flush()
    ratelimit.reset(ratelimit.login_key(ip, username))
    audit.record(db, user, "auth.login", entity_type="user", entity_id=user.id)
    return user


SIGNED_IN_ELSEWHERE = "signed_in_elsewhere"
DISPLACED_MSG = "You were signed out because this account signed in on another device."


def end_other_logins(db: Session, user: User, *, settings: SettingsStore) -> int:
    """One device per account: revoke every live web session and mobile token of ``user``.

    Called just before a brand-new login is issued, so the newest sign-in is the only one left.
    Returns how many were ended. A no-op when ``security.single_session`` is switched off."""
    if not bool(settings.get("security.single_session")):
        return 0
    now = _now()
    ended = 0
    for row in db.scalars(select(DbSession).where(DbSession.user_id == user.id, DbSession.revoked_at.is_(None))).all():
        row.revoked_at, row.revoked_reason = now, SIGNED_IN_ELSEWHERE
        ended += 1
    for tok in db.scalars(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))).all():
        tok.revoked_at = now
        ended += 1
    if ended:
        db.flush()
        audit.record(db, user, "auth.other_logins_ended", entity_type="user", entity_id=user.id, after={"ended": ended})
    return ended


def was_displaced(db: Session, raw_token: str | None) -> bool:
    """True when this cookie belongs to a session that a newer login replaced (to tell the person why)."""
    if not raw_token or len(raw_token) > 200:
        return False
    row = db.scalar(select(DbSession).where(DbSession.token_hash == token_hash(raw_token)))
    return row is not None and row.revoked_reason == SIGNED_IN_ELSEWHERE


def create_session(db: Session, user: User, *, ip: str | None, user_agent: str | None,
                   settings: SettingsStore) -> tuple[str, DbSession]:
    from app.core.config import get_settings
    cfg = get_settings()
    end_other_logins(db, user, settings=settings)
    raw = new_token()
    row = DbSession(
        token_hash=token_hash(raw), user_id=user.id, csrf_token=new_csrf_token(),
        ip=ip, user_agent=(user_agent or "")[:255],
        absolute_expires_at=_now() + dt.timedelta(seconds=cfg.session_absolute_seconds),
    )
    db.add(row)
    db.flush()
    return raw, row


def resolve_session(db: Session, raw_token: str | None, settings: SettingsStore) -> tuple[DbSession, User] | None:
    if not raw_token or len(raw_token) > 200:
        return None
    row = db.scalar(select(DbSession).where(DbSession.token_hash == token_hash(raw_token)))
    if row is None or row.revoked_at is not None:
        return None
    now = _now()
    idle = dt.timedelta(minutes=int(settings.get("security.session_idle_minutes")))
    if row.absolute_expires_at <= now or row.last_seen_at + idle <= now:
        row.revoked_at = now
        db.flush()
        return None
    user = db.get(User, row.user_id)
    if user is None or user.status != Status.ACTIVE:
        return None
    if user.password_changed_at and row.created_at < user.password_changed_at:
        row.revoked_at = now
        db.flush()
        return None
    if now - row.last_seen_at > dt.timedelta(seconds=60):
        row.last_seen_at = now
        db.flush()
    return row, user


def destroy_session(db: Session, row: DbSession, user: User | None = None) -> None:
    row.revoked_at = _now()
    db.flush()
    if user is not None:
        audit.record(db, user, "auth.logout", entity_type="user", entity_id=user.id)


def change_password(db: Session, user: User, *, current: str, new: str,
                    settings: SettingsStore) -> None:
    if not verify_password(current, user.password_hash):
        raise ValidationError("Your current password is incorrect.")
    problems = password_problems(new, user.username, min_length=int(settings.get("security.password_min_length")))
    if verify_password(new, user.password_hash):
        problems.append("Choose a password you have not used here before.")
    if problems:
        raise ValidationError(" ".join(dict.fromkeys(problems)))
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.password_changed_at = _now()
    db.flush()
    # Revoke every other session; the caller re-issues a fresh one.
    for row in db.scalars(select(DbSession).where(DbSession.user_id == user.id,
                                                  DbSession.revoked_at.is_(None))).all():
        row.revoked_at = _now()
    db.flush()
    audit.record(db, user, "auth.password_change", entity_type="user", entity_id=user.id)


# --- API tokens (mobile) ---------------------------------------------------

def create_api_token(db: Session, user: User, *, name: str, days: int = 30) -> tuple[str, ApiToken]:
    from app.core.config import get_settings
    raw = f"snx_{new_token(24)}"
    row = ApiToken(
        name=name.strip()[:80] or "mobile", token_hash=token_hash(raw), user_id=user.id,
        expires_at=_now() + dt.timedelta(days=max(1, min(days, get_settings().API_TOKEN_TTL_DAYS))),
    )
    db.add(row)
    db.flush()
    audit.record(db, user, "auth.token_create", entity_type="api_token", entity_id=row.id,
                 after={"name": row.name, "expires_at": row.expires_at})
    return raw, row


def resolve_api_token(db: Session, raw: str | None) -> User | None:
    if not raw or not raw.startswith("snx_") or len(raw) > 200:
        return None
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(raw)))
    if row is None or row.revoked_at is not None or row.expires_at <= _now():
        return None
    user = db.get(User, row.user_id)
    if user is None or user.status != Status.ACTIVE:
        return None
    row.last_used_at = _now()
    db.flush()
    return user


def revoke_api_token(db: Session, user: User, token_id: int) -> None:
    row = db.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        raise ValidationError("Token not found.")
    row.revoked_at = _now()
    db.flush()
    audit.record(db, user, "auth.token_revoke", entity_type="api_token", entity_id=row.id)


def list_api_tokens(db: Session, user: User) -> list[ApiToken]:
    return list(db.scalars(select(ApiToken).where(ApiToken.user_id == user.id)
                           .order_by(ApiToken.id.desc())).all())
