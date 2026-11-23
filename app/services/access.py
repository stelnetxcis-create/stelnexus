"""Asking first: requests for a right, the people who answer them, and the King's emergency switches.

A right can be set to *Ask first* (see app.core.rules). The holder may then START the action, but it does not run:

  1. ``gate()`` (called by ``ctx.need``) sees "ask first" on a mutating request and raises ApprovalRequired.
  2. The exception handler calls ``park_request()``: the request, with the form that was sent (secrets stripped), is
     stored and the King and the Bishops are notified.
  3. An approver opens Admin Panel -> Access -> Requests and answers: deny, or approve with a PASS:
       once      this exact action only (bound to the sent values: change the amount or name and it no longer fits)
       a while   any such action for 15 minutes ... a few days
       N uses    that many times
       always    switch the right on for that account for good
  4. The requester runs the action THEMSELVES (My requests -> Run). The approver never acts in their name.

A denied page works the same way: "Ask for access" creates an *access* request (answered with a time-boxed pass).

Who answers: the King answers everything. Bishops answer requests from anyone below them, if the King allows it
(flag ``bishops_may_approve``), only for rights they hold themselves and never for sealed ones. A request from a Bishop
goes to the King alone, and the Bishop is never told a King exists (the answer just says "an admin").
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from urllib.parse import parse_qsl

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import audit, rbac
from app.core import rules as rules_mod
from app.core.errors import ApprovalRequired, Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import FLAG_DEFAULTS, KING_ONLY, Perm, VIEW_PERMS, flag, rights_of
from app.db.models import (ADMIN_TIER, AccessFlag, AccessRequest, Notification, PermUsage, Role, Status, User)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_SECRET = re.compile(r"pass|token|secret|pin|otp|csrf", re.I)
ONCE_HOURS = 24                      # how long an approved one-time pass waits to be used
USES_HOURS = 24
TIMED_CHOICES = {"15m": 0.25, "1h": 1, "4h": 4, "8h": 8, "24h": 24, "3d": 72, "7d": 168}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(moment: dt.datetime | None) -> dt.datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def perm_label(perm: str) -> str:
    for _, items in rbac.PERM_CATALOG:
        for p, label in items:
            if p.value == perm:
                return label
    return perm


# --------------------------------------------------------------------------- what was sent

def body_fields(request) -> list[list[str]] | None:
    """The form the browser sent, without secrets (passwords, tokens), or None when it could not be captured
    (an XHR call, a file upload). Kept by the CSRF middleware."""
    raw = (request.scope.get("state") or {}).get("form_body")
    if not raw:
        return None
    pairs = parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return [[k, v[:500]] for k, v in pairs if not _SECRET.search(k)][:80]


def bind_hash(method: str, path: str, fields: list[list[str]] | None) -> str:
    """What a one-time pass is tied to: this action, on this address, with these values."""
    return hashlib.sha256(json.dumps([method.upper(), path, sorted(fields or [])]).encode()).hexdigest()


def describe_action(request, perm: str) -> str:
    endpoint = request.scope.get("endpoint")
    name = (getattr(endpoint, "__name__", "") or "").replace("_", " ").strip()
    return f"{perm_label(perm)}: {name or request.url.path}"[:300]


# --------------------------------------------------------------------------- the gate (called from ctx.need)

def _consume_limit(db: Session, user: User, perm: Perm, rule: dict, now: dt.datetime) -> bool:
    """Count one use against a Limit rule. False when the allowance for this period is already spent."""
    key = rules_mod.period_key(rule, now)
    row = db.get(PermUsage, (user.id, perm.value, key))
    if row is None:
        row = PermUsage(user_id=user.id, perm=perm.value, period=key, count=0)
        db.add(row)
        db.flush()
    if row.count >= int(rule["max"]):
        return False
    row.count += 1
    return True


def _use_pass(ctx, user: User, perm: Perm, now: dt.datetime) -> bool:
    """Spend an approved pass for this right, if one fits this request. Passes are consumed with the request's own
    transaction, so an action that fails validation does not burn the pass."""
    request = ctx.request
    rows = ctx.db.scalars(select(AccessRequest).where(
        AccessRequest.requester_id == user.id, AccessRequest.perm == perm.value,
        AccessRequest.status == "approved").order_by(AccessRequest.id)).all()
    fields = None
    for row in rows:
        until = _aware(row.pass_until)
        if until is not None and until <= now:
            row.status = "expired"
            continue
        if row.bind_hash is None and row.uses_left is None:
            continue                                    # a timed pass: already part of rights_of
        if row.bind_hash is not None:
            if request.method.upper() != row.method or request.url.path != row.path:
                continue
            if fields is None:
                fields = body_fields(request) or []
            if bind_hash(request.method, request.url.path, fields) != row.bind_hash:
                continue                                # the values changed: the approval no longer fits
        if row.uses_left is not None:
            row.uses_left -= 1
            if row.uses_left <= 0:
                row.status, row.used_at = "used", now
        else:
            row.status, row.used_at = "used", now
        elevated = user.__dict__.setdefault("_elevated", set())
        elevated.add(perm)
        return True
    return False


def gate(ctx, perm: Perm) -> None:
    """The one place a right is enforced for a request. Returns when it may go ahead; raises Forbidden when not and
    ApprovalRequired when the account must ask first (the action is then parked, not run)."""
    user = ctx.user
    if user is None or user.status != Status.ACTIVE:
        raise Forbidden("Your role is not allowed to perform this action.")
    rights = rights_of(user)
    now = _now()
    over_asks = False
    if perm in rights.allowed:
        rule = rights.limits.get(perm)
        if rule is None or _consume_limit(ctx.db, user, perm, rule, now):
            return
        if rule.get("over") != "ask":
            ctx.request.state.denied_perm = perm.value
            raise Forbidden(f"You have used your allowance for this ({rules_mod.describe(rule)}).")
        over_asks = True
    if rights.frozen:
        raise Forbidden("Your account is in read-only mode right now.")
    if flag("approvals_paused"):
        ctx.request.state.denied_perm = perm.value
        raise Forbidden("Approvals are paused right now. Try again later.")
    if _use_pass(ctx, user, perm, now):
        return
    if (perm in rights.ask or over_asks) and ctx.request.method.upper() in MUTATING and perm not in VIEW_PERMS:
        raise ApprovalRequired(perm=perm.value, user_id=user.id)
    if perm not in KING_ONLY:
        ctx.request.state.denied_perm = perm.value          # the 403 page offers "ask for access"
    raise Forbidden("Your role is not allowed to perform this action.")


# --------------------------------------------------------------------------- who answers

def approvers(db: Session, requester: User) -> list[User]:
    """Everyone who can answer this requester: the King, and Bishops when the King allows it and the requester is
    below them. A request from a Bishop goes to the King alone."""
    roles = [Role.ADMIN]
    if flag("bishops_may_approve") and requester.role not in ADMIN_TIER:
        roles.append(Role.BISHOP)
    return list(db.scalars(select(User).where(User.role.in_(roles), User.status == Status.ACTIVE,
                                              User.id != requester.id)).all())


def can_decide(db: Session, actor: User, req: AccessRequest, requester: User | None = None) -> bool:
    if actor.role not in ADMIN_TIER or Perm.APPROVALS_DECIDE not in rbac.permissions_of(actor):
        return False
    if actor.role == Role.ADMIN:
        return True
    requester = requester or db.get(User, req.requester_id)
    if requester is None or requester.role in ADMIN_TIER or not flag("bishops_may_approve"):
        return False
    try:
        perm = Perm(req.perm)
    except ValueError:
        return False
    return perm in rbac.permissions_of(actor) and perm not in rbac.sealed_perms()


def _invalidate_after_commit(db: Session) -> None:
    """Refresh the cached settings now and again once this transaction has committed, so a pass is live at once."""
    from sqlalchemy import event
    rbac.invalidate_role_overrides()
    event.listen(db, "after_commit", lambda _s: rbac.invalidate_role_overrides(), once=True)


def _notify(db: Session, users: list[User], text: str, level: str = "warning", link: str = "/admin/access/requests") -> None:
    for user in users:
        db.add(Notification(user_id=user.id, level=level, message=text[:255], link=link))


def _ttl() -> dt.timedelta:
    return dt.timedelta(hours=max(1, int(flag("request_ttl_hours") or 24)))


def expire_stale(db: Session) -> None:
    now = _now()
    db.execute(update(AccessRequest).where(AccessRequest.status == "pending", AccessRequest.expires_at.is_not(None),
                                           AccessRequest.expires_at < now).values(status="expired"))
    db.execute(update(AccessRequest).where(AccessRequest.status == "approved", AccessRequest.pass_until.is_not(None),
                                           AccessRequest.pass_until < now).values(status="expired"))


# --------------------------------------------------------------------------- creating requests

def _pending_twin(db: Session, user_id: int, perm: str, path: str, kind: str, digest: str | None):
    stmt = select(AccessRequest).where(AccessRequest.requester_id == user_id, AccessRequest.perm == perm,
                                       AccessRequest.status == "pending", AccessRequest.kind == kind,
                                       AccessRequest.path == path)
    if digest is not None:
        stmt = stmt.where(AccessRequest.bind_hash == digest)
    return db.scalars(stmt.limit(1)).first()


def park_request(db: Session, request, exc: ApprovalRequired) -> AccessRequest | None:
    """Store the action that was held back. Called by the exception handler with its own database session, because
    the request's own transaction is being rolled back."""
    user = db.get(User, exc.user_id) if exc.user_id else None
    if user is None:
        return None
    fields = body_fields(request)
    digest = bind_hash(request.method, request.url.path, fields)
    twin = _pending_twin(db, user.id, exc.perm, request.url.path, "action", digest)
    if twin is not None:
        return twin                                     # pressing the button twice does not queue it twice
    now = _now()
    row = AccessRequest(requester_id=user.id, perm=exc.perm, kind="action", method=request.method.upper(),
                        path=request.url.path[:300], summary=describe_action(request, exc.perm),
                        payload={"fields": fields} if fields is not None else None, bind_hash=digest,
                        status="pending", expires_at=now + _ttl(), created_at=now)
    db.add(row)
    db.flush()
    _notify(db, approvers(db, user), f"{user.username} asks: {row.summary}")
    return row


