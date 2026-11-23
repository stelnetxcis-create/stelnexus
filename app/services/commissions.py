"""Network commissions: the Master Agent / Agent / Sub Agent cut system.

How the money is split (per ticket, then summed per game)
---------------------------------------------------------
    pool        = basis * pool%                   basis = the ticket's share of the house win
                                                  (or its stake, if basis = turnover)
    agent_gross = pool * Volume %                 the Agent side's cut           (default 30%)
    sub_agent   = agent_gross * SOP %             taken OUT of the Agent's cut   (default 30% of it)
    agent       = agent_gross - sub_agent
    master      = pool - agent_gross              "Management": whatever the Agent side does not keep

Out of the box that is Master 70% / Agent 21% / Sub Agent 9% of the pool: the Master keeps 70, the
Agent side gets 30, and the Sub Agent gets 30% of that 30.

Because the Master's line is defined as the remainder, master + agent + sub_agent always equals the
pool to the centavo; integer rounding can never leak or invent money.

Each game has its own calculation: Meron / Wala and Bola each have their own basis, pool % and
default Volume % / SOP % (Settings -> Commissions / Bola). A cut typed on one account (Cuts page)
beats the game default and applies to every game.

Edge rules: a suspended Agent/Sub Agent earns nothing and their share rolls up to the next account
above; a bet with no Master in its chain leaves that share with the house; a player sitting directly
under an Agent has no Sub Agent, so the Agent keeps the whole Agent cut; walk-in tickets (no agent)
pay no commission. Admin is a hidden account and never earns.

Every line stores a snapshot of the numbers it was computed from (``Commission.calc``), and each
settlement writes one ``commission.split`` audit row, so any payout can be re-derived by hand.
Commissions are versioned per settlement run, so a redeclare cleanly reverses them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core.errors import ValidationError
from app.core.money import bps_of, fmt
from app.core.settings_registry import SettingsStore
from app.db.models import (
    BolaCommission,
    BolaDraw,
    BolaRun,
    BolaTicket,
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

MANILA = dt.timezone(dt.timedelta(hours=8))   # Asia/Manila has no DST


def manila_bounds(start: dt.date | None, end: dt.date | None) -> tuple[dt.datetime | None, dt.datetime | None]:
    """[start 00:00, end+1 00:00) in Manila time, as UTC datetimes, for filtering by game day."""
    lo = dt.datetime.combine(start, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc) if start else None
    hi = (dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA)
          .astimezone(dt.timezone.utc)) if end else None
    return lo, hi


MAX_CHAIN = 8
TIERS = ("master", "agent", "sub_agent")
GAMES = ("sabong", "bola")
# Per-game default cuts (Volume %, SOP %). The Meron / Wala keys keep their old names so values the
# Admin already saved carry over.
_DEFAULT_KEYS = {
    "sabong": ("commission.cavo_cut_bps", "commission.kubrador_cut_bps"),
    "bola": ("bola.commission.agent_cut_bps", "bola.commission.sub_agent_cut_bps"),
}


# ---------------------------------------------------------------------------
# The formula (pure functions: no database, trivially testable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Split:
    pool: int
    master: int
    agent: int
    sub_agent: int
    agent_gross: int         # what the Agent side kept before paying the Sub Agent

    @property
    def paid(self) -> int:
        return self.master + self.agent + self.sub_agent


def split_pool(pool: int, *, agent_bps: int, sub_bps: int, has_agent: bool = True,
               has_sub: bool = True, has_master: bool = True) -> Split:
    """Waterfall split of one pool. See the module docstring.

    ``has_*`` say whether that account exists *and is active*; a missing one earns nothing and
    its share rolls up. If there is no Master the Master line is 0 (the house keeps it).
    """
    pool = max(0, int(pool))
    agent_gross = bps_of(pool, agent_bps) if has_agent else 0
    sub_agent = bps_of(agent_gross, sub_bps) if (has_agent and has_sub) else 0
    agent = agent_gross - sub_agent
    master = (pool - agent_gross) if has_master else 0
    return Split(pool=pool, master=master, agent=agent, sub_agent=sub_agent, agent_gross=agent_gross)


def effective_agent_bps(agent: User | None, store: SettingsStore, game: str = "sabong") -> int:
    """Volume %: the Agent's own value, else the game's default."""
    if agent is not None and agent.cut_bps is not None:
        return int(agent.cut_bps)
    return int(store.get(_DEFAULT_KEYS[game][0]))


def effective_sub_bps(sub: User | None, agent: User | None, store: SettingsStore,
                      game: str = "sabong") -> int:
    """SOP %: the Sub Agent's personal override, else its Agent's SOP %, else the game's default."""
    if sub is not None and sub.cut_bps is not None:
        return int(sub.cut_bps)
    if agent is not None and agent.sop_bps is not None:
        return int(agent.sop_bps)
    return int(store.get(_DEFAULT_KEYS[game][1]))


