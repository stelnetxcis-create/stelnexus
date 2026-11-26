"""Scatter (the slot): the money side. The maths lives in ``app/games/scatter_engine.py`` and is never
touched here; this module only decides WHETHER a spin may happen and moves the money exactly as the round says.

    spin   wallet(account) +stake | scatter house -stake      kind "scatter.stake"
    win    scatter house +win     | wallet(account) -win       kind "scatter.win"   (only when the round paid)

Both postings and the ``ScatterRound`` row are one database transaction: a spin either happened completely
(stake taken, win paid, round recorded) or not at all. One round = the opening spin plus any free spins it started,
all resolved at once; what the browser shows afterwards is a replay.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, Forbidden, InsufficientFunds, LockedState, RateLimited, ValidationError
from app.core.money import fmt
from app.core.rbac import has_global_scope, scope_ids
from app.core.security import random_code
from app.core.settings_registry import SettingsStore
from app.db.base import utcnow
from app.db.models import ScatterRound, TicketStatus, User
from app.games import scatter_engine as engine
from app.services import accounts, ledger, players

NOTABLE_WIN_X100 = 1000      # rounds paying at least 10x keep their whole replay; every round keeps its seed


# ---- settings ----------------------------------------------------------------------------------------------
def enabled(settings: SettingsStore) -> bool:
    return bool(settings.get("scatter.enabled")) and bool(settings.get("games.show_scatter"))


def assert_enabled(settings: SettingsStore) -> None:
    if not enabled(settings):
        raise LockedState("Scatter is switched off right now.")


def bet_ladder(settings: SettingsStore) -> list[int]:
    """The bets a player may pick, in centavos, ascending, inside the min/max. Never empty: a broken setting
    falls back to the minimum bet so the game stays playable instead of throwing."""
    low = int(settings.get("scatter.min_bet_cents"))
    high = max(int(settings.get("scatter.max_bet_cents")), low)
    steps: set[int] = set()
    for part in str(settings.get("scatter.bet_ladder") or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            cents = int(part) * 100
            if low <= cents <= high:
                steps.add(cents)
    return sorted(steps) or [low]


def config(settings: SettingsStore) -> dict:
    return {"bets": bet_ladder(settings), "max_win_x": int(settings.get("scatter.max_win_x")),
            "rtp": engine.MEASURED_RTP, "min_gap": int(settings.get("scatter.min_seconds_between_spins") or 0)}


def validate_bet(settings: SettingsStore, bet_cents) -> int:
    if isinstance(bet_cents, bool) or not isinstance(bet_cents, int):
        raise ValidationError("Pick one of the bet sizes.")
    if bet_cents not in bet_ladder(settings):
        raise ValidationError("That bet size is not available.")
    return bet_cents


# ---- the spin ----------------------------------------------------------------------------------------------
def _code(db: Session) -> str:
    for _ in range(20):
        code = random_code(10)
        if not db.scalar(select(ScatterRound.id).where(ScatterRound.code == code)):
            return code
    raise Conflict("Could not allocate a round code, please retry.")


def win_cents_of(bet_cents: int, win_x100: int) -> int:
    """Whole centavos, rounded down (never in the player's favour by accident, never by a fraction of a cent)."""
    return bet_cents * win_x100 // 100


def spin(db: Session, actor: User, *, bet_cents, settings: SettingsStore, idempotency_key: str | None = None,
         ip: str | None = None) -> tuple[ScatterRound, dict, int]:
    """Play one round for ``actor`` from their own credit. Returns (round, engine result, balance after).

    A repeated ``idempotency_key`` (a double tap, a retried request) returns the first round instead of charging
    twice; the same key with a different bet is refused."""
    if idempotency_key:
        done = db.scalar(select(ScatterRound).where(ScatterRound.idempotency_key == idempotency_key))
        if done is not None:
            return _replay(db, done, actor, bet_cents)

    assert_enabled(settings)
    if not actor.has_wallet:
        raise Forbidden(f"A {actor.role_label} account has no balance and cannot play.")
    bet = validate_bet(settings, bet_cents)

    gap = int(settings.get("scatter.min_seconds_between_spins") or 0)
    if gap:
        last = db.scalar(select(func.max(ScatterRound.placed_at)).where(ScatterRound.user_id == actor.id))
        if last is not None:
            last = last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)
            wait = gap - (utcnow() - last).total_seconds()
            if wait > 0:
                raise RateLimited(f"Easy does it: next spin in {int(wait) + 1}s.")

    players.assert_account_can_bet(db, actor, bet, settings)
    wallet = accounts.wallet_for_user(db, actor)
    if wallet.balance_cents < bet:
        raise InsufficientFunds(f"Not enough credit: this spin costs {fmt(bet)} and you have "
                                f"{fmt(wallet.balance_cents)}.")
    house = accounts.scatter_house(db)

    cap_x = int(settings.get("scatter.max_win_x"))
    seed = engine.new_seed()
    result = engine.play_seeded(seed, max_win_x=cap_x)
    facts = engine.summary(result)
    win = win_cents_of(bet, result["win"])

    rnd = ScatterRound(
        code=_code(db), user_id=actor.id, stake_cents=bet, win_cents=win, win_x100=result["win"],
        status=TicketStatus.SETTLED, spins=facts["spins"], free_spins=facts["free_spins"],
        top_multiplier=facts["top_multiplier"], capped=facts["capped"], seed=seed,
        engine_version=engine.ENGINE_VERSION, max_win_x=cap_x, placed_ip=(ip or "")[:64] or None,
        result=result if (result["bonus"] or result["win"] >= NOTABLE_WIN_X100) else None,
        idempotency_key=idempotency_key)
    db.add(rnd)
    db.flush()

    stake_txn = ledger.post(
        db, kind="scatter.stake", entries=[(wallet, +bet), (house, -bet)], actor=actor,
        memo=f"Scatter {rnd.code}", ref_type="scatter_round", ref_id=rnd.id,
        idempotency_key=f"scatter-stake:{rnd.code}")
    rnd.stake_txn_id = stake_txn.id
    if win:
        win_txn = ledger.post(
            db, kind="scatter.win", entries=[(house, +win), (wallet, -win)], actor=actor,
            memo=f"Scatter {rnd.code} · {result['win'] / 100:g}x", ref_type="scatter_round", ref_id=rnd.id,
            idempotency_key=f"scatter-win:{rnd.code}")
        rnd.win_txn_id = win_txn.id
    db.flush()
    return rnd, result, accounts.wallet_for_user(db, actor).balance_cents


