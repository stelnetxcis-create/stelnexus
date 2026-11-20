"""Cash desk: online load / cash-out requests (GCash or bank) and their approval.

Fixes vs. cashin_out.php: approval was implied by a hidden Action column. Here a
request is a state machine (pending -> approved/rejected) protected by a row lock
and a unique idempotency key, so it can never be approved twice, and the money
only moves on approval.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.core import audit
from app.core import ttlcache
from app.core.errors import Conflict, Forbidden, LockedState, NotFound, ValidationError
from app.core.money import fmt
from app.core.rbac import assert_player_in_scope, is_top, scope_ids
from app.core.settings_registry import SettingsStore
from app.db.models import (
    CashRequest,
    Notification,
    Player,
    RequestDirection,
    RequestStatus,
    Role,
    Status,
    User,
)
from app.services import accounts, commissions, gcash, ledger, players

METHODS = ("gcash", "bank")


def create_request(
    db: Session, actor: User, *, direction: RequestDirection, amount_cents: int,
    settings: SettingsStore, player_id: int | None = None, agent_id: int | None = None,
    method: str = "gcash", reference: str | None = None, idempotency_key: str | None = None,
    target: str = "commission",
) -> CashRequest:
    """`target` only matters for an agent subject: "wallet" loads / cashes out the agent's own
    credit (the Wallet page); "commission" is the original commission withdrawal."""
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
    if idempotency_key:
        existing = db.scalar(select(CashRequest).where(CashRequest.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
    if direction == RequestDirection.OUT and not settings.get("wallet.withdrawals_enabled"):
        raise LockedState("Withdrawals are switched off by an admin (emergency stop). Try again later.")
    if (player_id is None) == (agent_id is None):
        raise ValidationError("Choose exactly one subject: a player or an agent.")
    if target not in ("commission", "wallet"):
        raise ValidationError("Unknown request target.")
    if method not in METHODS:
        raise ValidationError("Method must be GCash or bank transfer.")
    minimum = int(settings.get("wallet.min_cash_cents"))
    maximum = int(settings.get("wallet.max_cash_cents"))
    if amount_cents < minimum or amount_cents > maximum:
        raise ValidationError(f"Amount must be between {fmt(minimum)} and {fmt(maximum)}.")
    reference = (reference or "").strip()[:64] or None
    if method == "gcash" and direction == RequestDirection.IN and not reference:
        raise ValidationError("A GCash reference number is required for a load request.")

    player = None
    if player_id is not None:
        player = players.get_player(db, actor, player_id)
        if player.status != Status.ACTIVE:
            raise ValidationError("That player is not active.")
        if direction == RequestDirection.OUT:
            if players.wallet(db, player).balance_cents < amount_cents:
                raise ValidationError("Player does not have enough balance for that cash-out.")
    else:
        if agent_id != actor.id and (target == "wallet" or not is_top(actor)):
            raise Forbidden("You may only request a load or withdrawal for yourself.")
        subject_user = db.get(User, agent_id)
        if subject_user is None:
            raise NotFound("Agent not found.")
        if not subject_user.has_wallet:
            raise Forbidden(f"A {subject_user.role_label} account has no balance, so it has nothing to load or cash out.")
        if direction == RequestDirection.OUT:
            if target == "wallet":
                if accounts.wallet_for_user(db, subject_user).balance_cents < amount_cents:
                    raise ValidationError("You do not have enough credit for that cash-out.")
            elif accounts.commission_for_user(db, subject_user).balance_cents < amount_cents:
                raise ValidationError("Not enough unclaimed commission for that withdrawal.")

    request = CashRequest(
        player_id=player_id, agent_id=agent_id, requested_by_id=actor.id, direction=direction,
        method=method, amount_cents=amount_cents, reference=reference,
        status=RequestStatus.PENDING, idempotency_key=idempotency_key,
        target=target if agent_id is not None else "commission",
    )
    db.add(request)
    db.flush()
    routed = gcash.route_request(db, request, settings)   # pooled GCash: pick the number with room
    audit.record(db, actor, "cash.request", entity_type="cash_request", entity_id=request.id,
                 after={"direction": direction, "amount_cents": amount_cents, "method": method,
                        "player_id": player_id, "agent_id": agent_id, "reference": reference,
                        "target": request.target,
                        "gcash_account_id": routed.id if routed else None})
    for admin_id in db.scalars(select(User.id).where(User.role.in_([Role.ADMIN, Role.MASTER, Role.TELLER]),
                                                      User.id != actor.id)).all():
        db.add(Notification(user_id=admin_id, level="info",
                            message=f"{actor.username} requested cash {direction} of {fmt(amount_cents)}",
                            link="/wallet?t=approvals"))
    db.flush()
    return request


def review(db: Session, actor: User, *, request_id: int, approve: bool, settings: SettingsStore,
           note: str | None = None) -> CashRequest:
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
    request = db.scalar(select(CashRequest).where(CashRequest.id == request_id).with_for_update())
    if request is None:
        raise NotFound("Request not found.")
    if request.status != RequestStatus.PENDING:
        raise Conflict(f"That request was already {request.status}.")
    if request.requested_by_id == actor.id and not is_top(actor):
        raise Forbidden("You cannot approve your own request.")
    if actor.role == Role.TELLER:
        if not gcash.has_active_number(db, actor):
            raise Forbidden("A Teller needs an active GCash number before approving requests.")
        gcash.assert_can_review(db, actor, request)
    if (request.requested_by_id == actor.id and request.agent_id is not None
            and request.target == "wallet" and actor.role != Role.ADMIN):
        # Nobody tops up or cashes out their own credit by themselves; the Admin is the checker.
        raise Forbidden("You cannot approve a request for your own credit. Ask the Admin.")

    note = (note or "").strip()[:255] or None
    if not approve and not note:
        raise ValidationError("A note is required when rejecting a request.")

    if not approve:
        request.status = RequestStatus.REJECTED
        request.reviewed_by_id = actor.id
        request.reviewed_at = dt.datetime.now(dt.timezone.utc)
        request.review_note = note
        db.flush()
        audit.record(db, actor, "cash.request.reject", entity_type="cash_request",
                     entity_id=request.id, reason=note)
        _tell_requester(db, request, f"Your {request.direction.value} request of {fmt(request.amount_cents)} was rejected: {note}")
        return request

    if request.direction == RequestDirection.OUT and not settings.get("wallet.withdrawals_enabled"):
        raise LockedState("Withdrawals are switched off by an admin (emergency stop). Try again later.")

    clearing = accounts.gcash_clearing(db)   # online payments clearing (GCash and bank)
    if request.player_id:
        player = db.get(Player, request.player_id)
        subject = accounts.wallet_for_player(db, player)
        label = player.full_name
    else:
        agent = db.get(User, request.agent_id)
        if request.target == "wallet":
            subject = accounts.wallet_for_user(db, agent)
            label = f"{agent.username} (credit)"
        else:
            subject = accounts.commission_for_user(db, agent)
            label = agent.username

    if request.direction == RequestDirection.IN:
        entries = [(clearing, +request.amount_cents), (subject, -request.amount_cents)]
    else:
        entries = [(subject, +request.amount_cents), (clearing, -request.amount_cents)]

    txn = ledger.post(
        db, kind=f"request.{request.direction}", entries=entries, actor=actor,
        memo=f"{request.method.upper()} {request.direction} · {label}",
        ref_type="cash_request", ref_id=request.id,
        idempotency_key=f"cashreq:{request.id}", reason=note,
    )
    if request.agent_id and request.direction == RequestDirection.OUT and request.target != "wallet":
        _mark_agent_commissions_claimed(db, request.agent_id, request.amount_cents)

    request.status = RequestStatus.APPROVED
    request.reviewed_by_id = actor.id
    request.reviewed_at = dt.datetime.now(dt.timezone.utc)
    request.review_note = note
    request.txn_id = txn.id
    db.flush()
    audit.record(db, actor, "cash.request.approve", entity_type="cash_request", entity_id=request.id,
                 after={"amount_cents": request.amount_cents, "txn": txn.uuid}, reason=note)
    _tell_requester(db, request, f"Your {request.direction.value} request of {fmt(request.amount_cents)} was approved.")
    return request


def _tell_requester(db: Session, request: CashRequest, message: str) -> None:
    if request.requested_by_id:
        db.add(Notification(user_id=request.requested_by_id, level="info", message=message[:255],
                            link="/wallet?t=history"))
        db.flush()


def _mark_agent_commissions_claimed(db: Session, agent_id: int, amount_cents: int) -> None:
    from app.db.models import Commission, CommissionStatus
    remaining = amount_cents
    rows = db.scalars(select(Commission).where(
        Commission.agent_id == agent_id, Commission.status == CommissionStatus.UNCLAIMED
    ).order_by(Commission.id)).all()
    now = dt.datetime.now(dt.timezone.utc)
    for row in rows:
        if remaining <= 0:
            break
        if row.amount_cents <= remaining:
            remaining -= row.amount_cents
            row.status = CommissionStatus.CLAIMED
            row.claimed_at = now
    db.flush()


# Request rows are rendered with their player / agent / requester / GCash number: load them with
# the list (joined many-to-one) rather than one lazy SELECT per relationship per row.
_REQ_LOAD = (joinedload(CashRequest.player), joinedload(CashRequest.agent),
             joinedload(CashRequest.requester), joinedload(CashRequest.gcash_account))


def pending_count(db: Session, actor: User) -> int:
    """Requests waiting for review that `actor` could act on (shown as a dot on the wallet)."""
    from sqlalchemy import func
    stmt = select(func.count(CashRequest.id)).where(CashRequest.status == RequestStatus.PENDING)
    if actor.role != Role.ADMIN:
        stmt = stmt.where(CashRequest.requested_by_id != actor.id)
    stmt = gcash.visible_to(stmt, actor)
    return int(db.scalar(stmt) or 0)


def my_requests(db: Session, user: User, *, limit: int = 50) -> list[CashRequest]:
    """The signed-in account's own requests: for itself, its commission, or one of its players."""
    stmt = select(CashRequest).where(or_(CashRequest.requested_by_id == user.id,
                                         CashRequest.agent_id == user.id))
    return list(db.scalars(stmt.options(*_REQ_LOAD).order_by(CashRequest.id.desc())
                           .limit(min(limit, 200))).all())


def list_requests(db: Session, actor: User, *, view: str = "in", limit: int = 200) -> list[CashRequest]:
    stmt = select(CashRequest)
    if not (is_top(actor) or actor.role == Role.TELLER):
        stmt = stmt.where(or_(CashRequest.requested_by_id == actor.id,
                              CashRequest.agent_id.in_(scope_ids(db, actor))))
    if view == "in":
        stmt = stmt.where(CashRequest.direction == RequestDirection.IN,
                          CashRequest.status == RequestStatus.PENDING)
    elif view == "out":
        stmt = stmt.where(CashRequest.direction == RequestDirection.OUT,
                          CashRequest.status == RequestStatus.PENDING)
    stmt = gcash.visible_to(stmt, actor)
    return list(db.scalars(stmt.options(*_REQ_LOAD).order_by(CashRequest.id.desc())
                           .limit(min(limit, 1000))).all())