def ask_for_access(db: Session, user: User, perm_value: str, *, path: str = "", reason: str = "") -> AccessRequest:
    """Someone who hit a closed door asks for a way in (answered with a time-boxed pass)."""
    try:
        perm = Perm(perm_value)
    except ValueError:
        raise ValidationError("That right does not exist.")
    if perm in KING_ONLY or (user.role in (Role.BISHOP, Role.PAWN) and perm in rbac.sealed_perms()):
        raise Forbidden("That cannot be requested.")
    if user.role == Role.ADMIN:
        raise Forbidden("Nothing to ask for.")
    if perm in rbac.permissions_of(user):
        raise Conflict("You already have that right.")
    twin = _pending_twin(db, user.id, perm.value, path[:300], "access", None)
    if twin is not None:
        return twin
    now = _now()
    row = AccessRequest(requester_id=user.id, perm=perm.value, kind="access", method="GET", path=path[:300],
                        summary=f"Access: {perm_label(perm.value)}", reason=(reason or "").strip()[:300] or None,
                        status="pending", expires_at=now + _ttl(), created_at=now)
    db.add(row)
    db.flush()
    _notify(db, approvers(db, user), f"{user.username} asks for access: {perm_label(perm.value)}")
    return row


# --------------------------------------------------------------------------- answering requests

