"""Cash desk: GCash load / cash-out requests, approvals and station remittance.

Fixes vs. cashin_out.php: approval was implied by a hidden Action column. Here a
request is a state machine (pending -> approved/rejected) protected by a row lock
and a unique idempotency key, so it can never be approved twice, and the money
only moves on approval.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, LockedState, NotFound, ValidationError
from app.core.money import fmt
from app.core.rbac import assert_player_in_scope, scope_ids
from app.core.settings_registry import SettingsStore
from app.db.models import (
    CashRequest,
    Notification,
    Player,
    RequestDirection,
    RequestStatus,
    Role,
    Station,
    Status,
    User,
)
from app.services import accounts, commissions, ledger, players

METHODS = ("gcash", "cash", "bank")


def create_request(
    db: Session, actor: User, *, direction: RequestDirection, amount_cents: int,
    settings: SettingsStore, player_id: int | None = None, agent_id: int | None = None,
    method: str = "gcash", reference: str | None = None, idempotency_key: str | None = None,
) -> CashRequest:
    if idempotency_key:
        existing = db.scalar(select(CashRequest).where(CashRequest.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
    if direction == RequestDirection.OUT and not settings.get("wallet.withdrawals_enabled"):
        raise LockedState("Withdrawals are switched off by an admin (emergency stop). Try again later.")
    if (player_id is None) == (agent_id is None):
        raise ValidationError("Choose exactly one subject: a player or an agent.")
    if method not in METHODS:
        raise ValidationError("Method must be gcash, cash or bank.")
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
        if agent_id != actor.id and actor.role != Role.ADMIN:
            raise Forbidden("You may only request a withdrawal for yourself.")
        target = db.get(User, agent_id)
        if target is None:
            raise NotFound("Agent not found.")
        if accounts.commission_for_user(db, target).balance_cents < amount_cents:
            raise ValidationError("Not enough unclaimed commission for that withdrawal.")

    request = CashRequest(
        player_id=player_id, agent_id=agent_id, requested_by_id=actor.id, direction=direction,
        method=method, amount_cents=amount_cents, reference=reference,
        status=RequestStatus.PENDING, idempotency_key=idempotency_key,
    )
    db.add(request)
    db.flush()
    audit.record(db, actor, "cash.request", entity_type="cash_request", entity_id=request.id,
                 after={"direction": direction, "amount_cents": amount_cents, "method": method,
                        "player_id": player_id, "agent_id": agent_id, "reference": reference})
    for admin_id in db.scalars(select(User.id).where(User.role == Role.ADMIN)).all():
        db.add(Notification(user_id=admin_id, level="info",
                            message=f"{actor.username} requested cash {direction} of {fmt(amount_cents)}",
                            link="/cashin_out"))
    db.flush()
    return request


def review(db: Session, actor: User, *, request_id: int, approve: bool, settings: SettingsStore,
           note: str | None = None) -> CashRequest:
    request = db.scalar(select(CashRequest).where(CashRequest.id == request_id).with_for_update())
    if request is None:
        raise NotFound("Request not found.")
    if request.status != RequestStatus.PENDING:
        raise Conflict(f"That request was already {request.status}.")
    if request.requested_by_id == actor.id and actor.role != Role.ADMIN:
        raise Forbidden("You cannot approve your own request.")

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
        return request

    if request.direction == RequestDirection.OUT and not settings.get("wallet.withdrawals_enabled"):
        raise LockedState("Withdrawals are switched off by an admin (emergency stop). Try again later.")

    clearing = accounts.gcash_clearing(db) if request.method == "gcash" else accounts.central_cash(db)
    if request.player_id:
        player = db.get(Player, request.player_id)
        subject = accounts.wallet_for_player(db, player)
        label = player.full_name
    else:
        agent = db.get(User, request.agent_id)
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
    if request.agent_id and request.direction == RequestDirection.OUT:
        _mark_agent_commissions_claimed(db, request.agent_id, request.amount_cents)

    request.status = RequestStatus.APPROVED
    request.reviewed_by_id = actor.id
    request.reviewed_at = dt.datetime.now(dt.timezone.utc)
    request.review_note = note
    request.txn_id = txn.id
    db.flush()
    audit.record(db, actor, "cash.request.approve", entity_type="cash_request", entity_id=request.id,
                 after={"amount_cents": request.amount_cents, "txn": txn.uuid}, reason=note)
    return request


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


def list_requests(db: Session, actor: User, *, view: str = "in", limit: int = 200) -> list[CashRequest]:
    stmt = select(CashRequest)
    if actor.role != Role.ADMIN:
        stmt = stmt.where(or_(CashRequest.requested_by_id == actor.id,
                              CashRequest.agent_id.in_(scope_ids(db, actor))))
    if view == "in":
        stmt = stmt.where(CashRequest.direction == RequestDirection.IN,
                          CashRequest.status == RequestStatus.PENDING)
    elif view == "out":
        stmt = stmt.where(CashRequest.direction == RequestDirection.OUT,
                          CashRequest.status == RequestStatus.PENDING)
    return list(db.scalars(stmt.order_by(CashRequest.id.desc()).limit(min(limit, 1000))).all())


def remit(db: Session, actor: User, *, station_id: int, amount_cents: int,
          note: str | None = None, idempotency_key: str | None = None) -> ledger.LedgerTxn:
    """Station hands cash in to the central cashier. Replaces the old
    'SUM(credit) FROM tbl_users WHERE id IN (167,168)' remit figure."""
    station = db.get(Station, station_id)
    if station is None:
        raise NotFound("Station not found.")
    if amount_cents <= 0:
        raise ValidationError("Remit amount must be positive.")
    drawer = accounts.cash_for_station(db, station)
    if drawer.balance_cents < amount_cents:
        raise ValidationError(f"{station.name} only holds {fmt(drawer.balance_cents)}.")
    txn = ledger.post(
        db, kind="station.remit",
        entries=[(drawer, -amount_cents), (accounts.central_cash(db), +amount_cents)],
        actor=actor, memo=f"Remit · {station.name}", ref_type="station", ref_id=station.id,
        idempotency_key=idempotency_key, reason=note,
    )
    audit.record(db, actor, "station.remit", entity_type="station", entity_id=station.id,
                 after={"amount_cents": amount_cents, "txn": txn.uuid}, reason=note)
    return txn
