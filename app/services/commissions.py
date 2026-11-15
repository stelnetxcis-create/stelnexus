"""Agent commissions.

Every rate is admin-editable (Settings -> Commissions); nothing is hard-coded.
The engine walks the player's upline chain, applies the tier rate, and caps the
total so the house can never pay out more commission than it earned on a fight.
Commissions are versioned per settlement run, so a redeclare cleanly reverses
them instead of double-paying.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, ValidationError
from app.core.money import bps_of, fmt
from app.core.settings_registry import SettingsStore
from app.db.models import (
    Commission,
    CommissionStatus,
    Fight,
    Role,
    SettlementRun,
    Status,
    Ticket,
    User,
)
from app.services import accounts, ledger

MAX_CHAIN = 8


@dataclass
class CommissionLine:
    ticket_id: int
    player_id: int | None
    agent_id: int
    tier: str
    rate_bps: int
    base_cents: int
    amount_cents: int


def _chain(db: Session, start_id: int | None) -> list[User]:
    chain: list[User] = []
    current = db.get(User, start_id) if start_id else None
    seen: set[int] = set()
    while current is not None and len(chain) < MAX_CHAIN:
        if current.id in seen:  # defensive: a cycle must never multiply money
            break
        seen.add(current.id)
        chain.append(current)
        current = db.get(User, current.parent_id) if current.parent_id else None
    return chain


def compute_lines(
    db: Session, tickets: list[Ticket], *, basis: str, house_gross: int, total_staked: int,
    settings: SettingsStore,
) -> list[CommissionLine]:
    if not settings.get("commission.enabled"):
        return []
    pay_refunds = bool(settings.get("commission.pay_on_refund"))
    lines: list[CommissionLine] = []
    for ticket in tickets:
        if ticket.payout_cents == 0 and ticket.status == "refunded" and not pay_refunds:
            continue
        if str(ticket.status) == "refunded" and not pay_refunds:
            continue
        if basis == "netwin":
            if house_gross <= 0 or total_staked <= 0:
                continue
            base = (house_gross * ticket.stake_cents) // total_staked
        else:
            base = ticket.stake_cents
        if base <= 0:
            continue
        for agent in _chain(db, ticket.agent_id):
            if agent.status != Status.ACTIVE:
                continue
            tier = str(agent.role)
            if tier not in ("admin", "master", "gold", "sub"):
                continue
            rate = int(settings.get(f"commission.rate_{tier}_bps"))
            if rate <= 0:
                continue
            amount = bps_of(base, rate)
            if amount > 0:
                lines.append(CommissionLine(ticket.id, ticket.player_id, agent.id, tier, rate, base, amount))

    cap_bps = int(settings.get("commission.cap_of_house_cut_bps"))
    cap = bps_of(max(0, house_gross), cap_bps)
    total = sum(line.amount_cents for line in lines)
    if total > cap and total > 0:
        # Scale down deterministically (floor), preserving order by ticket then agent.
        for line in sorted(lines, key=lambda l: (l.ticket_id, l.agent_id)):
            line.amount_cents = (line.amount_cents * cap) // total
        lines = [line for line in lines if line.amount_cents > 0]
    return lines


def persist(
    db: Session, actor: User, run: SettlementRun, fight: Fight, lines: list[CommissionLine],
    *, basis: str,
) -> ledger.LedgerTxn | None:
    if not lines:
        return None
    per_agent: dict[int, int] = {}
    for line in lines:
        db.add(Commission(
            agent_id=line.agent_id, player_id=line.player_id, ticket_id=line.ticket_id,
            fight_id=fight.id, run_id=run.id, tier=line.tier, basis=basis,
            rate_bps=line.rate_bps, base_cents=line.base_cents, amount_cents=line.amount_cents,
            status=CommissionStatus.UNCLAIMED,
        ))
        per_agent[line.agent_id] = per_agent.get(line.agent_id, 0) + line.amount_cents
    db.flush()

    total = sum(per_agent.values())
    if total <= 0:
        return None
    entries: list[tuple] = [(accounts.commission_expense(db), +total)]
    for agent_id, amount in sorted(per_agent.items()):
        agent = db.get(User, agent_id)
        entries.append((accounts.commission_for_user(db, agent), -amount))
    return ledger.post(
        db, kind="commission.accrue", entries=entries, actor=actor,
        memo=f"Commissions · game #{fight.number} v{run.version}",
        ref_type="fight", ref_id=fight.id,
        idempotency_key=f"commission:{fight.id}:{run.version}",
        meta={"run_id": run.id, "basis": basis},
    )


def mark_reversed(db: Session, run_id: int) -> int:
    rows = list(db.scalars(select(Commission).where(
        Commission.run_id == run_id, Commission.status != CommissionStatus.REVERSED)).all())
    for row in rows:
        row.status = CommissionStatus.REVERSED
    db.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

def totals_for(db: Session, agent: User) -> dict:
    earned = int(db.scalar(select(func.coalesce(func.sum(Commission.amount_cents), 0)).where(
        Commission.agent_id == agent.id, Commission.status != CommissionStatus.REVERSED)) or 0)
    payable = accounts.commission_for_user(db, agent)
    return {
        "unclaimed": payable.balance_cents,
        "earned": earned,
        "wallet": accounts.wallet_for_user(db, agent).balance_cents,
    }


def claim_to_wallet(db: Session, actor: User, *, amount_cents: int | None = None,
                    idempotency_key: str | None = None) -> ledger.LedgerTxn:
    payable = accounts.commission_for_user(db, actor)
    available = payable.balance_cents
    if available <= 0:
        raise ValidationError("You have no unclaimed commission.")
    amount = available if amount_cents is None else int(amount_cents)
    if amount <= 0 or amount > available:
        raise ValidationError(f"Amount must be between {fmt(1)} and {fmt(available)}.")

    rows = list(db.scalars(select(Commission).where(
        Commission.agent_id == actor.id, Commission.status == CommissionStatus.UNCLAIMED
    ).order_by(Commission.id)).all())
    remaining = amount
    now = dt.datetime.now(dt.timezone.utc)
    for row in rows:
        if remaining <= 0:
            break
        if row.amount_cents <= remaining:
            remaining -= row.amount_cents
            row.status = CommissionStatus.CLAIMED
            row.claimed_at = now
        # partial rows stay UNCLAIMED; the payable account is the source of truth

    wallet = accounts.wallet_for_user(db, actor)
    txn = ledger.post(
        db, kind="commission.convert",
        entries=[(payable, +amount), (wallet, -amount)],
        actor=actor, memo="Commission converted to wallet",
        ref_type="user", ref_id=actor.id, idempotency_key=idempotency_key,
    )
    audit.record(db, actor, "commission.convert", entity_type="user", entity_id=actor.id,
                 after={"amount_cents": amount, "txn": txn.uuid})
    return txn


def list_for(db: Session, actor: User, *, status: CommissionStatus | None = None,
             limit: int = 200, offset: int = 0) -> list[Commission]:
    stmt = select(Commission).where(Commission.agent_id == actor.id)
    if status:
        stmt = stmt.where(Commission.status == status)
    return list(db.scalars(stmt.order_by(Commission.id.desc())
                           .limit(min(limit, 1000)).offset(max(offset, 0))).all())