def preview(store: SettingsStore, *, agent: User | None, sub: User | None = None,
            pool: int = 100_000, game: str = "sabong") -> Split:
    """What a pool (default PHP 1,000) would split into for this Agent / Sub Agent, as configured."""
    return split_pool(pool, agent_bps=effective_agent_bps(agent, store, game),
                      sub_bps=effective_sub_bps(sub, agent, store, game),
                      has_agent=agent is not None, has_sub=sub is not None)


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
    game: str = "sabong",
) -> list[CommissionLine]:
    """Commission lines for the settled tickets of one game. ``game`` picks which default Volume % /
    SOP % apply. ``pay_master`` False leaves the Master's remainder with the house (Bola plan D4)."""
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
        if getattr(ticket, "user_id", None):   # a self-bet: the bettor is the player, only their upline earns
            chain = [u for u in chain if u.id != ticket.user_id]
        sub, agent, master = (_first(chain, Role.SUB_AGENT), _first(chain, Role.AGENT),
                              _first(chain, Role.MASTER))
        agent_bps = effective_agent_bps(agent, settings, game)
        sub_bps = effective_sub_bps(sub, agent, settings, game)
        split = split_pool(pool, agent_bps=agent_bps, sub_bps=sub_bps,
                           has_agent=_active(agent), has_sub=_active(sub),
                           has_master=_active(master) and pay_master)
        calc = {
            "formula": "pool=basis*pool%; agent_gross=pool*volume%; sub_agent=agent_gross*sop%; "
                       "agent=agent_gross-sub_agent; master=pool-agent_gross",
            "game": game,
            "basis": basis, "base_cents": base, "pool_bps": pool_bps, "pool_cents": pool,
            "volume_bps": agent_bps, "sop_bps": sub_bps, "agent_gross_cents": split.agent_gross,
            "chain": [{"id": u.id, "u": u.username, "role": str(u.role), "active": _active(u)}
                      for u in chain if u.role not in (Role.ADMIN, Role.BISHOP)],
        }
        for tier, account, amount, rate in (
            ("master", master, split.master, 10000 - agent_bps if _active(agent) else 10000),
            ("agent", agent, split.agent, agent_bps),
            ("sub_agent", sub, split.sub_agent, sub_bps),
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
# Same waterfall as Meron / Wala (split_pool / effective_* are shared); the game has its own basis,
# pool % and default cuts (``bola.commission.*``) and the rows live in ``bola_commissions``.
# SOP is paid OUT of the Agent's cut. Plan D4: by default the Master's remainder of the pool stays
# with the house; ``bola.commission.pay_master`` turns it into a commission line.

def compute_bola_lines(
    db: Session, tickets: list, *, basis: str, house_gross: int, total_staked: int,
    settings: SettingsStore,
) -> list[CommissionLine]:
    """Commission lines for a settled Bola draw. ``tickets`` are BolaTicket rows."""
    return compute_lines(db, tickets, basis=basis, house_gross=house_gross,
                         total_staked=total_staked, settings=settings,
                         pool_key="bola.commission.pool_bps",
                         pay_master=bool(settings.get("bola.commission.pay_master")),
                         game="bola")


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
                           .limit(clamp_limit(limit, 1000)).offset(clamp_offset(offset))).all())


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
    """Change an Agent's Volume % / SOP % or a Sub Agent's personal SOP %. Audited with before/after.

    Master is held to the guard rails in Settings; Admin is not. ``None`` leaves a field alone,
    an empty string resets it to the game defaults."""
    from app.core.errors import Forbidden
    from app.core.rbac import assert_user_in_scope, Perm, has_perm

    if not has_perm(actor, Perm.CUTS_MANAGE):
        raise Forbidden("Your role cannot change cuts.")
    target = assert_user_in_scope(db, actor, target.id)
    if target.role not in (Role.AGENT, Role.SUB_AGENT):
        raise ValidationError("Only an Agent or a Sub Agent has a cut.")

    unlimited = actor.is_admin_tier
    before = {"cut_bps": target.cut_bps, "sop_bps": target.sop_bps}

    def _checked(raw: str | None, label: str, limit_key: str) -> int | None:
        bps = _parse_percent(raw, label=label)
        limit = int(settings.get(limit_key))
        if bps is not None and not unlimited and bps > limit:
            raise ValidationError(f"{label} cannot be more than {limit / 100:g}% (set by Admin).")
        return bps

    if target.role == Role.AGENT:
        if volume is not None:
            target.cut_bps = _checked(volume, "Volume %", "commission.max_cavo_cut_bps")
        if sop is not None:
            target.sop_bps = _checked(sop, "SOP %", "commission.max_kubrador_cut_bps")
    else:  # sub agent: only a personal SOP override
        if sop is not None:
            target.cut_bps = _checked(sop, "SOP %", "commission.max_kubrador_cut_bps")
    db.flush()
    after = {"cut_bps": target.cut_bps, "sop_bps": target.sop_bps}
    if before != after:
        audit.record(db, actor, "cut.update", entity_type="user", entity_id=target.id,
                     before=before, after={**after, "username": target.username,
                                           "role": str(target.role)}, reason=reason)
    return target


