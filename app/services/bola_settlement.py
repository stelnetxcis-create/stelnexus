"""Bola settlement: declare, redeclare, recompute, void.

Same proven machinery as ``settlement.py`` (versioned runs, reversals instead of edits,
maker-checker, written reasons, deterministic integer maths), applied to a fixed-multiplier
exact-match game instead of a pari-mutuel pool:

    for every ACTIVE ticket, for every line (a, b):
        draw == (a, b)  ->  payout += payout_of(line.tumbok_cents, M)
        draw == (b, a)  ->  payout += payout_of(line.sahod_cents,  M)
    capped per ticket by payout.max_payout_cents

One ledger transaction per run:
    pool(draw) +total_staked | wallet(winner) -payout | house -(staked - payouts)

When payouts exceed stakes the house line is positive and the house balance goes negative:
a **recorded loss**, not an error. The liability caps in ``bola.check_limits`` are what keep it
bounded. A correction reverses the previous run's payout and commission transactions with
``force=True``; a claw-back after winnings were spent leaves a negative wallet (a recorded debt)
and an audit row, exactly like sabong.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, LockedState, NotFound, ValidationError
from app.core.money import payout_of
from app.core.rbac import is_top
from app.core.settings_registry import SettingsStore
from app.db.base import utcnow
from app.db.models import (
    Account,
    BolaCommission,
    BolaDraw,
    BolaRun,
    BolaTicket,
    DrawStatus,
    Notification,
    Player,
    Role,
    RunStatus,
    TicketStatus,
    User,
)
from app.services import accounts, bola, commissions, ledger


@dataclass
class PayoutLine:
    ticket: BolaTicket
    payout_cents: int
    outcome: str                     # win | lose | refund
    winning_side: str | None = None  # tumbok | sahod | None
    winning_pair: tuple[int, int] | None = None


@dataclass
class Plan:
    ball_first: int | None
    ball_second: int | None
    multiplier_x100: int
    total: int = 0
    payout_total: int = 0
    refund_total: int = 0
    house_take: int = 0
    capped: int = 0
    void: bool = False
    lines: list[PayoutLine] = field(default_factory=list)

    @property
    def winners(self) -> int:
        return sum(1 for line in self.lines if line.outcome == "win")

    def as_dict(self) -> dict:
        return {
            "ball_first": self.ball_first, "ball_second": self.ball_second,
            "multiplier_x100": self.multiplier_x100, "total": self.total,
            "payout_total": self.payout_total, "refund_total": self.refund_total,
            "house_take": self.house_take, "tickets": len(self.lines),
            "winners": self.winners, "capped": self.capped, "void": self.void,
            "winning_lines": [
                {"ticket": line.ticket.code, "side": line.winning_side,
                 "pair": list(line.winning_pair) if line.winning_pair else None,
                 "payout_cents": line.payout_cents}
                for line in self.lines if line.outcome == "win"
            ],
        }


def validate_balls(ball_first: int, ball_second: int, settings: SettingsStore) -> tuple[int, int]:
    ceiling = bola.max_number(settings)
    try:
        first, second = int(ball_first), int(ball_second)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Enter the two winning numbers.") from exc
    if not (1 <= first <= ceiling and 1 <= second <= ceiling):
        raise ValidationError(f"Each ball must be between 1 and {ceiling}.")
    if first == second and not settings.get("bola.allow_duplicate_balls"):
        raise ValidationError("The two balls must be different numbers.")
    return first, second


def build_plan(db: Session, draw: BolaDraw, ball_first: int | None, ball_second: int | None,
               settings: SettingsStore, *, void: bool = False) -> Plan:
    tickets = bola.active_tickets(db, draw)
    rate = bola.multiplier(settings)
    plan = Plan(ball_first=ball_first, ball_second=ball_second, multiplier_x100=rate,
                total=sum(t.stake_cents for t in tickets), void=void)
    if not tickets:
        return plan
    cap = int(settings.get("payout.max_payout_cents")) or None

    if void:
        for ticket in tickets:
            plan.lines.append(PayoutLine(ticket, ticket.stake_cents, "refund"))
        plan.refund_total = plan.total
        plan.house_take = 0
        return plan

    result = (ball_first, ball_second)
    reverse = (ball_second, ball_first)
    for ticket in tickets:
        payout = 0
        side = None
        pair = None
        for line in bola.ticket_lines(db, ticket):
            if (line.first, line.second) == result:
                payout += payout_of(line.tumbok_cents, rate)
                side, pair = "tumbok", (line.first, line.second)
            elif (line.first, line.second) == reverse:
                payout += payout_of(line.sahod_cents, rate)
                side, pair = "sahod", (line.first, line.second)
        if cap and payout > cap:
            payout = cap
            plan.capped += 1
        plan.lines.append(PayoutLine(ticket, payout, "win" if payout > 0 else "lose",
                                     side if payout > 0 else None, pair if payout > 0 else None))
        plan.payout_total += payout
    plan.house_take = plan.total - plan.payout_total
    return plan


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def _next_version(db: Session, draw: BolaDraw) -> int:
    highest = db.scalar(select(func.coalesce(func.max(BolaRun.version), 0))
                        .where(BolaRun.draw_id == draw.id)) or 0
    return max(int(highest), draw.settlement_version) + 1


def _rollback_previous(db: Session, draw: BolaDraw, actor: User, reason: str) -> BolaRun | None:
    previous = db.scalar(select(BolaRun).where(
        BolaRun.draw_id == draw.id, BolaRun.status == RunStatus.APPLIED
    ).order_by(BolaRun.version.desc()))
    if previous is None:
        return None
    for txn_id, prefix in ((previous.payout_txn_id, "bola-settle-reverse"),
                           (previous.commission_txn_id, "bola-comm-reverse")):
        if not txn_id:
            continue
        txn = db.get(ledger.LedgerTxn, txn_id)
        if txn is not None and txn.reversed_by_txn_id is None:
            ledger.reverse(db, txn, actor=actor, reason=reason, force=True,
                           idempotency_key=f"{prefix}:{draw.id}:{previous.version}",
                           memo=f"Reverse bola settlement v{previous.version} of draw #{draw.id}")
    commissions.mark_bola_reversed(db, previous.id)

    reset = db.scalars(select(BolaTicket).where(
        BolaTicket.draw_id == draw.id, BolaTicket.settlement_version == previous.version,
        BolaTicket.status.in_([TicketStatus.SETTLED, TicketStatus.REFUNDED]))).all()
    for ticket in reset:
        ticket.status = TicketStatus.ACTIVE
        ticket.payout_cents = 0
        ticket.settled_at = None
        ticket.settlement_version = None
        ticket.payout_txn_id = None
    previous.status = RunStatus.REVERSED
    db.flush()
    return previous


def beneficiary_account(db: Session, ticket: BolaTicket) -> Account:
    """Payouts and refunds land on the bettor's credit wallet: the player's, or the account's own for a
    self-bet. A legacy archive ticket with neither falls back to the retired central account so old rows
    still balance."""
    if ticket.player_id:
        player = db.get(Player, ticket.player_id)
        if player is not None:
            return accounts.wallet_for_player(db, player)
    if ticket.user_id:
        bettor = db.get(User, ticket.user_id)
        if bettor is not None and bettor.has_wallet:
            return accounts.wallet_for_user(db, bettor)
    return accounts.central_cash(db)


def _post_plan(db: Session, draw: BolaDraw, run: BolaRun, plan: Plan, actor: User,
               kind: str) -> ledger.LedgerTxn | None:
    if plan.total <= 0:
        return None
    pool = accounts.pool_for_draw(db, draw)
    credits: dict[int, int] = {}
    for line in plan.lines:
        if line.payout_cents <= 0:
            continue
        account = beneficiary_account(db, line.ticket)
        credits[account.id] = credits.get(account.id, 0) + line.payout_cents

    entries: list[tuple[Account, int]] = [(pool, +plan.total)]
    for account_id, amount in sorted(credits.items()):
        entries.append((db.get(Account, account_id), -amount))
    if plan.house_take:
        # negative house_take (the house lost) simply flips the sign: a recorded loss.
        entries.append((accounts.house(db), -plan.house_take))
    return ledger.post(
        db, kind=kind, entries=entries, actor=actor,
        memo=f"Bola draw #{draw.id} v{run.version} · {draw.result_label or 'void'}",
        ref_type="bola_draw", ref_id=draw.id, reason=run.reason,
        idempotency_key=f"bola-{kind.split('.')[-1]}:{draw.id}:{run.version}",
        meta={"run_id": run.id, "plan": plan.as_dict()},
    )


def _apply(db: Session, run: BolaRun, actor: User, settings: SettingsStore) -> BolaRun:
    draw = bola._lock_draw(db, run.draw_id)
    if run.status != RunStatus.PENDING:
        raise Conflict("That settlement run has already been processed.")

    before = {"balls": draw.result_label, "status": str(draw.status),
              "version": draw.settlement_version}
    previous = _rollback_previous(db, draw, actor, run.reason)
    void = run.action == "void"
    plan = build_plan(db, draw, run.ball_first, run.ball_second, settings, void=void)

    payout_txn = _post_plan(db, draw, run, plan, actor,
                            "bola.refund" if void else "bola.settle")
    now = utcnow()
    for line in plan.lines:
        ticket = line.ticket
        ticket.payout_cents = line.payout_cents
        ticket.status = TicketStatus.REFUNDED if line.outcome == "refund" else TicketStatus.SETTLED
        ticket.settled_at = now
        ticket.settlement_version = run.version
        ticket.payout_txn_id = payout_txn.id if payout_txn else None

    commission_txn = None
    if not void:
        basis = str(settings.get("bola.commission.basis"))
        lines = commissions.compute_bola_lines(
            db, [line.ticket for line in plan.lines], basis=basis, house_gross=plan.house_take,
            total_staked=plan.total, settings=settings,
        )
        commission_txn = commissions.persist_bola(db, actor, run, draw, lines, basis=basis)

    draw.status = DrawStatus.VOID if void else DrawStatus.SETTLED
    if not void:
        draw.ball_first, draw.ball_second = run.ball_first, run.ball_second
    draw.settlement_version = run.version
    draw.declared_at = now
    draw.declared_by_id = actor.id
    if draw.locked_at is None:
        draw.locked_at = now

    run.status = RunStatus.APPLIED
    run.totals = plan.as_dict()
    run.payout_txn_id = payout_txn.id if payout_txn else None
    run.commission_txn_id = commission_txn.id if commission_txn else None
    if previous is not None:
        run.reverses_run_id = previous.id
    db.flush()

    audit.record(db, actor, f"bola.{run.action}", entity_type="bola_draw", entity_id=draw.id,
                 before=before,
                 after={"balls": draw.result_label, "version": run.version,
                        "totals": plan.as_dict(), "reversed_run": previous.id if previous else None},
                 reason=run.reason)
    return run


def declare(db: Session, actor: User, *, draw_id: int, ball_first: int, ball_second: int,
            settings: SettingsStore, reason: str | None = None) -> BolaRun:
    """First declaration of the two balls. Settles immediately."""
    bola.assert_enabled(settings)
    first, second = validate_balls(ball_first, ball_second, settings)
    draw = bola._lock_draw(db, draw_id)
    if draw.status == DrawStatus.SETTLED:
        raise LockedState("This draw is already settled. Use a correction to change the result.")
    if draw.status == DrawStatus.VOID:
        raise LockedState("This draw is void.")
    now = utcnow()
    if now < draw.scheduled_at:
        # No bet may be settled before the draw time unless the operator tier says why.
        if not (is_top(actor) and reason and len(reason.strip()) >= 5):
            raise LockedState("The draw time has not arrived yet. Master/Admin may declare early "
                              "with a written reason.")
    if settings.get("event.auto_lock_on_declare") and draw.status in (DrawStatus.SCHEDULED,
                                                                     DrawStatus.OPEN):
        draw.status = DrawStatus.LOCKED
        draw.locked_at = now
        db.flush()

    run = BolaRun(
        draw_id=draw.id, version=_next_version(db, draw), action="declare",
        ball_first=first, ball_second=second, status=RunStatus.PENDING,
        reason=(reason or "Initial declaration")[:255], requested_by_id=actor.id,
        approved_by_id=actor.id, reviewed_at=now,
    )
    db.add(run)
    db.flush()
    return _apply(db, run, actor, settings)


def void_draw(db: Session, actor: User, *, draw_id: int, reason: str,
              settings: SettingsStore) -> BolaRun:
    """Call the draw off: every stake refunded in full, no commission. A settled draw is
    reversed first, so the books stay balanced."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("A written reason of at least 5 characters is required to void a draw.")
    draw = bola._lock_draw(db, draw_id)
    if draw.status == DrawStatus.VOID:
        raise LockedState("This draw is already void.")
    pending = db.scalar(select(BolaRun).where(BolaRun.draw_id == draw.id,
                                              BolaRun.status == RunStatus.PENDING))
    if pending is not None:
        raise Conflict("There is a pending correction for this draw. Resolve it first.")
    run = BolaRun(
        draw_id=draw.id, version=_next_version(db, draw), action="void",
        ball_first=draw.ball_first, ball_second=draw.ball_second, status=RunStatus.PENDING,
        reason=reason[:255], requested_by_id=actor.id, approved_by_id=actor.id, reviewed_at=utcnow(),
    )
    db.add(run)
    db.flush()
    return _apply(db, run, actor, settings)


