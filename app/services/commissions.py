"""Network commissions: the Master / Cavo / Kubrador cut system.

How the money is split (per ticket, then summed per fight)
----------------------------------------------------------
    pool         = basis * pool%                      basis = the ticket's share of the house win
                                                      (or its stake, if basis = turnover)
    cavo_gross   = pool * Volume %                    the Cavo side's cut          (default 30%)
    kubrador     = cavo_gross * SOP %                 taken OUT of the Cavo's cut  (default 20% of it)
    cavo         = cavo_gross - kubrador
    master       = pool - cavo_gross                  "Management": whatever the Cavo side does not keep

Because the Master's line is defined as the remainder, master + cavo + kubrador always equals the
pool to the centavo; integer rounding can never leak or invent money. Volume % and SOP % are
per-account (Cuts page, Master/Admin only) and fall back to Settings -> Commissions.

Edge rules: a suspended Cavo/Kubrador earns nothing and their share rolls up to the next account
above; a bet with no Master in its chain leaves that share with the house; walk-in tickets (no
agent) pay no commission. Admin is a hidden account and never earns.

Every line stores a snapshot of the numbers it was computed from (``Commission.calc``), and each
settlement writes one ``commission.split`` audit row, so any payout can be re-derived by hand.
Commissions are versioned per settlement run, so a redeclare cleanly reverses them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import ValidationError
from app.core.money import bps_of, fmt
from app.core.settings_registry import SettingsStore
from app.db.models import (
    BolaCommission,
    BolaDraw,
    BolaRun,
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
TIERS = ("master", "cavo", "kubrador")


# ---------------------------------------------------------------------------
# The formula (pure functions: no database, trivially testable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Split:
    pool: int
    master: int
    cavo: int
    kubrador: int
    cavo_gross: int          # what the Cavo side kept before paying the Kubrador

    @property
    def paid(self) -> int:
        return self.master + self.cavo + self.kubrador


def split_pool(pool: int, *, cavo_bps: int, sop_bps: int, has_cavo: bool = True,
               has_kubrador: bool = True, has_master: bool = True) -> Split:
    """Waterfall split of one pool. See the module docstring.

    ``has_*`` say whether that account exists *and is active*; a missing one earns nothing and
    its share rolls up. If there is no Master the Master line is 0 (the house keeps it).
    """
    pool = max(0, int(pool))
    cavo_gross = bps_of(pool, cavo_bps) if has_cavo else 0
    kubrador = bps_of(cavo_gross, sop_bps) if (has_cavo and has_kubrador) else 0
    cavo = cavo_gross - kubrador
    master = (pool - cavo_gross) if has_master else 0
    return Split(pool=pool, master=master, cavo=cavo, kubrador=kubrador, cavo_gross=cavo_gross)


def effective_cavo_bps(cavo: User | None, store: SettingsStore) -> int:
    """Volume %: the Cavo's own value, else the global default."""
    if cavo is not None and cavo.cut_bps is not None:
        return int(cavo.cut_bps)
    return int(store.get("commission.cavo_cut_bps"))


def effective_sop_bps(kubrador: User | None, cavo: User | None, store: SettingsStore) -> int:
    """SOP %: the Kubrador's personal override, else its Cavo's SOP %, else the global default."""
    if kubrador is not None and kubrador.cut_bps is not None:
        return int(kubrador.cut_bps)
    if cavo is not None and cavo.sop_bps is not None:
        return int(cavo.sop_bps)
    return int(store.get("commission.kubrador_cut_bps"))


def preview(store: SettingsStore, *, cavo: User | None, kubrador: User | None = None,
            pool: int = 100_000) -> Split:
    """What a pool (default PHP 1,000) would split into for this Cavo / Kubrador, as configured."""
    return split_pool(pool, cavo_bps=effective_cavo_bps(cavo, store),
                      sop_bps=effective_sop_bps(kubrador, cavo, store),
                      has_cavo=cavo is not None, has_kubrador=kubrador is not None)


# ---------------------------------------------------------------------------
# Settlement integration
# ---------------------------------------------------------------------------

@dataclass
class CommissionLine:
    ticket_id: int
    player_id: int | None
    agent_id: int
    tier: str
    rate_bps: int
    base_cents: int
    amount_cents: int
    calc: dict = field(default_factory=dict)


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


def _first(chain: list[User], role: Role) -> User | None:
    return next((u for u in chain if u.role == role), None)


def _active(user: User | None) -> bool:
    return user is not None and user.status == Status.ACTIVE


