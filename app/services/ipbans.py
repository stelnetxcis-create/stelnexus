"""IP bans: block an address or a small range from the whole site.

* Enforced by ``IpBanMiddleware`` from an in-memory snapshot (the middleware layer never queries per
  request). The snapshot is rebuilt when a ban changes in this process and at most every
  ``TTL`` seconds otherwise, so another worker picks a change up within that time.
* Lockout protection: you cannot ban the address you are using, or the address the Admin last
  signed in from. Recovery from the server shell: ``python -m app.cli unban-ip <address>``.
* Ranges are capped (IPv4 /16, IPv6 /48 or narrower) so one typo cannot shut out a whole ISP.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import threading
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.db.base import utcnow
from app.db.models import IpBan, Role, User

TTL = 10.0
MIN_PREFIX = {4: 16, 6: 48}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "nets": []}


def parse_network(raw: str):
    text = (raw or "").strip()
    if not text or len(text) > 64:
        raise ValidationError("Enter an IP address like 203.0.113.9 or a range like 203.0.113.0/24.")
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise ValidationError("That is not a valid IP address or range.") from exc
    if net.prefixlen < MIN_PREFIX[net.version]:
        raise ValidationError(f"That range is too wide. Use /{MIN_PREFIX[net.version]} or narrower.")
    return net


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0


def _load(db: Session) -> list:
    now = utcnow()
    rows = db.scalars(select(IpBan).where(or_(IpBan.expires_at.is_(None), IpBan.expires_at > now))).all()
    out = []
    for row in rows:
        try:
            out.append(ipaddress.ip_network(row.network, strict=False))
        except ValueError:
            continue
    return out


def refresh() -> None:
    """Rebuild the snapshot (own session: called from the middleware, outside a request)."""
    from app.db.session import session_scope
    try:
        with session_scope() as db:
            nets = _load(db)
    except Exception:  # noqa: BLE001 - a DB hiccup must not take the site down; keep the old list
        with _lock:
            _cache["at"] = time.monotonic()
        return
    with _lock:
        _cache["nets"], _cache["at"] = nets, time.monotonic()


def snapshot_stale() -> bool:
    return time.monotonic() - _cache["at"] > TTL


def is_banned(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in _cache["nets"])


def list_bans(db: Session, *, limit: int = 200, offset: int = 0) -> list[IpBan]:
    """Newest first, one page at a time (auto-bans can pile up without bound)."""
    return list(db.scalars(select(IpBan).order_by(IpBan.id.desc())
                           .limit(clamp_limit(limit, 500)).offset(clamp_offset(offset))).all())


def ban(db: Session, actor: User, *, network: str, reason: str, hours: int | None = None,
        actor_ip: str | None = None) -> IpBan:
    net = parse_network(network)
    reason = (reason or "").strip()
    if len(reason) < 3 or len(reason) > 255:
        raise ValidationError("Give a reason (3-255 characters).")
    if hours is not None and not 1 <= int(hours) <= 24 * 365:
        raise ValidationError("Duration must be between 1 hour and 365 days, or empty for no expiry.")
    protected = {actor_ip}
    admin_ips = db.scalars(select(User.last_login_ip).where(User.role == Role.ADMIN)).all()
    protected.update(admin_ips)
    for ip in protected:
        try:
            if ip and ipaddress.ip_address(ip) in net:
                raise Forbidden("That address is protected: banning it could lock out an administrator.")
        except ValueError:
            continue
    text = str(net.network_address) if net.num_addresses == 1 else str(net)
    if db.scalar(select(IpBan.id).where(IpBan.network == text)):
        raise Conflict("That address is already banned.")
    expires = utcnow() + dt.timedelta(hours=int(hours)) if hours else None
    row = IpBan(network=text, reason=reason, created_by_id=actor.id, expires_at=expires)
    db.add(row)
    db.flush()
    audit.record(db, actor, "ip.ban", entity_type="ip_ban", entity_id=row.id,
                 after={"network": text, "expires_at": expires}, reason=reason)
    invalidate()
    return row


def unban(db: Session, actor: User | None, ban_id: int) -> IpBan:
    row = db.get(IpBan, ban_id)
    if row is None:
        raise NotFound("That ban no longer exists.")
    audit.record(db, actor, "ip.unban", entity_type="ip_ban", entity_id=row.id,
                 before={"network": row.network, "reason": row.reason})
    db.delete(row)
    db.flush()
    invalidate()
    return row
