"""Fight settlement: declare, recompute, redeclare.

This is the highest-risk area the study flagged (§14: "Redeclare/Recompute let an
admin change outcomes after bets are settled ... nothing shows an audit log").

How StelNexus makes it safe:

* **Versioned runs.** Each declaration is a `SettlementRun` with a version number.
  A correction never edits the old numbers -- it *reverses* the previous run's
  ledger transactions and posts a new one. Both remain visible forever.
* **Maker-checker.** When `fight.redeclare_requires_approval` is on, the admin who
  requests a correction cannot approve it; a second approver (Master or Admin) must.
* **Reason required.** No correction without a written reason, stored on the run,
  the ledger transactions and the audit log.
* **Deterministic integer math.** Same inputs -> same centavo outputs, so
  "recompute" is genuinely idempotent instead of drifting.
* **Claw-back is explicit.** If a winner already spent their winnings, the reversal
  drives their wallet negative (a recorded debt) and is flagged in the audit log.

Payout model (pool / pari-mutuel, the standard Meron-Wala model):
    total   = meron_pool + wala_pool
    main    = meron_pool + wala_pool            (Draw stakes never dilute this pool)
    cut     = floor(main * tong_bps / 10000)
    net     = main - cut
    payout  = floor(stake * net / winning_pool)     for each winning ticket
    maximum odds: the winning side's multiple is capped (payout.max_llamado_bps for the Llamado side,
        payout.max_dehado_bps for the other); the capped-off surplus stays with the house
    remainder (0-2 centavos) -> house (or spread to winners, admin choice)
    minimum odds: if net / winning_pool is below payout.min_odds_bps the fight is
        cancelled automatically and every stake is refunded.
Draw is a separate fixed-odds side bet (payout.draw_odds_bps):
    result draw   -> Draw tickets win stake * draw_odds; Meron/Wala stakes are refunded
    result meron/wala -> Draw stakes go to the house
canceled -> every stake refunded in full, no cut
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core import ttlcache
from app.core.errors import Conflict, Forbidden, LockedState, NotFound, ValidationError
from app.core.money import bps_of, odds_of
from app.core.settings_registry import SettingsStore
from app.db.models import (
    Account,
    Commission,
    Fight,
    FightStatus,
    Notification,
    PayoutModel,
    Result,
    Role,
    RunStatus,
    SettlementRun,
    Side,
    Ticket,
    TicketStatus,
    User,
)
from app.services import accounts, betting, commissions, events, ledger


@dataclass
class PayoutLine:
    ticket: Ticket
    payout_cents: int
    outcome: str  # win | lose | refund


@dataclass
class Plan:
    result: Result
    model: str
    meron: int = 0
    wala: int = 0
    draw: int = 0
    total: int = 0
    auto_cancel: bool = False
    house_cut: int = 0
    platform_cut: int = 0
    net_pool: int = 0
    payout_total: int = 0
    refund_total: int = 0
    remainder: int = 0
    capped_surplus: int = 0
    house_take: int = 0
    lines: list[PayoutLine] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "result": str(self.result), "model": self.model, "meron": self.meron, "wala": self.wala,
            "draw": self.draw, "auto_cancel": self.auto_cancel, "total": self.total, "house_cut": self.house_cut, "platform_cut": self.platform_cut, "net_pool": self.net_pool,
            "payout_total": self.payout_total, "refund_total": self.refund_total,
            "remainder": self.remainder, "capped_surplus": self.capped_surplus, "house_take": self.house_take,
            "tickets": len(self.lines),
            "winners": sum(1 for line in self.lines if line.outcome == "win"),
        }


def active_tickets(db: Session, fight: Fight) -> list[Ticket]:
    return list(db.scalars(select(Ticket).where(
        Ticket.fight_id == fight.id, Ticket.status == TicketStatus.ACTIVE).order_by(Ticket.id)).all())


def _refund_all(plan: Plan, tickets: list[Ticket]) -> Plan:
    for ticket in tickets:
        plan.lines.append(PayoutLine(ticket, ticket.stake_cents, "refund"))
    plan.refund_total = sum(t.stake_cents for t in tickets)
    plan.house_cut = plan.platform_cut = plan.net_pool = plan.house_take = plan.payout_total = plan.remainder = 0
    return plan


def build_plan(db: Session, fight: Fight, result: Result, settings: SettingsStore) -> Plan:
    event = events.get_event(db, fight.event_id)
    tickets = active_tickets(db, fight)
    snap = betting.pool_snapshot(db, fight)
    plan = Plan(result=result, model=str(event.payout_model), meron=snap["meron"],
                wala=snap["wala"], draw=snap["draw"], total=snap["total"])
    if not tickets or plan.total == 0:
        return plan

    cap = int(settings.get("payout.max_payout_cents")) or None
    draw_odds = int(settings.get("payout.draw_odds_bps"))
    main_tickets = [t for t in tickets if t.side != Side.DRAW]
    draw_tickets = [t for t in tickets if t.side == Side.DRAW]

    def finish() -> Plan:
        plan.refund_total = sum(l.payout_cents for l in plan.lines if l.outcome == "refund")
        plan.house_take = plan.total - sum(l.payout_cents for l in plan.lines)
        return plan

    # --- canceled: refund everything -------------------------------------
    if result == Result.CANCELED:
        return _refund_all(plan, tickets)

    # --- draw: Draw tickets win at the draw odds, Meron/Wala are refunded -
    if result == Result.DRAW:
        for ticket in main_tickets:
            plan.lines.append(PayoutLine(ticket, ticket.stake_cents, "refund"))
        for ticket in draw_tickets:
            payout = odds_of(ticket.stake_cents, draw_odds)
            if cap:
                payout = min(payout, cap)
            plan.lines.append(PayoutLine(ticket, payout, "win"))
            plan.payout_total += payout
        return finish()

    # --- meron / wala won: Draw side loses ---------------------------------
    draw_lines = [PayoutLine(t, 0, "lose") for t in draw_tickets]

    if event.payout_model == PayoutModel.FIXED:
        odds = (fight.fixed_odds_meron_bps if result == Result.MERON else fight.fixed_odds_wala_bps) \
            or int(settings.get("payout.default_fixed_odds_bps"))
        for ticket in main_tickets:
            if ticket.side.value == result.value:
                payout = odds_of(ticket.stake_cents, odds)
                if cap:
                    payout = min(payout, cap)
                plan.lines.append(PayoutLine(ticket, payout, "win"))
                plan.payout_total += payout
            else:
                plan.lines.append(PayoutLine(ticket, 0, "lose"))
        plan.lines.extend(draw_lines)
        plan.house_take = plan.total - plan.payout_total
        plan.house_cut = max(0, plan.house_take)
        plan.net_pool = plan.payout_total
        return plan

    # pool / pari-mutuel on Meron + Wala only
    main_total = snap["main"]
    cut_bps = events.house_cut_for(db, event, settings)
    fig = betting.pool_figures(plan.meron, plan.wala, cut_bps, events.platform_cut_for(settings))
    plan.platform_cut, plan.house_cut, plan.net_pool = fig["platform"], fig["house"], fig["net"]
    winning_pool = plan.meron if result == Result.MERON else plan.wala

    # Minimum-odds rule: a winner that would pay under the floor cancels the fight.
    min_odds = int(settings.get("payout.min_odds_bps"))
    if main_total and min_odds and winning_pool and (plan.net_pool * 10000) // winning_pool < min_odds:
        plan.auto_cancel = True
        return _refund_all(plan, tickets)

    if winning_pool == 0:
        if str(settings.get("payout.no_winner_policy")) == "refund":
            for ticket in main_tickets:
                plan.lines.append(PayoutLine(ticket, ticket.stake_cents, "refund"))
            plan.lines.extend(draw_lines)
            plan.house_cut = plan.platform_cut = plan.net_pool = 0
            return finish()
        for ticket in main_tickets:
            plan.lines.append(PayoutLine(ticket, 0, "lose"))
        plan.lines.extend(draw_lines)
        plan.house_take = plan.total
        return plan

    # Llamado / Dehado maximum: the winning side's multiple never exceeds its ceiling; the rest stays with the house.
    ceiling = betting.max_odds_bps(settings, result.value, snap, fight.llamado_side)
    paid_pool = min(plan.net_pool, winning_pool * ceiling // 10000) if ceiling else plan.net_pool
    plan.capped_surplus = plan.net_pool - paid_pool

    for ticket in main_tickets:
        if ticket.side.value == result.value:
            payout = (ticket.stake_cents * paid_pool) // winning_pool
            if cap:
                payout = min(payout, cap)
            plan.lines.append(PayoutLine(ticket, payout, "win"))
            plan.payout_total += payout
        else:
            plan.lines.append(PayoutLine(ticket, 0, "lose"))
    plan.lines.extend(draw_lines)

    plan.remainder = paid_pool - plan.payout_total
    if plan.remainder > 0 and str(settings.get("payout.rounding_remainder")) == "winners":
        winners = sorted((line for line in plan.lines if line.outcome == "win"),
                         key=lambda l: (-l.ticket.stake_cents, l.ticket.id))
        index = 0
        while plan.remainder > 0 and winners:
            winners[index % len(winners)].payout_cents += 1
            plan.payout_total += 1
            plan.remainder -= 1
            index += 1
    # house keeps: tong + rounding remainder + every losing Draw stake
    plan.house_take = plan.total - plan.payout_total
    return plan


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def _next_version(db: Session, fight: Fight) -> int:
    highest = db.scalar(select(func.coalesce(func.max(SettlementRun.version), 0))
                        .where(SettlementRun.fight_id == fight.id)) or 0
    return max(int(highest), fight.settlement_version) + 1


def _rollback_previous(db: Session, fight: Fight, actor: User, reason: str) -> SettlementRun | None:
    previous = db.scalar(select(SettlementRun).where(
        SettlementRun.fight_id == fight.id, SettlementRun.status == RunStatus.APPLIED
    ).order_by(SettlementRun.version.desc()))
    if previous is None:
        return None

    if previous.payout_txn_id:
        txn = db.get(ledger.LedgerTxn, previous.payout_txn_id)
        if txn and txn.reversed_by_txn_id is None:
            ledger.reverse(db, txn, actor=actor, reason=reason, force=True,
                           idempotency_key=f"settle-reverse:{fight.id}:{previous.version}",
                           memo=f"Reverse settlement v{previous.version} of fight #{fight.number}")
    if previous.commission_txn_id:
        txn = db.get(ledger.LedgerTxn, previous.commission_txn_id)
        if txn and txn.reversed_by_txn_id is None:
            ledger.reverse(db, txn, actor=actor, reason=reason, force=True,
                           idempotency_key=f"comm-reverse:{fight.id}:{previous.version}")
    commissions.mark_reversed(db, previous.id)

    reset = db.scalars(select(Ticket).where(
        Ticket.fight_id == fight.id, Ticket.settlement_version == previous.version,
        Ticket.status.in_([TicketStatus.SETTLED, TicketStatus.REFUNDED]))).all()
    for ticket in reset:
        ticket.status = TicketStatus.ACTIVE
        ticket.payout_cents = 0
        ticket.settled_at = None
        ticket.settlement_version = None
        ticket.payout_txn_id = None

    previous.status = RunStatus.REVERSED
    db.flush()
    return previous


def _apply(db: Session, run: SettlementRun, actor: User, settings: SettingsStore) -> SettlementRun:
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
    fight = db.scalar(select(Fight).where(Fight.id == run.fight_id).with_for_update())
    if fight is None:
        raise NotFound("Game not found.")
    if run.status != RunStatus.PENDING:
        raise Conflict("That settlement run has already been processed.")

    before = {"result": fight.result, "status": fight.status, "version": fight.settlement_version}
    previous = _rollback_previous(db, fight, actor, run.reason)
    plan = build_plan(db, fight, run.result, settings)

    payout_txn = None
    if plan.total > 0:
        pool = accounts.pool_for_fight(db, fight)
        credits: dict[int, int] = {}
        house_take = plan.house_take

        for line in plan.lines:
            if line.payout_cents <= 0:
                continue
            account = betting.beneficiary_account(db, line.ticket)
            credits[account.id] = credits.get(account.id, 0) + line.payout_cents

        entries: list[tuple[Account, int]] = [(pool, +plan.total)]
        by_id = {pool.id: pool}
        for account_id, amount in sorted(credits.items()):
            account = db.get(Account, account_id)
            by_id[account_id] = account
            entries.append((account, -amount))
        if house_take > 0:
            house = accounts.house(db)
            entries.append((house, -house_take))
        elif house_take < 0:  # fixed-odds fight the house lost
            house = accounts.house(db)
            entries.append((house, -house_take))

        payout_txn = ledger.post(
            db, kind="fight.settle", entries=entries, actor=actor,
            memo=f"Settle fight #{fight.number} v{run.version} · {run.result}",
            ref_type="fight", ref_id=fight.id, reason=run.reason,
            idempotency_key=f"settle:{fight.id}:{run.version}",
            meta={"run_id": run.id, "plan": plan.as_dict()},
        )

        now = dt.datetime.now(dt.timezone.utc)
        for line in plan.lines:
            ticket = line.ticket
            ticket.payout_cents = line.payout_cents
            ticket.status = TicketStatus.REFUNDED if line.outcome == "refund" else TicketStatus.SETTLED
            ticket.settled_at = now
            ticket.settlement_version = run.version
            ticket.payout_txn_id = payout_txn.id

    basis = str(settings.get("commission.basis"))
    settled_tickets = [line.ticket for line in plan.lines]
    lines = commissions.compute_lines(
        db, settled_tickets, basis=basis, house_gross=plan.house_take,
        total_staked=plan.total, settings=settings,
    )
    commission_txn = commissions.persist(db, actor, run, fight, lines, basis=basis)

    fight.result = run.result
    fight.status = FightStatus.SETTLED
    fight.settlement_version = run.version
    fight.declared_at = dt.datetime.now(dt.timezone.utc)
    fight.declared_by_id = actor.id
    if fight.locked_at is None:
        fight.locked_at = fight.declared_at

    run.status = RunStatus.APPLIED
    run.totals = plan.as_dict()
    run.payout_txn_id = payout_txn.id if payout_txn else None
    run.commission_txn_id = commission_txn.id if commission_txn else None
    if previous is not None:
        run.reverses_run_id = previous.id
    db.flush()

    audit.record(db, actor, f"fight.{run.action}", entity_type="fight", entity_id=fight.id,
                 before=before,
                 after={"result": run.result, "version": run.version, "totals": plan.as_dict(),
                        "reversed_run": previous.id if previous else None},
                 reason=run.reason)
    return run


def declare(db: Session, actor: User, *, fight_id: int, result: Result, settings: SettingsStore,
            reason: str | None = None) -> SettlementRun:
    """First declaration of a result. Settles immediately."""
    fight = db.scalar(select(Fight).where(Fight.id == fight_id).with_for_update())
    if fight is None:
        raise NotFound("Game not found.")
    if fight.status == FightStatus.SETTLED:
        raise LockedState("This fight is already settled. Use Redeclare to correct it.")
    if fight.status == FightStatus.VOID:
        raise LockedState("This game is void.")
    if settings.get("event.auto_lock_on_declare") and fight.status == FightStatus.OPEN:
        fight.status = FightStatus.LOCKED
        fight.locked_at = dt.datetime.now(dt.timezone.utc)
        db.flush()

    run = SettlementRun(
        fight_id=fight.id, version=_next_version(db, fight), action="declare", result=result,
        status=RunStatus.PENDING, reason=(reason or "Initial declaration")[:255],
        requested_by_id=actor.id, approved_by_id=actor.id,
        reviewed_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(run)
    db.flush()
    return _apply(db, run, actor, settings)


def request_correction(
    db: Session, actor: User, *, fight_id: int, result: Result, reason: str,
    settings: SettingsStore, action: str = "redeclare",
) -> SettlementRun:
    """Redeclare (change result) or recompute (same result, re-run the maths)."""
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("A written reason of at least 5 characters is required.")
    if action not in ("redeclare", "recompute"):
        raise ValidationError("Unknown correction action.")
    fight = db.scalar(select(Fight).where(Fight.id == fight_id).with_for_update())
    if fight is None:
        raise NotFound("Game not found.")
    if fight.status != FightStatus.SETTLED:
        raise LockedState("Only a settled fight can be redeclared or recomputed.")
    if action == "recompute":
        result = fight.result
    elif result == fight.result:
        raise ValidationError("That is already the declared result. Use Recompute instead.")

    pending = db.scalar(select(SettlementRun).where(
        SettlementRun.fight_id == fight.id, SettlementRun.status == RunStatus.PENDING))
    if pending is not None:
        raise Conflict("There is already a pending correction for this fight.")

    run = SettlementRun(
        fight_id=fight.id, version=_next_version(db, fight), action=action, result=result,
        status=RunStatus.PENDING, reason=reason[:255], requested_by_id=actor.id,
    )
    db.add(run)
    db.flush()
    audit.record(db, actor, f"fight.{action}.request", entity_type="fight", entity_id=fight.id,
                 before={"result": fight.result}, after={"requested_result": result, "run": run.id},
                 reason=reason)

    if not settings.get("fight.redeclare_requires_approval"):
        run.approved_by_id = actor.id
        run.reviewed_at = dt.datetime.now(dt.timezone.utc)
        return _apply(db, run, actor, settings)

    for admin_id in db.scalars(select(User.id).where(User.role.in_([Role.ADMIN, Role.MASTER]),
                                                      User.id != actor.id)).all():
        db.add(Notification(user_id=admin_id, level="warning",
                            message=f"{actor.username} requested a {action} on fight #{fight.number}",
                            link="/change_name"))
    db.flush()
    return run


def approve(db: Session, actor: User, *, run_id: int, settings: SettingsStore) -> SettlementRun:
    run = db.scalar(select(SettlementRun).where(SettlementRun.id == run_id).with_for_update())
    if run is None:
        raise NotFound("Settlement run not found.")
    if run.status != RunStatus.PENDING:
        raise Conflict("That request has already been processed.")
    if settings.get("fight.redeclare_requires_approval") and run.requested_by_id == actor.id:
        raise Forbidden("Maker-checker: a second Master/Admin must approve this correction.")
    run.approved_by_id = actor.id
    run.reviewed_at = dt.datetime.now(dt.timezone.utc)
    return _apply(db, run, actor, settings)


def reject(db: Session, actor: User, *, run_id: int, note: str) -> SettlementRun:
    ttlcache.invalidate_after_commit(db)   # dashboard KPIs changed
    run = db.scalar(select(SettlementRun).where(SettlementRun.id == run_id).with_for_update())
    if run is None:
        raise NotFound("Settlement run not found.")
    if run.status != RunStatus.PENDING:
        raise Conflict("That request has already been processed.")
    run.status = RunStatus.REJECTED
    run.approved_by_id = actor.id
    run.reviewed_at = dt.datetime.now(dt.timezone.utc)
    run.reason = f"{run.reason} | rejected: {note[:120]}"
    db.flush()
    audit.record(db, actor, "fight.correction.reject", entity_type="settlement_run", entity_id=run.id,
                 reason=note)
    return run


def pending_runs(db: Session) -> list[SettlementRun]:
    return list(db.scalars(select(SettlementRun).where(SettlementRun.status == RunStatus.PENDING)
                           .order_by(SettlementRun.id.desc())).all())


def runs_for(db: Session, fight_id: int) -> list[SettlementRun]:
    return list(db.scalars(select(SettlementRun).where(SettlementRun.fight_id == fight_id)
                           .order_by(SettlementRun.version.desc())).all())
