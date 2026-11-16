"""Who is signed in right now. Admin-only by default (a Pawn needs the 'online:view' grant)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Forbidden
from app.core.rbac import Perm, has_perm
from app.core.settings_registry import SettingsStore
from app.db.models import Role, Session as SessionRow, User


def _device(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    kind = "phone" if any(k in ua for k in ("iphone", "android", "mobile")) else "computer"
    for name, needle in (("Edge", "edg/"), ("Chrome", "chrome/"), ("Firefox", "firefox/"), ("Safari", "safari/")):
        if needle in ua:
            return f"{name} · {kind}"
    return kind


def online_accounts(db: Session, viewer: User, settings: SettingsStore) -> list[dict]:
    if not has_perm(viewer, Perm.ONLINE_VIEW):
        raise Forbidden("Only the Admin can see who is online.")
    now = dt.datetime.now(dt.timezone.utc)
    window = dt.timedelta(minutes=int(settings.get("presence.window_minutes")))
    stmt = (select(SessionRow, User).join(User, User.id == SessionRow.user_id)
            .where(SessionRow.revoked_at.is_(None), SessionRow.absolute_expires_at > now,
                   SessionRow.last_seen_at >= now - window)
            .order_by(SessionRow.last_seen_at.desc()))
    if viewer.role != Role.ADMIN:
        stmt = stmt.where(User.role != Role.ADMIN)
    people: dict[int, dict] = {}
    for session, user in db.execute(stmt).all():
        slot = people.get(user.id)
        if slot is None:
            people[user.id] = {"user": user, "last_seen_at": session.last_seen_at, "ip": session.ip,
                               "device": _device(session.user_agent), "sessions": 1}
        else:
            slot["sessions"] += 1
    return list(people.values())