def request_correction(db: Session, actor: User, *, draw_id: int, ball_first: int | None,
                       ball_second: int | None, reason: str, settings: SettingsStore,
                       action: str = "redeclare") -> BolaRun:
    """Redeclare (new balls) or recompute (same balls, re-run the maths). Maker-checker."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("A written reason of at least 5 characters is required.")
    if action not in ("redeclare", "recompute"):
        raise ValidationError("Unknown correction action.")
    draw = bola._lock_draw(db, draw_id)
    if draw.status != DrawStatus.SETTLED:
        raise LockedState("Only a settled draw can be redeclared or recomputed.")
    if action == "recompute":
        first, second = draw.ball_first, draw.ball_second
    else:
        first, second = validate_balls(ball_first, ball_second, settings)
        if (first, second) == (draw.ball_first, draw.ball_second):
            raise ValidationError("Those are already the declared balls. Use Recompute instead.")

    pending = db.scalar(select(BolaRun).where(BolaRun.draw_id == draw.id,
                                              BolaRun.status == RunStatus.PENDING))
    if pending is not None:
        raise Conflict("There is already a pending correction for this draw.")

    run = BolaRun(
        draw_id=draw.id, version=_next_version(db, draw), action=action,
        ball_first=first, ball_second=second, status=RunStatus.PENDING, reason=reason[:255],
        requested_by_id=actor.id,
    )
    db.add(run)
    db.flush()
    audit.record(db, actor, f"bola.{action}.request", entity_type="bola_draw", entity_id=draw.id,
                 before={"balls": draw.result_label},
                 after={"requested": f"{first}/{second}", "run": run.id}, reason=reason)

    if not settings.get("fight.redeclare_requires_approval"):
        run.approved_by_id = actor.id
        run.reviewed_at = utcnow()
        return _apply(db, run, actor, settings)

    for admin_id in db.scalars(select(User.id).where(User.role.in_([Role.ADMIN, Role.BISHOP, Role.MASTER]),
                                                     User.id != actor.id)).all():
        db.add(Notification(user_id=admin_id, level="warning",
                            message=f"{actor.username} requested a {action} on bola draw #{draw.id}",
                            link=f"/bola/draws/{draw.id}"))
    db.flush()
    return run


def approve(db: Session, actor: User, *, run_id: int, settings: SettingsStore) -> BolaRun:
    run = db.scalar(select(BolaRun).where(BolaRun.id == run_id).with_for_update())
    if run is None:
        raise NotFound("Settlement run not found.")
    if run.status != RunStatus.PENDING:
        raise Conflict("That request has already been processed.")
    if settings.get("fight.redeclare_requires_approval") and run.requested_by_id == actor.id:
        raise Forbidden("Maker-checker: a second Master/Admin must approve this correction.")
    run.approved_by_id = actor.id
    run.reviewed_at = utcnow()
    applied = _apply(db, run, actor, settings)
    audit.record(db, actor, "bola.redeclare.approve", entity_type="bola_run", entity_id=run.id,
                 after={"draw_id": run.draw_id, "version": run.version})
    return applied


def reject(db: Session, actor: User, *, run_id: int, note: str) -> BolaRun:
    run = db.scalar(select(BolaRun).where(BolaRun.id == run_id).with_for_update())
    if run is None:
        raise NotFound("Settlement run not found.")
    if run.status != RunStatus.PENDING:
        raise Conflict("That request has already been processed.")
    run.status = RunStatus.REJECTED
    run.approved_by_id = actor.id
    run.reviewed_at = utcnow()
    run.reason = f"{run.reason} | rejected: {(note or '')[:120]}"
    db.flush()
    audit.record(db, actor, "bola.correction.reject", entity_type="bola_run", entity_id=run.id,
                 reason=note)
    return run


def pending_runs(db: Session) -> list[BolaRun]:
    return list(db.scalars(select(BolaRun).where(BolaRun.status == RunStatus.PENDING)
                           .order_by(BolaRun.id.desc())).all())


def runs_for(db: Session, draw_id: int) -> list[BolaRun]:
    return list(db.scalars(select(BolaRun).where(BolaRun.draw_id == draw_id)
                           .order_by(BolaRun.version.desc())).all())


def latest_run(db: Session, draw_id: int) -> BolaRun | None:
    return db.scalar(select(BolaRun).where(BolaRun.draw_id == draw_id,
                                           BolaRun.status == RunStatus.APPLIED)
                     .order_by(BolaRun.version.desc()))
