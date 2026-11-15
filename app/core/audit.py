"""Append-only audit trail + request context.

The study called out that Recompute/Redeclare could silently rewrite settled
fights with no record of who/when/why. Every privileged mutation now writes an
audit row, and the database refuses UPDATE/DELETE on this table.
"""
from __future__ import annotations

import contextvars
import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import mask
from app.db.models import AuditLog, User

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar("client_ip", default=None)
_user_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_agent", default=None)

SENSITIVE_KEYS = {"password", "password_hash", "csrf_token", "token", "token_hash", "secret", "totp_secret"}


def set_context(request_id: str | None, ip: str | None, user_agent: str | None) -> None:
    _request_id.set(request_id)
    _client_ip.set(ip)
    _user_agent.set(user_agent)


def get_request_id() -> str | None:
    return _request_id.get()


def get_ip() -> str | None:
    return _client_ip.get()


def sanitize(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if lowered in SENSITIVE_KEYS:
            out[key] = "***"
        elif any(s in lowered for s in ("gcash", "address", "contact", "card_number")):
            out[key] = mask(str(value)) if value else value
        elif isinstance(value, (dt.datetime, dt.date)):
            out[key] = value.isoformat()
        elif hasattr(value, "value"):  # enums
            out[key] = value.value
        else:
            out[key] = value
    return out


def record(
    db: Session,
    actor: User | None,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        actor_username=actor.username if actor else "system",
        actor_role=str(getattr(actor, "role", "") or "") or None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        before=sanitize(before),
        after=sanitize(after),
        reason=(reason or None) and reason[:255],
        ip=get_ip(),
        user_agent=(_user_agent.get() or "")[:255] or None,
        request_id=get_request_id(),
    )
    db.add(entry)
    db.flush()
    return entry