def compute_lines(
    db: Session, tickets: list[Ticket], *, basis: str, house_gross: int, total_staked: int,
    settings: SettingsStore, pool_key: str = "commission.pool_bps", pay_master: bool = True,
) -> list[CommissionLine]:
    if not settings.get("commission.enabled"):
        return []
    pay_refunds = bool(settings.get("commission.pay_on_refund"))
    pool_bps = int(settings.get(pool_key))
    lines: list[CommissionLine] = []
    for ticket in tickets:
        if str(ticket.status) == "refunded" and not pay_refunds:
            continue
        if ticket.agent_id is None:
            continue                      # walk-in / counter ticket: no network, no commission
        if basis == "netwin":
            if house_gross <= 0 or total_staked <= 0:
                continue
            base = (house_gross * ticket.stake_cents) // total_staked
        else:
            base = ticket.stake_cents
        pool = bps_of(base, pool_bps)
        if pool <= 0:
            continue

        chain = _chain(db, ticket.agent_id)
        kubrador, cavo, master = (_first(chain, Role.KUBRADOR), _first(chain, Role.CAVO),
                                  _first(chain, Role.MASTER))
        cavo_bps = effective_cavo_bps(cavo, settings)
        sop_bps = effective_sop_bps(kubrador, cavo, settings)
        split = split_pool(pool, cavo_bps=cavo_bps, sop_bps=sop_bps,
                           has_cavo=_active(cavo), has_kubrador=_active(kubrador),
                           has_master=_active(master) and pay_master)
        calc = {
            "formula": "pool=basis*pool%; cavo_gross=pool*volume%; kubrador=cavo_gross*sop%; "
                       "cavo=cavo_gross-kubrador; master=pool-cavo_gross",
            "basis": basis, "base_cents": base, "pool_bps": pool_bps, "pool_cents": pool,
            "volume_bps": cavo_bps, "sop_bps": sop_bps, "cavo_gross_cents": split.cavo_gross,
            "chain": [{"id": u.id, "u": u.username, "role": str(u.role), "active": _active(u)}
                      for u in chain if u.role != Role.ADMIN],
        }
        for tier, account, amount, rate in (
            ("master", master, split.master, 10000 - cavo_bps if _active(cavo) else 10000),
            ("cavo", cavo, split.cavo, cavo_bps),
            ("kubrador", kubrador, split.kubrador, sop_bps),
        ):
            if account is not None and amount > 0:
                lines.append(CommissionLine(ticket.id, ticket.player_id, account.id, tier, rate,
                                            base, amount, calc))

    cap_bps = int(settings.get("commission.cap_of_house_cut_bps"))
    cap = bps_of(max(0, house_gross), cap_bps)
    total = sum(line.amount_cents for line in lines)
    if total > cap and total > 0:
        # Scale down deterministically (floor), preserving order by ticket then agent.
        for line in sorted(lines, key=lambda l: (l.ticket_id, l.agent_id)):
            line.calc = {**line.calc, "capped": True, "cap_cents": cap, "uncapped_cents": line.amount_cents}
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
    per_tier = {t: 0 for t in TIERS}
    for line in lines:
        db.add(Commission(
            agent_id=line.agent_id, player_id=line.player_id, ticket_id=line.ticket_id,
            fight_id=fight.id, run_id=run.id, tier=line.tier, basis=basis,
            rate_bps=line.rate_bps, base_cents=line.base_cents, amount_cents=line.amount_cents,
            calc=line.calc, status=CommissionStatus.UNCLAIMED,
        ))
        per_agent[line.agent_id] = per_agent.get(line.agent_id, 0) + line.amount_cents
        per_tier[line.tier] = per_tier.get(line.tier, 0) + line.amount_cents
    db.flush()

    total = sum(per_agent.values())
    if total <= 0:
        return None
    entries: list[tuple] = [(accounts.commission_expense(db), +total)]
    for agent_id, amount in sorted(per_agent.items()):
        agent = db.get(User, agent_id)
        entries.append((accounts.commission_for_user(db, agent), -amount))
    txn = ledger.post(
        db, kind="commission.accrue", entries=entries, actor=actor,
        memo=f"Commissions · fight #{fight.number} v{run.version}",
        ref_type="fight", ref_id=fight.id,
        idempotency_key=f"commission:{fight.id}:{run.version}",
        meta={"run_id": run.id, "basis": basis, "per_tier": per_tier},
    )
    audit.record(db, actor, "commission.split", entity_type="fight", entity_id=fight.id,
                 after={"version": run.version, "basis": basis, "tickets": len({l.ticket_id for l in lines}),
                        "total_cents": total, **{f"{t}_cents": v for t, v in per_tier.items()},
                        "txn": txn.uuid})
    return txn