def _replay(db: Session, rnd: ScatterRound, actor: User, bet_cents) -> tuple[ScatterRound, dict, int]:
    if rnd.user_id != actor.id or rnd.stake_cents != bet_cents:
        raise Conflict("That request id was already used for a different spin.")
    result = rnd.result or engine.play_seeded(rnd.seed, max_win_x=rnd.max_win_x)
    return rnd, result, accounts.wallet_for_user(db, actor).balance_cents


def verify(rnd: ScatterRound) -> bool:
    """Does the stored seed still produce the stored win? The check an operator runs on a disputed round."""
    again = engine.play_seeded(rnd.seed, max_win_x=rnd.max_win_x)
    return again["win"] == rnd.win_x100 and (rnd.result is None or again == rnd.result)


# ---- reading rounds ----------------------------------------------------------------------------------------
def _scope(stmt, db: Session, actor: User):
    preview_pid = getattr(actor, "preview_player_id", None)
    if preview_pid is not None or not has_global_scope(actor):
        stmt = stmt.where(ScatterRound.user_id.in_(scope_ids(db, actor)))
    return stmt


def recent(db: Session, user: User, limit: int = 12) -> list[ScatterRound]:
    return list(db.scalars(select(ScatterRound).where(ScatterRound.user_id == user.id)
                           .order_by(ScatterRound.id.desc()).limit(limit)))


def list_rounds(db: Session, actor: User, *, search: str | None = None, only_wins: bool = False,
                since: dt.datetime | None = None, until: dt.datetime | None = None,
                limit: int = 100, offset: int = 0) -> list[ScatterRound]:
    stmt = _scope(select(ScatterRound), db, actor)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(ScatterRound.code.ilike(like))
    if only_wins:
        stmt = stmt.where(ScatterRound.win_cents > 0)
    if since is not None:
        stmt = stmt.where(ScatterRound.placed_at >= since)
    if until is not None:
        stmt = stmt.where(ScatterRound.placed_at < until)
    return list(db.scalars(stmt.order_by(ScatterRound.id.desc()).limit(max(1, min(limit, 500)))
                           .offset(max(offset, 0))))


def totals(db: Session, actor: User, since: dt.datetime, until: dt.datetime) -> dict:
    """Rounds, stakes and wins for [since, until) inside the viewer's reach. house = what the game kept."""
    stmt = _scope(select(func.count(ScatterRound.id), func.coalesce(func.sum(ScatterRound.stake_cents), 0),
                         func.coalesce(func.sum(ScatterRound.win_cents), 0))
                  .where(ScatterRound.placed_at >= since, ScatterRound.placed_at < until), db, actor)
    n, staked, paid = db.execute(stmt).one()
    return {"rounds": int(n), "staked": int(staked), "paid": int(paid), "house": int(staked) - int(paid)}


def by_player(db: Session, actor: User, since: dt.datetime, until: dt.datetime) -> list[dict]:
    stmt = _scope(select(User.username, func.count(ScatterRound.id), func.coalesce(func.sum(ScatterRound.stake_cents), 0),
                         func.coalesce(func.sum(ScatterRound.win_cents), 0))
                  .join(User, User.id == ScatterRound.user_id)
                  .where(ScatterRound.placed_at >= since, ScatterRound.placed_at < until)
                  .group_by(User.username).order_by(func.sum(ScatterRound.stake_cents).desc()), db, actor)
    return [{"who": u, "rounds": int(n), "staked": int(s), "paid": int(p), "house": int(s) - int(p)}
            for u, n, s, p in db.execute(stmt).all()]


def by_day(db: Session, actor: User, since: dt.datetime, until: dt.datetime) -> dict[dt.date, dict]:
    out: dict[dt.date, dict] = {}
    for r in db.execute(_scope(select(ScatterRound.placed_at, ScatterRound.stake_cents, ScatterRound.win_cents)
                               .where(ScatterRound.placed_at >= since, ScatterRound.placed_at < until), db, actor)):
        placed = r[0] if r[0].tzinfo else r[0].replace(tzinfo=dt.timezone.utc)
        row = out.setdefault(placed.astimezone(MANILA).date(), {"rounds": 0, "staked": 0, "paid": 0})
        row["rounds"] += 1
        row["staked"] += int(r[1])
        row["paid"] += int(r[2])
    return out


MANILA = dt.timezone(dt.timedelta(hours=8))