def visible_requests(db: Session, viewer: User, *, status: str | None = None, limit: int = 100) -> list[AccessRequest]:
    """Requests this approver may see: the King all of them; a Bishop those of people below it."""
    expire_stale(db)
    if viewer.role not in ADMIN_TIER:
        return []
    stmt = select(AccessRequest).order_by(AccessRequest.id.desc()).limit(limit)
    if viewer.role == Role.BISHOP:
        if not flag("bishops_may_approve"):
            return []
        stmt = stmt.join(User, User.id == AccessRequest.requester_id).where(User.role.notin_(ADMIN_TIER))
    if status:
        stmt = stmt.where(AccessRequest.status == status)
    return list(db.scalars(stmt).all())


def pending_count(db: Session, viewer: User) -> int:
    if viewer.role not in ADMIN_TIER or Perm.APPROVALS_DECIDE not in rbac.permissions_of(viewer):
        return 0
    stmt = select(func.count(AccessRequest.id)).where(
        AccessRequest.status == "pending",
        (AccessRequest.expires_at.is_(None)) | (AccessRequest.expires_at >= _now()))
    if viewer.role == Role.BISHOP:
        if not flag("bishops_may_approve"):
            return 0
        stmt = stmt.join(User, User.id == AccessRequest.requester_id).where(User.role.notin_(ADMIN_TIER))
    return int(db.scalar(stmt) or 0)