def mark_reversed(db: Session, run_id: int) -> int:
    rows = list(db.scalars(select(Commission).where(
        Commission.run_id == run_id, Commission.status != CommissionStatus.REVERSED)).all())
    for row in rows:
        row.status = CommissionStatus.REVERSED
    db.flush()
    return len(rows)



# ---------------------------------------------------------------------------
# Bola (number game) commissions
# ---------------------------------------------------------------------------
# Same waterfall as sabong (split_pool / effective_* are reused unchanged); only the pool share
# comes from ``bola.commission.pool_bps`` and the rows live in ``bola_commissions``.
# Plan D3: SOP is paid OUT of the Cavo's cut. Plan D4: by default the Master's remainder of the
# pool stays with the house; ``bola.commission.pay_master`` turns it into a commission line.

def compute_bola_lines(
    db: Session, tickets: list, *, basis: str, house_gross: int, total_staked: int,
    settings: SettingsStore,
) -> list[CommissionLine]:
    """Commission lines for a settled Bola draw. ``tickets`` are BolaTicket rows."""
    return compute_lines(db, tickets, basis=basis, house_gross=house_gross,
                         total_staked=total_staked, settings=settings,
                         pool_key="bola.commission.pool_bps",
                         pay_master=bool(settings.get("bola.commission.pay_master")))


def persist_bola(
    db: Session, actor: User, run: BolaRun, draw: BolaDraw, lines: list[CommissionLine],
    *, basis: str,
) -> ledger.LedgerTxn | None:
    if not lines:
        return None
    per_agent: dict[int, int] = {}
    per_tier = {t: 0 for t in TIERS}
    for line in lines:
        db.add(BolaCommission(
            agent_id=line.agent_id, player_id=line.player_id, ticket_id=line.ticket_id,
            draw_id=draw.id, run_id=run.id, tier=line.tier, basis=basis,
            rate_bps=line.rate_bps, base_cents=line.base_cents, amount_cents=line.amount_cents,
            calc=line.calc, status=CommissionStatus.UNCLAIMED,
        ))
        per_agent[line.agent_id] = per_agent.get(line.agent_id, 0) + line.amount_cents
        per_tier[line.tier] = per_tier.get(line.tier, 0) + line.amount_cents
    db.flush()

    total = sum(per_agent.values())
    if total <= 0:
        return None
    entries: list[tuple] = [(accounts.commission_expense(db), +total)]
    for agent_id, amount in sorted(per_agent.items()):
        entries.append((accounts.commission_for_user(db, db.get(User, agent_id)), -amount))
    txn = ledger.post(
        db, kind="commission.accrue", entries=entries, actor=actor,
        memo=f"Bola commissions · draw #{draw.id} v{run.version}",
        ref_type="bola_draw", ref_id=draw.id,
        idempotency_key=f"bola-commission:{draw.id}:{run.version}",
        meta={"run_id": run.id, "basis": basis, "per_tier": per_tier, "game": "bola"},
    )
    audit.record(db, actor, "bola.commission.split", entity_type="bola_draw", entity_id=draw.id,
                 after={"version": run.version, "basis": basis,
                        "tickets": len({l.ticket_id for l in lines}), "total_cents": total,
                        **{f"{t}_cents": v for t, v in per_tier.items()}, "txn": txn.uuid})
    return txn


def mark_bola_reversed(db: Session, run_id: int) -> int:
    rows = list(db.scalars(select(BolaCommission).where(
        BolaCommission.run_id == run_id,
        BolaCommission.status != CommissionStatus.REVERSED)).all())
    for row in rows:
        row.status = CommissionStatus.REVERSED
    db.flush()
    return len(rows)


def list_bola_for(db: Session, actor: User, *, status: CommissionStatus | None = None,
                  limit: int = 200, offset: int = 0) -> list[BolaCommission]:
    stmt = select(BolaCommission).where(BolaCommission.agent_id == actor.id)
    if status:
        stmt = stmt.where(BolaCommission.status == status)
    return list(db.scalars(stmt.order_by(BolaCommission.id.desc())
                           .limit(min(limit, 1000)).offset(max(offset, 0))).all())


# ---------------------------------------------------------------------------
# Cut management (Master / Admin)
# ---------------------------------------------------------------------------

