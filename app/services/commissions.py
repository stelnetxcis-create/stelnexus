"""Network commissions: the Master Agent / Agent / Sub Agent cut system.

How the money is split (per ticket, then summed per game)
---------------------------------------------------------
    cut         = basis * pool%                   the game's cut: Sabong 5% (the Tong), Bola 10% of every stake
                                                  (basis = the ticket's share of the house win, or its stake)
    reserve     = cut * Reserve %                 the house reserve, default 30%: it is NOT paid to anyone. It builds up
                                                  in the system pool shown on the Teller page (accounts.system_pool)
    pool        = cut - reserve                   the network pool (default 70% of the cut)
    agent_gross = pool * Volume %                 the Agent side's cut           (default 30% of the network pool)
    sub_agent   = agent_gross * SOP %             taken OUT of the Agent's cut   (default 20% Sabong / 30% Bola of it)
    agent       = agent_gross - sub_agent
    master      = pool - agent_gross              "Management": whatever the Agent side does not keep

Out of the box, per 100 of the cut: reserve 30, Master 49, Agent side 21 (Sub Agent 4.2 / 6.3, Agent 16.8 / 14.7).

Because the Master's line is defined as the remainder, master + agent + sub_agent always equals the
network pool, and reserve + network pool always equals the cut, to the centavo; integer rounding can never leak or invent money.

Each game has its own calculation: Meron / Wala and Bola each have their own basis, pool % and
default Volume % / SOP % (Settings -> Commissions / Bola). A cut typed on one account (Cuts page)
beats the game default and applies to every game.

Rooms: a cut can also be set for one room (a location) of one game (``RoomCut``). From most to least specific:
  Volume %  Agent's own row for that room > Agent's account-wide cut > the room's default > the game default
  SOP %     the row for that one Sub Agent in that room (its Agent sets it) > the Agent's row for that room
            (all its Sub Agents) > a Sub Agent's personal cut
            > the Agent's account-wide SOP > the room's default > the game default

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
    DrawStatus,
    Event,
    EventStatus,
    Fight,
    Location,
    Role,
    RoomCut,
    SettlementRun,
    SubRoomCut,
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


GAME_LABELS = {"sabong": "Meron / Wala", "bola": "Bola"}


@dataclass
class RoomRates:
    """The room-level cuts that apply to one (game, location), loaded once per settlement run."""
    default: RoomCut | None = None
    by_agent: dict[int, RoomCut] = field(default_factory=dict)
    by_sub: dict[int, SubRoomCut] = field(default_factory=dict)


def load_room_rates(db: Session, game: str, location_id: int | None) -> RoomRates | None:
    """Every RoomCut row of this room (its default and each Agent's), or None when there is no room."""
    if not location_id:
        return None
    rates = RoomRates()
    for row in db.scalars(select(RoomCut).where(RoomCut.game == game, RoomCut.location_id == location_id)).all():
        if row.agent_id is None:
            rates.default = row
        else:
            rates.by_agent[row.agent_id] = row
    for row in db.scalars(select(SubRoomCut).where(SubRoomCut.game == game, SubRoomCut.location_id == location_id)).all():
        rates.by_sub[row.sub_id] = row
    return rates


def effective_agent_bps(agent: User | None, store: SettingsStore, game: str = "sabong",
                        room: RoomRates | None = None) -> int:
    """Volume %: the Agent's row for this room, else the Agent's own value, else the room's default, else the game's."""
    if room is not None and agent is not None:
        row = room.by_agent.get(agent.id)
        if row is not None and row.volume_bps is not None:
            return int(row.volume_bps)
    if agent is not None and agent.cut_bps is not None:
        return int(agent.cut_bps)
    if room is not None and room.default is not None and room.default.volume_bps is not None:
        return int(room.default.volume_bps)
    return int(store.get(_DEFAULT_KEYS[game][0]))


def sub_bps_source(sub: User | None, agent: User | None, store: SettingsStore, game: str = "sabong",
                   room: RoomRates | None = None) -> tuple[int, str]:
    """SOP % and where it comes from: 'sub_room' (set for this Sub Agent in the room), 'agent_room' (the Agent's
    setting for the room), 'personal' (the Sub Agent's own cut), 'agent' (the Agent's account-wide SOP),
    'room' (the room's default) or 'game' (the game's default)."""
    if room is not None and sub is not None:
        row = room.by_sub.get(sub.id)
        if row is not None:
            return int(row.sop_bps), "sub_room"
    if room is not None and agent is not None:
        row = room.by_agent.get(agent.id)
        if row is not None and row.sop_bps is not None:
            return int(row.sop_bps), "agent_room"
    if sub is not None and sub.cut_bps is not None:
        return int(sub.cut_bps), "personal"
    if agent is not None and agent.sop_bps is not None:
        return int(agent.sop_bps), "agent"
    if room is not None and room.default is not None and room.default.sop_bps is not None:
        return int(room.default.sop_bps), "room"
    return int(store.get(_DEFAULT_KEYS[game][1])), "game"


def effective_sub_bps(sub: User | None, agent: User | None, store: SettingsStore,
                      game: str = "sabong", room: RoomRates | None = None) -> int:
    """SOP %: see ``sub_bps_source`` for the order of the levels."""
    return sub_bps_source(sub, agent, store, game, room)[0]


def preview(store: SettingsStore, *, agent: User | None, sub: User | None = None,
            pool: int = 100_000, game: str = "sabong", room: RoomRates | None = None) -> Split:
    """What a pool (default PHP 1,000) would split into for this Agent / Sub Agent, as configured."""
    return split_pool(pool, agent_bps=effective_agent_bps(agent, store, game, room),
                      sub_bps=effective_sub_bps(sub, agent, store, game, room),
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


class Lines(list):
    """The commission lines of one settlement, plus ``reserve_cents``: the house reserve those tickets set aside
    (it has no account to be paid to, so it travels beside the lines and goes to the system pool)."""
    reserve_cents: int = 0


def compute_lines(
    db: Session, tickets: list[Ticket], *, basis: str, house_gross: int, total_staked: int,
    settings: SettingsStore, pool_key: str = "commission.pool_bps", pay_master: bool = True,
    game: str = "sabong", location_id: int | None = None, reserve_key: str = "commission.reserve_bps",
) -> Lines:
    """Commission lines for the settled tickets of one game. ``game`` picks which default Volume % /
    SOP % apply, ``location_id`` is the room whose cuts apply on top of them (see the module docstring).
    ``pay_master`` False leaves the Master's remainder with the house (Bola plan D4)."""
    if not settings.get("commission.enabled"):
        return Lines()
    pay_refunds = bool(settings.get("commission.pay_on_refund"))
    pool_bps = int(settings.get(pool_key))
    reserve_bps = int(settings.get(reserve_key))
    room = load_room_rates(db, game, location_id)        # one read for the whole run
    lines = Lines()
    reserves: list[int] = []                              # per ticket, scaled with the lines if the cap bites
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
        cut = bps_of(base, pool_bps)
        if cut <= 0:
            continue
        reserve = bps_of(cut, reserve_bps)               # kept by the house, in the system pool
        pool = cut - reserve                             # the network pool: Master / Agent / Sub Agent split this
        reserves.append(reserve)

        chain = _chain(db, ticket.agent_id)
        if getattr(ticket, "user_id", None):   # a self-bet: the bettor is the player, only their upline earns
            chain = [u for u in chain if u.id != ticket.user_id]
        sub, agent, master = (_first(chain, Role.SUB_AGENT), _first(chain, Role.AGENT),
                              _first(chain, Role.MASTER))
        agent_bps = effective_agent_bps(agent, settings, game, room)
        sub_bps = effective_sub_bps(sub, agent, settings, game, room)
        split = split_pool(pool, agent_bps=agent_bps, sub_bps=sub_bps,
                           has_agent=_active(agent), has_sub=_active(sub),
                           has_master=_active(master) and pay_master)
        calc = {
            "formula": "cut=basis*cut%; reserve=cut*reserve%; pool=cut-reserve; agent_gross=pool*volume%; "
                       "sub_agent=agent_gross*sop%; agent=agent_gross-sub_agent; master=pool-agent_gross",
            "game": game, "room_location_id": location_id,
            "basis": basis, "base_cents": base, "pool_bps": pool_bps, "cut_cents": cut,
            "reserve_bps": reserve_bps, "reserve_cents": reserve, "pool_cents": pool,
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
    reserve_total = sum(reserves)
    total = sum(line.amount_cents for line in lines) + reserve_total       # network pool + reserve = the whole cut
    if total > cap and total > 0:
        # Scale everything down by the same factor (floor), so the 30 / 70 shape survives the cap. Order: ticket, agent.
        for line in sorted(lines, key=lambda l: (l.ticket_id, l.agent_id)):
            line.calc = {**line.calc, "capped": True, "cap_cents": cap, "uncapped_cents": line.amount_cents}
            line.amount_cents = (line.amount_cents * cap) // total
        reserve_total = (reserve_total * cap) // total
        lines = Lines(line for line in lines if line.amount_cents > 0)
    lines.reserve_cents = reserve_total
    return lines


def persist(
    db: Session, actor: User, run: SettlementRun, fight: Fight, lines: list[CommissionLine],
    *, basis: str,
) -> ledger.LedgerTxn | None:
    reserve = int(getattr(lines, "reserve_cents", 0))
    if not lines and reserve <= 0:
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
    if total <= 0 and reserve <= 0:
        return None
    entries: list[tuple] = [(accounts.commission_expense(db), +(total + reserve))]
    for agent_id, amount in sorted(per_agent.items()):
        agent = db.get(User, agent_id)
        entries.append((accounts.commission_for_user(db, agent), -amount))
    if reserve > 0:
        entries.append((accounts.system_pool(db), -reserve))            # the house reserve, locked in the system pool
    txn = ledger.post(
        db, kind="commission.accrue", entries=entries, actor=actor,
        memo=f"Commissions · fight #{fight.number} v{run.version}",
        ref_type="fight", ref_id=fight.id,
        idempotency_key=f"commission:{fight.id}:{run.version}",
        meta={"run_id": run.id, "basis": basis, "per_tier": per_tier, "reserve_cents": reserve},
    )
    audit.record(db, actor, "commission.split", entity_type="fight", entity_id=fight.id,
                 after={"version": run.version, "basis": basis, "tickets": len({l.ticket_id for l in lines}),
                        "total_cents": total, "reserve_cents": reserve, **{f"{t}_cents": v for t, v in per_tier.items()},
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
    settings: SettingsStore, location_id: int | None = None,
) -> list[CommissionLine]:
    """Commission lines for a settled Bola draw. ``tickets`` are BolaTicket rows; ``location_id`` is the draw's room."""
    return compute_lines(db, tickets, basis=basis, house_gross=house_gross,
                         total_staked=total_staked, settings=settings,
                         pool_key="bola.commission.pool_bps", reserve_key="bola.commission.reserve_bps",
                         pay_master=bool(settings.get("bola.commission.pay_master")),
                         game="bola", location_id=location_id)


def persist_bola(
    db: Session, actor: User, run: BolaRun, draw: BolaDraw, lines: list[CommissionLine],
    *, basis: str,
) -> ledger.LedgerTxn | None:
    reserve = int(getattr(lines, "reserve_cents", 0))
    if not lines and reserve <= 0:
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
    if total <= 0 and reserve <= 0:
        return None
    entries: list[tuple] = [(accounts.commission_expense(db), +(total + reserve))]
    for agent_id, amount in sorted(per_agent.items()):
        entries.append((accounts.commission_for_user(db, db.get(User, agent_id)), -amount))
    if reserve > 0:
        entries.append((accounts.system_pool(db), -reserve))            # the house reserve, locked in the system pool
    txn = ledger.post(
        db, kind="commission.accrue", entries=entries, actor=actor,
        memo=f"Bola commissions · draw #{draw.id} v{run.version}",
        ref_type="bola_draw", ref_id=draw.id,
        idempotency_key=f"bola-commission:{draw.id}:{run.version}",
        meta={"run_id": run.id, "basis": basis, "per_tier": per_tier, "game": "bola", "reserve_cents": reserve},
    )
    audit.record(db, actor, "bola.commission.split", entity_type="bola_draw", entity_id=draw.id,
                 after={"version": run.version, "basis": basis, "reserve_cents": reserve,
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

    A Master (its Agents and their Sub Agents) and an Agent (its Sub Agents) are held to the guard rails in Settings; the
    Admin side is not. ``None`` leaves a field alone,
    an empty string resets it to the game defaults."""
    from app.core.errors import Forbidden
    from app.core.rbac import assert_user_in_scope, Perm, has_perm

    if has_perm(actor, Perm.CUTS_MANAGE):                      # the Admin side (and a Pawn with the grant): anyone in scope
        target = assert_user_in_scope(db, actor, target.id)
    elif has_perm(actor, Perm.AGENT_ROOM_CUTS):
        # A Master sets the overall cut of the Agents it invited and of their Sub Agents; an Agent only of its own Sub
        # Agents. Nobody sets their own, and never another branch's.
        target = db.get(User, target.id)
        parent = db.get(User, target.parent_id) if target is not None and target.parent_id else None
        ok = target is not None and (
            (actor.role == Role.MASTER and ((target.role == Role.AGENT and target.parent_id == actor.id)
                                            or (target.role == Role.SUB_AGENT and parent is not None
                                                and parent.parent_id == actor.id)))
            or (actor.role == Role.AGENT and target.role == Role.SUB_AGENT and target.parent_id == actor.id))
        if not ok:
            raise Forbidden("You can only set the cut of the Agents and Sub Agents below you.")
        if actor.role == Role.AGENT and volume is not None:
            raise Forbidden("Your Volume % is set by your upline.")
    else:
        raise Forbidden("Your role cannot change cuts.")
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
# Room cuts (per location, per game)
# ---------------------------------------------------------------------------

def set_room_cut(db: Session, actor: User, *, game: str, location_id: int, agent_id: int | None = None,
                 volume: str | None = None, sop: str | None = None, settings: SettingsStore,
                 reason: str | None = None) -> RoomCut | None:
    """Set (or clear) the cut of one room of one game. ``None`` leaves a field alone, '' clears it.

    Who may do what:
      * the Admin side (Cuts right): the room's default (``agent_id`` None) and any in-scope Agent's row, both fields;
        held to the Settings limits unless the actor is the Admin account itself;
      * a Master (Agent Panel right): the row of any Agent it invited, Volume % and SOP %, held to the Settings limits;
      * an Agent (Agent Panel right): only its OWN row and only SOP % (what its Sub Agents earn), held to the
        Settings limit. Its own Volume % stays with its Master / the Admin side. A Sub Agent has no one below it.
    Returns the saved row, or None when it ended up empty and was removed. Audited with before / after."""
    from app.core.errors import Forbidden, NotFound
    from app.core.rbac import Perm, assert_user_in_scope, has_perm

    if game not in GAMES:
        raise ValidationError("Unknown game.")
    location = db.get(Location, location_id)
    if location is None:
        raise NotFound("Room not found.")
    admin_side = has_perm(actor, Perm.CUTS_MANAGE)
    if agent_id is None:
        if not admin_side:
            raise Forbidden("Only the Admin side sets a room's default cut.")
        target = None
    else:
        # Permission first, so an account that may not touch this row learns nothing about it.
        if admin_side:
            target = db.get(User, agent_id)
            if target is None or target.role != Role.AGENT:
                raise ValidationError("A room cut belongs to an Agent.")
            assert_user_in_scope(db, actor, target.id)
        elif actor.role == Role.MASTER and has_perm(actor, Perm.AGENT_ROOM_CUTS):
            target = db.get(User, agent_id)
            if target is None or target.role != Role.AGENT or target.parent_id != actor.id:
                raise Forbidden("You can only set the cuts of the Agents you invited.")
        elif actor.role == Role.AGENT and actor.id == agent_id and has_perm(actor, Perm.AGENT_ROOM_CUTS):
            target = actor
            if volume is not None:
                raise Forbidden("Your Volume % is set by your upline. You can set what your Sub Agents earn (SOP %).")
        else:
            raise Forbidden("Your account cannot change cuts.")

    unlimited = actor.is_admin_tier

    def _checked(raw: str | None, label: str, limit_key: str) -> int | None:
        bps = _parse_percent(raw, label=label)
        limit = int(settings.get(limit_key))
        if bps is not None and not unlimited and bps > limit:
            raise ValidationError(f"{label} cannot be more than {limit / 100:g}% (set by Admin).")
        return bps

    new_volume = _checked(volume, "Volume %", "commission.max_cavo_cut_bps") if volume is not None else None
    new_sop = _checked(sop, "SOP %", "commission.max_kubrador_cut_bps") if sop is not None else None

    row = db.scalar(select(RoomCut).where(
        RoomCut.game == game, RoomCut.location_id == location_id,
        RoomCut.agent_id.is_(None) if agent_id is None else RoomCut.agent_id == agent_id))
    before = {"volume_bps": row.volume_bps, "sop_bps": row.sop_bps} if row else {"volume_bps": None, "sop_bps": None}
    after = dict(before)
    if volume is not None:
        after["volume_bps"] = new_volume
    if sop is not None:
        after["sop_bps"] = new_sop
    if before == after:
        return row

    if after["volume_bps"] is None and after["sop_bps"] is None:
        if row is not None:
            db.delete(row)
        row = None
    else:
        if row is None:
            row = RoomCut(game=game, location_id=location_id, agent_id=agent_id)
            db.add(row)
        row.volume_bps, row.sop_bps, row.set_by_id = after["volume_bps"], after["sop_bps"], actor.id
    db.flush()
    audit.record(db, actor, "cut.room.update", entity_type="room_cut", entity_id=row.id if row else None,
                 before=before, after={**after, "game": game, "location": location.name,
                                       "agent": target.username if target else None}, reason=reason)
    return row


def room_rows(db: Session, store: SettingsStore, *, agent: User | None = None) -> list[dict]:
    """One row per (game, active room): the game's default, the room's default and, when ``agent`` is given,
    that Agent's own row with the cuts that really apply to it (and what a Sub Agent would earn per PHP 1,000)."""
    locations = list(db.scalars(select(Location).where(Location.is_active.is_(True)).order_by(Location.name)).all())
    index = {(r.game, r.location_id, r.agent_id): r for r in db.scalars(select(RoomCut)).all()}
    rows = []
    for game in GAMES:
        game_volume, game_sop = int(store.get(_DEFAULT_KEYS[game][0])), int(store.get(_DEFAULT_KEYS[game][1]))
        for loc in locations:
            default = index.get((game, loc.id, None))
            mine = index.get((game, loc.id, agent.id)) if agent is not None else None
            row = {"game": game, "game_label": GAME_LABELS[game], "location": loc,
                   "game_volume_bps": game_volume, "game_sop_bps": game_sop,
                   "room_volume_bps": default.volume_bps if default else None,
                   "room_sop_bps": default.sop_bps if default else None,
                   "mine_volume_bps": mine.volume_bps if mine else None,
                   "mine_sop_bps": mine.sop_bps if mine else None}
            if agent is not None:
                room = RoomRates(default=default, by_agent={agent.id: mine} if mine else {})
                row["volume_bps"] = effective_agent_bps(agent, store, game, room)
                row["sop_bps"] = effective_sub_bps(None, agent, store, game, room)
                row["with_sub"] = split_pool(100_000, agent_bps=row["volume_bps"], sub_bps=row["sop_bps"],
                                             has_agent=True, has_sub=True)
            rows.append(row)
    return rows


def override_rows(db: Session) -> list[RoomCut]:
    """Every Agent-specific room row (for the Admin's Cuts page), newest rooms first by name."""
    return list(db.scalars(
        select(RoomCut).where(RoomCut.agent_id.is_not(None))
        .options(joinedload(RoomCut.location), joinedload(RoomCut.agent))
        .join(Location, Location.id == RoomCut.location_id).order_by(Location.name, RoomCut.game, RoomCut.id)).all())


def set_sub_room_cut(db: Session, actor: User, *, game: str, location_id: int, sub_id: int,
                     sop: str | None, settings: SettingsStore, reason: str | None = None) -> SubRoomCut | None:
    """Set what ONE Sub Agent earns (SOP %) in one room of one game; blank clears it (falls back to the Agent's room
    setting and the levels below it). An Agent may do this only for its own Sub Agents, a Master for the Sub Agents of its own Agents, both held to the Settings limit;
    the Admin side may do it for any Sub Agent in its scope. Audited with before / after."""
    from app.core.errors import Forbidden, NotFound
    from app.core.rbac import Perm, assert_user_in_scope, has_perm

    if game not in GAMES:
        raise ValidationError("Unknown game.")
    sub = db.get(User, sub_id)
    # Permission first, so an account that may not touch this Sub Agent learns nothing about it.
    if has_perm(actor, Perm.CUTS_MANAGE):
        if sub is None or sub.role != Role.SUB_AGENT:
            raise ValidationError("This cut belongs to a Sub Agent.")
        assert_user_in_scope(db, actor, sub.id)
    elif (actor.role == Role.AGENT and has_perm(actor, Perm.AGENT_ROOM_CUTS)
          and sub is not None and sub.role == Role.SUB_AGENT and sub.parent_id == actor.id):
        pass
    elif (actor.role == Role.MASTER and has_perm(actor, Perm.AGENT_ROOM_CUTS)
          and sub is not None and sub.role == Role.SUB_AGENT and sub.parent_id is not None
          and getattr(db.get(User, sub.parent_id), "parent_id", None) == actor.id):
        pass                                           # a Sub Agent of one of this Master's own Agents
    else:
        raise Forbidden("You can only set the cut of your own Sub Agents.")
    location = db.get(Location, location_id)
    if location is None:
        raise NotFound("Room not found.")

    bps = _parse_percent(sop, label="SOP %")
    limit = int(settings.get("commission.max_kubrador_cut_bps"))
    if bps is not None and not actor.is_admin_tier and bps > limit:
        raise ValidationError(f"SOP % cannot be more than {limit / 100:g}% (set by Admin).")
    row = db.scalar(select(SubRoomCut).where(SubRoomCut.game == game, SubRoomCut.location_id == location_id,
                                             SubRoomCut.sub_id == sub_id))
    before = {"sop_bps": row.sop_bps if row else None}
    if before["sop_bps"] == bps:
        return row
    if bps is None:
        if row is not None:
            db.delete(row)
        row = None
    else:
        if row is None:
            row = SubRoomCut(game=game, location_id=location_id, sub_id=sub_id)
            db.add(row)
        row.sop_bps, row.set_by_id = bps, actor.id
    db.flush()
    audit.record(db, actor, "cut.sub_room.update", entity_type="sub_room_cut", entity_id=row.id if row else None,
                 before=before, after={"sop_bps": bps, "game": game, "location": location.name,
                                       "sub": sub.username}, reason=reason)
    return row


def ongoing_games(db: Session) -> list[dict]:
    """The games being played right now, one entry per (game, room): open Sabong events and open / locked Bola draws
    that have a room. ``what`` lists them (event titles, draw slots)."""
    found: dict[tuple[str, int], dict] = {}
    names = {loc.id: loc for loc in db.scalars(select(Location)).all()}

    def _add(game: str, location_id: int | None, label: str) -> None:
        loc = names.get(location_id) if location_id else None
        if loc is None:
            return
        entry = found.setdefault((game, loc.id), {"game": game, "game_label": GAME_LABELS[game], "location": loc, "what": []})
        entry["what"].append(label)

    for ev in db.scalars(select(Event).where(Event.status == EventStatus.OPEN, Event.location_id.is_not(None))
                         .order_by(Event.event_date, Event.id)).all():
        _add("sabong", ev.location_id, ev.title)
    for draw in db.scalars(select(BolaDraw).where(BolaDraw.status.in_([DrawStatus.OPEN, DrawStatus.LOCKED]))
                           .order_by(BolaDraw.scheduled_at, BolaDraw.id)).all():
        _add("bola", draw.location_id, f"Draw · slot {draw.slot}")
    return sorted(found.values(), key=lambda e: (e["game"], e["location"].name))


def sub_ongoing_rows(db: Session, store: SettingsStore, agent: User, sub: User) -> list[dict]:
    """For one Sub Agent: each ongoing game's room with the SOP % that really applies, where it comes from, and what
    the Sub Agent would earn per PHP 1,000 of pool there."""
    room_rows_ = {(r.game, r.location_id, r.agent_id): r for r in db.scalars(select(RoomCut)).all()}
    sub_rows = {(r.game, r.location_id): r for r in db.scalars(select(SubRoomCut).where(SubRoomCut.sub_id == sub.id)).all()}
    out = []
    for g in ongoing_games(db):
        key = (g["game"], g["location"].id)
        mine = room_rows_.get((g["game"], g["location"].id, agent.id))
        room = RoomRates(default=room_rows_.get((g["game"], g["location"].id, None)),
                         by_agent={agent.id: mine} if mine else {},
                         by_sub={sub.id: sub_rows[key]} if key in sub_rows else {})
        volume = effective_agent_bps(agent, store, g["game"], room)
        sop, source = sub_bps_source(sub, agent, store, g["game"], room)
        out.append({**g, "volume_bps": volume, "sop_bps": sop, "source": source,
                    "own_bps": sub_rows[key].sop_bps if key in sub_rows else None,
                    "split": split_pool(100_000, agent_bps=volume, sub_bps=sop, has_agent=True, has_sub=True)})
    return out


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