def mine(db: Session, user: User, *, limit: int = 50) -> list[AccessRequest]:
    expire_stale(db)
    return list(db.scalars(select(AccessRequest).where(AccessRequest.requester_id == user.id)
                           .order_by(AccessRequest.id.desc()).limit(limit)).all())


def decide(db: Session, actor: User, request_id: int, *, decision: str, grant: str = "once", note: str = "") -> AccessRequest:
    """Approve (with a pass) or deny a pending request. ``grant``: once | uses:<n> | <15m|1h|4h|8h|24h|3d|7d> | always."""
    req = db.get(AccessRequest, request_id)
    if req is None:
        raise NotFound("Request not found.")
    requester = db.get(User, req.requester_id)
    if not can_decide(db, actor, req, requester):
        raise Forbidden("You cannot answer this request.")
    expire_stale(db)
    db.refresh(req)
    if req.status != "pending":
        raise Conflict(f"That request is already {req.status}.")
    now = _now()
    note = (note or "").strip()[:300] or None
    req.decided_by_id, req.decided_at, req.decision_note = actor.id, now, note
    who = "An admin"
    if decision == "deny":
        req.status = "denied"
        db.flush()
        audit.record(db, actor, "access.deny", entity_type="access_request", entity_id=req.id,
                     after={"perm": req.perm, "for": requester.username if requester else None})
        _notify(db, [requester] if requester else [], f"{who} said no: {req.summary}" + (f" ({note})" if note else ""),
                level="danger", link="/access/mine")
        return req
    if decision != "approve":
        raise ValidationError("Choose approve or deny.")
    max_hours = max(1, int(flag("max_pass_hours") or 168))
    req.grant_mode = grant.split(":")[0] if grant not in TIMED_CHOICES else "timed"
    if grant == "always":
        _grant_always(db, actor, requester, req.perm)
        req.status, req.grant_mode = "approved", "always"
    elif grant in TIMED_CHOICES:
        hours = min(TIMED_CHOICES[grant], max_hours)
        req.status, req.pass_until, req.bind_hash = "approved", now + dt.timedelta(hours=hours), None
        req.uses_left = None
    elif grant.startswith("uses:"):
        try:
            uses = int(grant.split(":", 1)[1])
        except ValueError:
            raise ValidationError("How many uses?")
        if not 1 <= uses <= 50:
            raise ValidationError("A pass can be for 1 to 50 uses.")
        req.status, req.uses_left, req.pass_until, req.bind_hash = "approved", uses, now + dt.timedelta(hours=USES_HOURS), None
        req.grant_mode = "uses"
    elif grant == "once":
        if req.kind == "access":
            req.status, req.uses_left, req.bind_hash = "approved", 1, None
        else:
            req.status, req.uses_left = "approved", None          # bound to the exact action (bind_hash kept)
        req.pass_until = now + dt.timedelta(hours=min(ONCE_HOURS, max_hours))
        req.grant_mode = "once"
    else:
        raise ValidationError("Choose how long the approval lasts.")
    db.flush()
    audit.record(db, actor, "access.approve", entity_type="access_request", entity_id=req.id,
                 after={"perm": req.perm, "for": requester.username if requester else None, "grant": grant})
    _invalidate_after_commit(db)
    detail = {"once": "one time", "uses": "a few times", "timed": "for a while", "always": "from now on"}[req.grant_mode]
    _notify(db, [requester] if requester else [], f"{who} approved ({detail}): {req.summary}. Open My requests to run it.",
            level="success", link="/access/mine")
    return req


