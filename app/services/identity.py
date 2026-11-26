"""Is this username / Gmail already used? One namespace across login accounts (users) and online players."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.core.identity import DEFAULT_USERNAME_MIN, email_key, gmail_parts
from app.core.security import pii
from app.db.models import Player, User


def min_username_length(settings) -> int:
    return int(settings.get("account.username_min_length")) if settings is not None else DEFAULT_USERNAME_MIN


def username_taken(db: Session, username: str, *, user_id: int | None = None, player_id: int | None = None) -> bool:
    """Case-insensitive, so ``Maria2024`` and ``maria2024`` are the same name (nobody can pass as somebody else)."""
    low = username.lower()
    q_user = select(User.id).where(func.lower(User.username) == low)
    q_player = select(Player.id).where(func.lower(Player.username) == low)
    if user_id:
        q_user = q_user.where(User.id != user_id)
    if player_id:
        q_player = q_player.where(Player.id != player_id)
    return bool(db.scalar(q_user) or db.scalar(q_player))


def assert_username_free(db: Session, username: str, *, user_id: int | None = None, player_id: int | None = None) -> None:
    if username_taken(db, username, user_id=user_id, player_id=player_id):
        raise Conflict("That username is already taken. Please choose another.")


def claim_gmail(db: Session, raw: str, *, user_id: int | None = None, player_id: int | None = None) -> tuple[str, str]:
    """Validate a Gmail address and make sure no other account uses it. Returns (encrypted address, reuse key)."""
    shown, canonical = gmail_parts(raw)
    key = email_key(canonical)
    q_user = select(User.id).where(User.email_key == key)
    q_player = select(Player.id).where(Player.email_key == key)
    if user_id:
        q_user = q_user.where(User.id != user_id)
    if player_id:
        q_player = q_player.where(Player.id != player_id)
    if db.scalar(q_user) or db.scalar(q_player):
        raise Conflict("That Gmail is already used by another account. Use a different one.")
    return pii.encrypt(shown), key


def masked_gmail(stored: str | None) -> str:
    """j***e@gmail.com: enough for staff to recognise it without showing the whole address."""
    plain = pii.decrypt(stored) if stored else ""
    if not plain or "@" not in plain:
        return ""
    local, _, domain = plain.partition("@")
    return (local[0] + "***" + local[-1] if len(local) > 2 else local[0] + "*") + "@" + domain