def _parse_percent(raw: str | None, *, label: str) -> int | None:
    """'30' / '30.5' / '' -> bps / None (None = inherit the default)."""
    from decimal import Decimal, InvalidOperation
    text = (raw or "").strip().replace("%", "")
    if text == "":
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationError(f"{label}: enter a percentage like 30 or 12.5.") from exc
    if value < 0 or value > 100:
        raise ValidationError(f"{label}: must be between 0 and 100.")
    return int((value * 100).quantize(Decimal(1)))


def set_cut(db: Session, actor: User, target: User, *, volume: str | None = None,
            sop: str | None = None, settings: SettingsStore, reason: str | None = None) -> User:
    """Change a Cavo's Volume % / SOP % or a Kubrador's personal SOP %. Audited with before/after.

    Master is held to the guard rails in Settings; Admin is not. ``None`` leaves a field alone,
    an empty string resets it to the default."""
    from app.core.errors import Forbidden
    from app.core.rbac import assert_user_in_scope, Perm, has_perm

    if not has_perm(actor, Perm.CUTS_MANAGE):
        raise Forbidden("Your role cannot change cuts.")
    target = assert_user_in_scope(db, actor, target.id)
    if target.role not in (Role.CAVO, Role.KUBRADOR):
        raise ValidationError("Only a Cavo or a Kubrador has a cut.")

    unlimited = actor.role == Role.ADMIN
    before = {"cut_bps": target.cut_bps, "sop_bps": target.sop_bps}

    if target.role == Role.CAVO:
        if volume is not None:
            bps = _parse_percent(volume, label="Volume %")
            limit = int(settings.get("commission.max_cavo_cut_bps"))
            if bps is not None and not unlimited and bps > limit:
                raise ValidationError(f"Volume % cannot be more than {limit / 100:g}% (set by Admin).")
            target.cut_bps = bps
        if sop is not None:
            bps = _parse_percent(sop, label="SOP %")
            limit = int(settings.get("commission.max_kubrador_cut_bps"))
            if bps is not None and not unlimited and bps > limit:
                raise ValidationError(f"SOP % cannot be more than {limit / 100:g}% (set by Admin).")
            target.sop_bps = bps
    else:  # kubrador: only a personal SOP override
        if sop is not None:
            bps = _parse_percent(sop, label="SOP %")
            limit = int(settings.get("commission.max_kubrador_cut_bps"))
            if bps is not None and not unlimited and bps > limit:
                raise ValidationError(f"SOP % cannot be more than {limit / 100:g}% (set by Admin).")
            target.cut_bps = bps
    db.flush()
    after = {"cut_bps": target.cut_bps, "sop_bps": target.sop_bps}
    if before != after:
        audit.record(db, actor, "cut.update", entity_type="user", entity_id=target.id,
                     before=before, after={**after, "username": target.username,
                                           "role": str(target.role)}, reason=reason)
    return target


def cut_rows(db: Session, actor: User, store: SettingsStore) -> list[dict]:
    """Cavos with their Kubradors and the effective cut of each, for the Cuts page."""
    from app.core.rbac import scope_ids
    ids = scope_ids(db, actor)
    cavos = list(db.scalars(select(User).where(User.role == Role.CAVO, User.id.in_(ids))
                            .order_by(User.username)).all())
    rows = []
    for cavo in cavos:
        subs = list(db.scalars(select(User).where(User.role == Role.KUBRADOR, User.parent_id == cavo.id)
                               .order_by(User.username)).all())
        rows.append({
            "cavo": cavo,
            "volume_bps": effective_cavo_bps(cavo, store),
            "volume_is_default": cavo.cut_bps is None,
            "sop_bps": effective_sop_bps(None, cavo, store),
            "sop_is_default": cavo.sop_bps is None,
            "preview": preview(store, cavo=cavo),
            "kubradors": [{
                "user": k,
                "sop_bps": effective_sop_bps(k, cavo, store),
                "is_override": k.cut_bps is not None,
                "preview": preview(store, cavo=cavo, kubrador=k),
            } for k in subs],
        })
    return rows


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

def totals_for(db: Session, agent: User) -> dict:
    earned = int(db.scalar(select(func.coalesce(func.sum(Commission.amount_cents), 0)).where(
        Commission.agent_id == agent.id, Commission.status != CommissionStatus.REVERSED)) or 0)
    earned += int(db.scalar(select(func.coalesce(func.sum(BolaCommission.amount_cents), 0)).where(
        BolaCommission.agent_id == agent.id,
        BolaCommission.status != CommissionStatus.REVERSED)) or 0)
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
    rows += list(db.scalars(select(BolaCommission).where(
        BolaCommission.agent_id == actor.id, BolaCommission.status == CommissionStatus.UNCLAIMED
    ).order_by(BolaCommission.id)).all())
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