def _grant_always(db: Session, actor: User, target: User | None, perm_value: str) -> None:
    """Approve 'always': switch the right on for that account (kept inside the approver's own rights)."""
    if target is None:
        raise NotFound("Account not found.")
    perm = Perm(perm_value)
    if perm in rbac.NON_DELEGABLE or perm in KING_ONLY:
        raise Forbidden("That right cannot be handed out.")
    if actor.role != Role.ADMIN and perm not in rbac.permissions_of(actor):
        raise Forbidden("You cannot hand out a right you do not hold.")
    layer_g = [k for k in (target.perm_grants or []) if k != perm.value]
    target.perm_grants = layer_g + [perm.value]
    target.perm_revokes = [k for k in (target.perm_revokes or []) if k != perm.value]
    rules = dict(target.perm_rules or {})
    rules.pop(perm.value, None)
    target.perm_rules = rules


def cancel(db: Session, user: User, request_id: int) -> AccessRequest:
    req = db.get(AccessRequest, request_id)
    if req is None or req.requester_id != user.id:
        raise NotFound("Request not found.")
    if req.status not in ("pending", "approved"):
        raise Conflict("That request is already closed.")
    req.status = "cancelled"
    db.flush()
    _invalidate_after_commit(db)
    return req


def revoke_pass(db: Session, actor: User, request_id: int) -> AccessRequest:
    req = db.get(AccessRequest, request_id)
    if req is None:
        raise NotFound("Request not found.")
    if not can_decide(db, actor, req):
        raise Forbidden("You cannot change this request.")
    if req.status != "approved":
        raise Conflict("That pass is not active.")
    req.status = "revoked"
    db.flush()
    audit.record(db, actor, "access.revoke", entity_type="access_request", entity_id=req.id, after={"perm": req.perm})
    _invalidate_after_commit(db)
    return req


# --------------------------------------------------------------------------- the King's switches

_BOOL_FLAGS = ("freeze_pawns", "freeze_bishops", "approvals_paused", "bishops_may_approve")


def set_flags(db: Session, actor: User, changes: dict) -> list[str]:
    """King only. Every switch applies live (a few seconds) and is audited."""
    if Perm.KING_DESK not in rbac.permissions_of(actor):
        raise Forbidden("Only the King can change these.")
    changed: list[str] = []
    for key, value in changes.items():
        if key not in FLAG_DEFAULTS:
            raise ValidationError("Unknown setting.")
        if key in _BOOL_FLAGS:
            value = bool(value)
        elif key in ("request_ttl_hours", "max_pass_hours"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValidationError("Enter a whole number of hours.")
            if not 1 <= value <= 24 * 60:
                raise ValidationError("Hours must be between 1 and 1440.")
        elif key == "sealed_perms":
            valid = []
            for item in value or []:
                try:
                    perm = Perm(str(item))
                except ValueError:
                    continue
                if perm not in KING_ONLY and perm not in rbac.NON_DELEGABLE:
                    valid.append(perm.value)
            value = sorted(set(valid), key=rbac._PERM_ORDER.__getitem__)
        elif key == "sealed_groups":
            from app.core.settings_registry import GROUPS
            value = sorted({str(g) for g in (value or []) if str(g) in GROUPS})
        row = db.get(AccessFlag, key)
        current = row.value if row is not None else FLAG_DEFAULTS[key]
        if current == value:
            continue
        if row is None:
            db.add(AccessFlag(key=key, value=value, updated_by_id=actor.id))
        else:
            row.value, row.updated_by_id, row.updated_at = value, actor.id, _now()
        db.flush()
        audit.record(db, actor, "access.flag", entity_type="access_flag", entity_id=key, before={"value": current}, after={"value": value})
        changed.append(key)
    if changed:
        from sqlalchemy import event
        rbac.invalidate_role_overrides()
        event.listen(db, "after_commit", lambda _s: rbac.invalidate_role_overrides(), once=True)
    return changed


def revoke_all_passes(db: Session, actor: User) -> int:
    """King only: the panic button for passes. Every approved pass ends now."""
    if Perm.KING_DESK not in rbac.permissions_of(actor):
        raise Forbidden("Only the King can do this.")
    count = db.execute(update(AccessRequest).where(AccessRequest.status == "approved").values(status="revoked")).rowcount or 0
    audit.record(db, actor, "access.revoke_all", entity_type="access_request", entity_id="*", after={"passes": int(count)})
    from sqlalchemy import event
    rbac.invalidate_role_overrides()
    event.listen(db, "after_commit", lambda _s: rbac.invalidate_role_overrides(), once=True)
    return int(count)