def cut_rows(db: Session, actor: User, store: SettingsStore) -> list[dict]:
    """Agents with their Sub Agents and the effective cut of each, for the Cuts page. Each row
    carries a Meron / Wala preview and a Bola preview, since the game defaults can differ."""
    from app.core.rbac import scope_ids
    ids = scope_ids(db, actor)
    agents = list(db.scalars(select(User).where(User.role == Role.AGENT, User.id.in_(ids))
                             .order_by(User.username)).all())
    rows = []
    subs_by_parent: dict[int, list[User]] = {}          # one query for every agent's subs
    if agents:
        for k in db.scalars(select(User).where(User.role == Role.SUB_AGENT,
                                               User.parent_id.in_([a.id for a in agents]))
                            .order_by(User.username)).all():
            subs_by_parent.setdefault(k.parent_id, []).append(k)
    for agent in agents:
        subs = subs_by_parent.get(agent.id, [])
        rows.append({
            "agent": agent,
            "volume_bps": effective_agent_bps(agent, store, "sabong"),
            "bola_volume_bps": effective_agent_bps(agent, store, "bola"),
            "volume_is_default": agent.cut_bps is None,
            "sop_bps": effective_sub_bps(None, agent, store, "sabong"),
            "bola_sop_bps": effective_sub_bps(None, agent, store, "bola"),
            "sop_is_default": agent.sop_bps is None,
            "preview": preview(store, agent=agent, game="sabong"),
            "bola_preview": preview(store, agent=agent, game="bola"),
            "subs": [{
                "user": k,
                "sop_bps": effective_sub_bps(k, agent, store, "sabong"),
                "bola_sop_bps": effective_sub_bps(k, agent, store, "bola"),
                "is_override": k.cut_bps is not None,
                "preview": preview(store, agent=agent, sub=k, game="sabong"),
                "bola_preview": preview(store, agent=agent, sub=k, game="bola"),
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

    remaining = amount
    now = dt.datetime.now(dt.timezone.utc)
    # Oldest first, Meron / Wala then Bola, read in chunks and stopped as soon as the amount is
    # covered: an agent with years of unclaimed lines no longer loads them all into memory.
    for model in (Commission, BolaCommission):
        if remaining <= 0:
            break
        result = db.scalars(select(model).where(
            model.agent_id == actor.id, model.status == CommissionStatus.UNCLAIMED
        ).order_by(model.id).execution_options(yield_per=500))
        try:
            for row in result:
                if remaining <= 0:
                    break
                if row.amount_cents <= remaining:
                    remaining -= row.amount_cents
                    row.status = CommissionStatus.CLAIMED
                    row.claimed_at = now
                # partial rows stay UNCLAIMED; the payable account is the source of truth
        finally:
            result.close()

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
    """Meron / Wala commission lines earned by ``actor`` itself."""
    stmt = select(Commission).where(Commission.agent_id == actor.id)
    if status:
        stmt = stmt.where(Commission.status == status)
    return list(db.scalars(stmt.order_by(Commission.id.desc())
                           .limit(clamp_limit(limit, 1000)).offset(clamp_offset(offset))).all())


def _earner_ids(db: Session, actor: User, agent_id: int | None) -> set[int] | None:
    """Whose commission lines ``actor`` may read. None = every visible account (operator tier).
    ``agent_id`` narrows it to one account in the actor's network, with the same checks as the
    statement pages."""
    from app.core.rbac import assert_user_in_scope, has_global_scope, scope_ids
    if agent_id is not None:
        assert_user_in_scope(db, actor, agent_id)
        return {agent_id}
    return None if has_global_scope(actor) else scope_ids(db, actor)


def list_scope(db: Session, actor: User, *, game: str = "all", status: CommissionStatus | None = None,
               agent_id: int | None = None, start: dt.date | None = None, end: dt.date | None = None,
               limit: int = 300, offset: int = 0) -> list[dict]:
    """Commission lines for the actor AND the accounts below it, both games, newest first.

    Every row is a plain dict (``game``, ``game_label``, ``earner``, ``tier``, ``player``,
    ``ticket``, ``base_cents``, ``rate_bps``, ``amount_cents``, ``status``, ``created_at``,
    ``calc``) so the page does not care which table it came from. The hidden Admin never appears
    for anyone else, and reversed lines are only shown when asked for."""
    ids = _earner_ids(db, actor, agent_id)
    out: list[dict] = []
    limit, offset = clamp_limit(limit, 1000), clamp_offset(offset)
    # Two tables merge into one newest-first list, so each is read down to offset + limit and the
    # merged list is sliced. Bounded at 2000 per table; narrow the date range to go further back.
    take = min(offset + limit, 2000)

    lo, hi = manila_bounds(start, end)

    def keep(model):
        stmt = select(model).join(User, User.id == model.agent_id)
        if model is Commission:               # a sabong game's day = the day its ticket was placed
            stmt = stmt.join(Ticket, Ticket.id == model.ticket_id)
            if lo is not None:
                stmt = stmt.where(Ticket.placed_at >= lo)
            if hi is not None:
                stmt = stmt.where(Ticket.placed_at < hi)
        else:                                 # a Bola game's day = its draw date
            stmt = stmt.join(BolaDraw, BolaDraw.id == model.draw_id)
            if start is not None:
                stmt = stmt.where(BolaDraw.draw_date >= start)
            if end is not None:
                stmt = stmt.where(BolaDraw.draw_date <= end)
        if ids is not None:
            stmt = stmt.where(model.agent_id.in_(ids))
        if actor.role != Role.ADMIN:
            stmt = stmt.where(User.role.notin_([Role.ADMIN, Role.BISHOP]))
        if status is not None:
            stmt = stmt.where(model.status == status)
        else:
            stmt = stmt.where(model.status != CommissionStatus.REVERSED)
        # Eager-load everything _line_view touches so the page costs a fixed handful of queries
        # instead of ~3 lazy loads per row.
        if model is Commission:
            stmt = stmt.options(joinedload(Commission.agent),
                                joinedload(Commission.ticket).joinedload(Ticket.fight),
                                joinedload(Commission.ticket).joinedload(Ticket.player),
                                joinedload(Commission.ticket).joinedload(Ticket.bettor))
        else:
            stmt = stmt.options(joinedload(BolaCommission.agent),
                                joinedload(BolaCommission.ticket).joinedload(BolaTicket.player),
                                joinedload(BolaCommission.ticket).joinedload(BolaTicket.bettor))
        return stmt.order_by(model.id.desc()).limit(take)

    if game in ("all", "sabong"):
        for row in db.scalars(keep(Commission)).all():
            out.append(_line_view(row, "sabong", f"#{row.ticket.fight.number}"))
    if game in ("all", "bola"):
        for row in db.scalars(keep(BolaCommission)).all():
            out.append(_line_view(row, "bola", f"Bola #{row.draw_id}"))
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[offset:offset + limit]


def _line_view(row, game: str, label: str) -> dict:
    ticket = row.ticket
    return {
        "game": game, "game_label": label, "earner": row.agent, "tier": row.tier,
        "player": (ticket.player.full_name if getattr(ticket, "player", None)
                   else ticket.bettor.username if getattr(ticket, "bettor", None) else "walk-in"),
        "ticket": ticket.code, "base_cents": row.base_cents, "rate_bps": row.rate_bps,
        "amount_cents": row.amount_cents, "status": row.status, "created_at": row.created_at,
        "calc": row.calc,
    }
