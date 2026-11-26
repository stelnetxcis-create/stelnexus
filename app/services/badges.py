"""Achievements: hard-to-earn badges won by what an account does in the games.

* The catalog (names, conditions, icons) lives here, in code. The database only records which badge an account
  earned and when (``UserBadge``) and which up to three it shows next to its name (``UserBadge.slot``).
* A badge belongs to one game or to all of them. While the Admin has a game switched off (Variables > Games) its
  badges are hidden, are not awarded and are not shown next to names; they come back with the game, like its records.
* ``sync`` measures an account and awards whatever it has newly reached. It is cheap (a few aggregate queries), runs
  when the player opens Achievements or Profile and after a Scatter round, and ``stelnexus award-badges`` sweeps
  everybody. Badges are never taken back.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from html import escape
from zoneinfo import ZoneInfo

from markupsafe import Markup
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.core.money import fmt
from app.db.base import utcnow
from app.db.models import (BolaKind, BolaTicket, Notification, ScatterRound, Side, Ticket, TicketStatus, User,
                           UserBadge)

MANILA = ZoneInfo("Asia/Manila")
MAX_SHOWN = 3
STREAK_WINDOW = 5000          # most recent rounds / tickets scanned for streaks and busiest day

GAME_LABELS = {"sabong": "Sabong", "bola": "Number game", "scatter": "Scatter", "all": "All games"}
GAME_ORDER = ("sabong", "bola", "scatter", "all")

# tier -> (face, rim, label)
TIERS = {
    "rare": ("#2f6fe4", "#9cc2ff", "Rare"),
    "epic": ("#8a3fe0", "#d2a9ff", "Epic"),
    "legendary": ("#e08a12", "#ffe29a", "Legendary"),
    "mythic": ("#d6254a", "#ffd166", "Mythic"),
}
TIER_ORDER = ("rare", "epic", "legendary", "mythic")


@dataclass(frozen=True)
class Badge:
    key: str
    name: str
    desc: str
    game: str            # sabong | bola | scatter | all
    tier: str
    metric: str
    target: int
    unit: str = "count"  # count | money | x100 (hundredths of a bet) | badges


def _b(key, name, desc, game, tier, metric, target, unit="count") -> Badge:
    return Badge(key, name, desc, game, tier, metric, target, unit)


PESO = 100
CATALOG: tuple[Badge, ...] = (
    # ---- Sabong -------------------------------------------------------------------------------------------
    _b("pit_regular", "Pit Regular", "Place 500 bets in Sabong.", "sabong", "rare", "sabong_bets", 500),
    _b("pit_veteran", "Pit Veteran", "Place 2,500 bets in Sabong.", "sabong", "epic", "sabong_bets", 2500),
    _b("pit_legend", "Pit Legend", "Place 10,000 bets in Sabong.", "sabong", "legendary", "sabong_bets", 10000),
    _b("hot_hand", "Hot Hand", "Win 7 settled bets in a row.", "sabong", "rare", "sabong_streak", 7),
    _b("on_fire", "On Fire", "Win 12 settled bets in a row.", "sabong", "epic", "sabong_streak", 12),
    _b("untouchable", "Untouchable", "Win 20 settled bets in a row.", "sabong", "mythic", "sabong_streak", 20),
    _b("century_wins", "Century of Wins", "Win 100 Sabong bets.", "sabong", "epic", "sabong_wins", 100),
    _b("winners_circle", "Winner's Circle", "Win 500 Sabong bets.", "sabong", "legendary", "sabong_wins", 500),
    _b("big_catch", "Big Catch", "One Sabong bet pays PHP 50,000 or more.", "sabong", "epic",
       "sabong_best_payout", 50_000 * PESO, "money"),
    _b("jackpot_pit", "Jackpot Pit", "One Sabong bet pays PHP 250,000 or more.", "sabong", "legendary",
       "sabong_best_payout", 250_000 * PESO, "money"),
    _b("draw_hunter", "Draw Hunter", "Win 3 Draw bets.", "sabong", "epic", "sabong_draw_wins", 3),
    _b("high_roller_pit", "Max Bet Maniac", "Place a single Sabong bet of PHP 10,000 or more.", "sabong", "epic",
       "sabong_max_stake", 10_000 * PESO, "money"),
    _b("marathon", "Marathon Bettor", "Place 100 Sabong bets in one day.", "sabong", "epic", "sabong_busiest_day", 100),
    _b("ringside", "Ringside Seat", "Bet in 50 different events.", "sabong", "epic", "sabong_events", 50),
    _b("pit_whale", "Pit Whale", "Stake PHP 1,000,000 in total on Sabong.", "sabong", "legendary",
       "sabong_staked", 1_000_000 * PESO, "money"),
    # ---- Number game --------------------------------------------------------------------------------------
    _b("number_cruncher", "Number Cruncher", "Buy 250 Number game tickets.", "bola", "rare", "bola_tickets", 250),
    _b("lotto_lifer", "Lotto Lifer", "Buy 2,000 Number game tickets.", "bola", "epic", "bola_tickets", 2000),
    _b("lucky_strike", "Lucky Strike", "Win 3 Number game tickets.", "bola", "rare", "bola_wins", 3),
    _b("lucky_hand", "Lucky Hand", "Win 15 Number game tickets.", "bola", "epic", "bola_wins", 15),
    _b("fortunes_favorite", "Fortune's Favorite", "Win 50 Number game tickets.", "bola", "legendary",
       "bola_wins", 50),
    _b("back_to_back", "Back to Back", "Win 2 settled Number game tickets in a row.", "bola", "epic",
       "bola_streak", 2),
    _b("hat_trick", "Hat-trick of Fate", "Win 3 settled Number game tickets in a row.", "bola", "mythic",
       "bola_streak", 3),
    _b("casas_crown", "Casas Crown", "Win with a 3, 4 or 5 number Casas ticket.", "bola", "epic",
       "bola_casas_wins", 1),
    _b("bola_big_hit", "Big Hit", "One Number game ticket pays PHP 100,000 or more.", "bola", "epic",
       "bola_best_payout", 100_000 * PESO, "money"),
    _b("bola_comet", "Once in a Lifetime", "One Number game ticket pays PHP 1,000,000 or more.", "bola", "mythic",
       "bola_best_payout", 1_000_000 * PESO, "money"),
    _b("bola_whale", "Anchor of the Draw", "Stake PHP 500,000 in total on the Number game.", "bola", "legendary",
       "bola_staked", 500_000 * PESO, "money"),
    # ---- Scatter ------------------------------------------------------------------------------------------
    _b("spin_cycle", "Spin Cycle", "Play 1,000 Scatter rounds.", "scatter", "rare", "scatter_rounds", 1000),
    _b("reel_addict", "Cherry Picker", "Play 10,000 Scatter rounds.", "scatter", "epic", "scatter_rounds", 10000),
    _b("reel_legend", "Reel Legend", "Play 50,000 Scatter rounds.", "scatter", "legendary",
       "scatter_rounds", 50000),
    _b("hundred_x", "Hundred Club", "Win 100x your bet in one Scatter round.", "scatter", "rare",
       "scatter_best_x", 100 * 100, "x100"),
    _b("five_hundred_x", "Diamond Hands", "Win 500x your bet in one Scatter round.", "scatter", "epic",
       "scatter_best_x", 500 * 100, "x100"),
    _b("thousand_x", "Sunburst", "Win 1,000x your bet in one Scatter round.", "scatter", "legendary",
       "scatter_best_x", 1000 * 100, "x100"),
    _b("max_win", "Summit", "Reach the maximum win in one Scatter round.", "scatter", "mythic",
       "scatter_capped", 1),
    _b("multiplier_master", "Multiplier Master", "Bank a x50 multiplier in one Scatter bonus.", "scatter", "epic",
       "scatter_top_mult", 50),
    _b("multiplier_titan", "Atom Smasher", "Bank a x150 multiplier in one Scatter bonus.", "scatter", "legendary",
       "scatter_top_mult", 150),
    _b("retrigger_royalty", "Retrigger Royalty", "Play 25 or more free spins in a single round.", "scatter",
       "epic", "scatter_max_free", 25),
    _b("bonus_hunter", "Bonus Hunter", "Trigger the free spins bonus 50 times.", "scatter", "epic",
       "scatter_bonus_rounds", 50),
    _b("reel_streak", "Horseshoe", "Win 10 Scatter rounds in a row.", "scatter", "epic", "scatter_streak", 10),
    _b("big_wins_25", "Moonshot", "Win 10x or more, 25 times, in Scatter.", "scatter", "legendary",
       "scatter_big_wins", 25),
    _b("scatter_whale", "Vault Breaker", "Stake PHP 1,000,000 in total on Scatter.", "scatter", "legendary",
       "scatter_staked", 1_000_000 * PESO, "money"),
    # ---- Across games -------------------------------------------------------------------------------------
    _b("triple_threat", "Triple Threat", "Play every game: Sabong, Number game and Scatter.", "all", "rare",
       "games_played", 3),
    _b("all_rounder", "All-Rounder", "Play at least 100 times in each of the three games.", "all", "epic",
       "min_plays", 100),
    _b("million_club", "Million Club", "Stake PHP 5,000,000 in total across all games.", "all", "mythic",
       "lifetime_staked", 5_000_000 * PESO, "money"),
    _b("grand_master", "Grand Master", "Earn 20 other badges.", "all", "mythic", "badges_earned", 20, "badges"),
)
BY_KEY: dict[str, Badge] = {b.key: b for b in CATALOG}
assert len(BY_KEY) == len(CATALOG), "duplicate badge key"

SABONG_METRICS = ("sabong_bets", "sabong_wins", "sabong_staked", "sabong_best_payout", "sabong_max_stake",
                  "sabong_draw_wins", "sabong_events", "sabong_streak", "sabong_busiest_day")
BOLA_METRICS = ("bola_tickets", "bola_wins", "bola_staked", "bola_best_payout", "bola_casas_wins", "bola_streak")
SCATTER_METRICS = ("scatter_rounds", "scatter_staked", "scatter_best_x", "scatter_capped", "scatter_top_mult",
                   "scatter_max_free", "scatter_bonus_rounds", "scatter_big_wins", "scatter_streak")


# ---------------------------------------------------------------------------------------------------------------
# Which games are on
# ---------------------------------------------------------------------------------------------------------------
def games_on(settings) -> set[str]:
    return {g for g in ("sabong", "bola", "scatter") if settings.get(f"games.show_{g}")}


def listed(settings) -> list[Badge]:
    """The catalog as the player sees it: badges of a switched-off game are gone."""
    on = games_on(settings)
    return [b for b in CATALOG if b.game == "all" or b.game in on]


# ---------------------------------------------------------------------------------------------------------------
# Measuring an account
# ---------------------------------------------------------------------------------------------------------------
def _streak(outcomes) -> int:
    """Longest run of True. None is neutral (a refund neither extends nor breaks it); False breaks it."""
    best = cur = 0
    for won in outcomes:
        if won is None:
            continue
        cur = cur + 1 if won else 0
        best = max(best, cur)
    return best


def _local_day(moment: dt.datetime) -> dt.date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(MANILA).date()


def sabong_metrics(db: Session, uid: int) -> dict[str, int]:
    live = and_(Ticket.user_id == uid, Ticket.status != TicketStatus.CANCELED)
    settled = Ticket.status == TicketStatus.SETTLED
    won = and_(settled, Ticket.payout_cents > Ticket.stake_cents)
    row = db.execute(select(
        func.count(Ticket.id),
        func.coalesce(func.sum(case((won, 1), else_=0)), 0),
        func.coalesce(func.sum(case((settled, Ticket.stake_cents), else_=0)), 0),
        func.coalesce(func.max(Ticket.payout_cents), 0),
        func.coalesce(func.max(Ticket.stake_cents), 0),
        func.coalesce(func.sum(case((and_(won, Ticket.side == Side.DRAW), 1), else_=0)), 0),
        func.count(func.distinct(Ticket.event_id)),
    ).where(live)).one()
    recent = db.execute(select(Ticket.placed_at, Ticket.status, Ticket.stake_cents, Ticket.payout_cents)
                        .where(live).order_by(Ticket.placed_at.desc(), Ticket.id.desc())
                        .limit(STREAK_WINDOW)).all()
    recent.reverse()                                            # oldest first
    outcomes = []
    per_day: dict[dt.date, int] = {}
    for placed_at, status, stake, payout in recent:
        per_day[_local_day(placed_at)] = per_day.get(_local_day(placed_at), 0) + 1
        if status != TicketStatus.SETTLED:
            continue
        outcomes.append(True if payout > stake else (None if payout == stake else False))
    return {"sabong_bets": int(row[0]), "sabong_wins": int(row[1]), "sabong_staked": int(row[2]),
            "sabong_best_payout": int(row[3]), "sabong_max_stake": int(row[4]), "sabong_draw_wins": int(row[5]),
            "sabong_events": int(row[6]), "sabong_streak": _streak(outcomes),
            "sabong_busiest_day": max(per_day.values(), default=0)}


def bola_metrics(db: Session, uid: int) -> dict[str, int]:
    live = and_(BolaTicket.user_id == uid, BolaTicket.status != TicketStatus.CANCELED, BolaTicket.legacy.is_(False))
    settled = BolaTicket.status == TicketStatus.SETTLED
    won = and_(settled, BolaTicket.payout_cents > BolaTicket.stake_cents)
    multi = BolaTicket.kind.in_([BolaKind.CASAS_3, BolaKind.CASAS_6, BolaKind.CASAS_10])
    row = db.execute(select(
        func.count(BolaTicket.id),
        func.coalesce(func.sum(case((won, 1), else_=0)), 0),
        func.coalesce(func.sum(case((settled, BolaTicket.stake_cents), else_=0)), 0),
        func.coalesce(func.max(BolaTicket.payout_cents), 0),
        func.coalesce(func.sum(case((and_(won, multi), 1), else_=0)), 0),
    ).where(live)).one()
    recent = db.execute(select(BolaTicket.status, BolaTicket.stake_cents, BolaTicket.payout_cents)
                        .where(live).order_by(BolaTicket.placed_at.desc(), BolaTicket.id.desc())
                        .limit(STREAK_WINDOW)).all()
    recent.reverse()
    outcomes = [True if payout > stake else (None if payout == stake else False)
                for status, stake, payout in recent if status == TicketStatus.SETTLED]
    return {"bola_tickets": int(row[0]), "bola_wins": int(row[1]), "bola_staked": int(row[2]),
            "bola_best_payout": int(row[3]), "bola_casas_wins": int(row[4]), "bola_streak": _streak(outcomes)}


def scatter_metrics(db: Session, uid: int) -> dict[str, int]:
    mine = ScatterRound.user_id == uid
    row = db.execute(select(
        func.count(ScatterRound.id),
        func.coalesce(func.sum(ScatterRound.stake_cents), 0),
        func.coalesce(func.max(ScatterRound.win_x100), 0),
        func.coalesce(func.sum(case((ScatterRound.capped.is_(True), 1), else_=0)), 0),
        func.coalesce(func.max(ScatterRound.top_multiplier), 0),
        func.coalesce(func.max(ScatterRound.free_spins), 0),
        func.coalesce(func.sum(case((ScatterRound.free_spins > 0, 1), else_=0)), 0),
        func.coalesce(func.sum(case((ScatterRound.win_x100 >= 1000, 1), else_=0)), 0),
    ).where(mine)).one()
    recent = db.execute(select(ScatterRound.win_cents).where(mine).order_by(ScatterRound.id.desc())
                        .limit(STREAK_WINDOW)).scalars().all()
    outcomes = [w > 0 for w in reversed(recent)]
    return {"scatter_rounds": int(row[0]), "scatter_staked": int(row[1]), "scatter_best_x": int(row[2]),
            "scatter_capped": int(row[3]), "scatter_top_mult": int(row[4]), "scatter_max_free": int(row[5]),
            "scatter_bonus_rounds": int(row[6]), "scatter_big_wins": int(row[7]),
            "scatter_streak": _streak(outcomes)}


def measure(db: Session, uid: int, games: set[str] | None = None) -> dict[str, int]:
    """Every number the catalog needs for ``uid``. ``games`` limits the work to those games (None = all three)."""
    games = {"sabong", "bola", "scatter"} if games is None else games
    out = {m: 0 for m in SABONG_METRICS + BOLA_METRICS + SCATTER_METRICS}
    if "sabong" in games:
        out.update(sabong_metrics(db, uid))
    if "bola" in games:
        out.update(bola_metrics(db, uid))
    if "scatter" in games:
        out.update(scatter_metrics(db, uid))
    plays = (out["sabong_bets"], out["bola_tickets"], out["scatter_rounds"])
    out["games_played"] = sum(1 for p in plays if p > 0)
    out["min_plays"] = min(plays)
    out["lifetime_staked"] = out["sabong_staked"] + out["bola_staked"] + out["scatter_staked"]
    return out


# ---------------------------------------------------------------------------------------------------------------
# Awarding
# ---------------------------------------------------------------------------------------------------------------
def earned_rows(db: Session, uid: int) -> dict[str, UserBadge]:
    return {r.badge_key: r for r in db.scalars(select(UserBadge).where(UserBadge.user_id == uid)).all()}


def sync(db: Session, user: User, settings, *, games: set[str] | None = None) -> list[Badge]:
    """Award every badge ``user`` has newly reached. ``games`` narrows the measuring (a Scatter round only needs
    Scatter); the cross-game badges are skipped then. Returns the new badges (also announced in the bell)."""
    if user is None or not user.has_wallet:
        return []
    on = games_on(settings)
    scope = on if games is None else (on & games)
    have = earned_rows(db, user.id)
    wanted = [b for b in CATALOG if b.key not in have and b.metric != "badges_earned"
              and (b.game in scope or (b.game == "all" and games is None))]
    if not wanted:
        return []
    metrics = measure(db, user.id, None if games is None else scope)   # cross-game badges count every game
    new = [b for b in wanted if metrics.get(b.metric, 0) >= b.target]
    if games is None:                                   # the meta badge counts what was just earned too
        total = len(have) + len(new)
        meta = BY_KEY["grand_master"]
        if meta.key not in have and total >= meta.target:
            new.append(meta)
    now = utcnow()
    for b in new:
        db.add(UserBadge(user_id=user.id, badge_key=b.key, earned_at=now))
        db.add(Notification(user_id=user.id, level="success", message=f"Achievement unlocked: {b.name}",
                            link="/achievements"))
    if new:
        db.flush()
    return new


def sweep(db: Session, settings, *, batch: int = 200) -> int:
    """Award badges to every wallet account (the ``award-badges`` command). Returns how many were handed out."""
    total = 0
    last = 0
    while True:
        users = db.scalars(select(User).where(User.id > last).order_by(User.id).limit(batch)).all()
        if not users:
            return total
        for user in users:
            last = user.id
            total += len(sync(db, user, settings))
        db.flush()


# ---------------------------------------------------------------------------------------------------------------
# Showcase: up to three badges next to the name
# ---------------------------------------------------------------------------------------------------------------
def showcase_for(db: Session, user_ids, settings) -> dict[int, list[Badge]]:
    """{user_id: [Badge, ...]} in slot order, only for badges of games that are on."""
    ids = {i for i in user_ids if i}
    if not ids:
        return {}
    visible = {b.key for b in listed(settings)}
    out: dict[int, list[Badge]] = {}
    rows = db.execute(select(UserBadge.user_id, UserBadge.badge_key)
                      .where(UserBadge.user_id.in_(ids), UserBadge.slot.is_not(None))
                      .order_by(UserBadge.user_id, UserBadge.slot)).all()
    for uid, key in rows:
        if key in visible:
            out.setdefault(uid, []).append(BY_KEY[key])
    return out


def my_showcase(db: Session, user: User, settings) -> list[Badge]:
    return showcase_for(db, [user.id], settings).get(user.id, [])


def set_showcase(db: Session, user: User, keys: list[str], settings) -> list[Badge]:
    """Replace the badges shown next to ``user``'s name. At most three, all earned, all of a game that is on."""
    keys = [k for k in dict.fromkeys(k.strip() for k in keys if k and k.strip())]
    if len(keys) > MAX_SHOWN:
        raise ValidationError(f"You can show up to {MAX_SHOWN} badges next to your name.")
    have = earned_rows(db, user.id)
    visible = {b.key for b in listed(settings)}
    for key in keys:
        if key not in BY_KEY or key not in have or key not in visible:
            raise ValidationError("You can only show badges you have earned.")
    for row in have.values():                           # clear first so a swap never trips the unique slot
        if row.slot is not None:
            row.slot = None
    db.flush()
    for slot, key in enumerate(keys):
        have[key].slot = slot
    db.flush()
    return [BY_KEY[k] for k in keys]


# ---------------------------------------------------------------------------------------------------------------
# Progress, for the Achievements page
# ---------------------------------------------------------------------------------------------------------------
def describe_amount(b: Badge, value: int) -> str:
    if b.unit == "money":
        return fmt(value)
    if b.unit == "x100":
        return f"{value / 100:g}x"
    return f"{value:,}"


def board(db: Session, user: User, settings) -> dict:
    """Everything the Achievements page shows: every listed badge with its state and progress."""
    have = earned_rows(db, user.id)
    metrics = measure(db, user.id)
    metrics["badges_earned"] = len([k for k in have if k != "grand_master"])
    rows = []
    for b in listed(settings):
        mine = have.get(b.key)
        value = min(metrics.get(b.metric, 0), b.target)
        rows.append({"badge": b, "earned": mine is not None, "at": mine.earned_at if mine else None,
                     "slot": mine.slot if mine else None, "value": value,
                     "pct": 100 if mine else int(value * 100 / b.target) if b.target else 0,
                     "now_label": describe_amount(b, value), "target_label": describe_amount(b, b.target)})
    shown = [r for r in rows if r["earned"]]
    return {"rows": rows, "earned": len(shown), "total": len(rows),
            "by_tier": {t: sum(1 for r in shown if r["badge"].tier == t) for t in TIER_ORDER},
            "showcase": sorted((r for r in shown if r["slot"] is not None), key=lambda r: r["slot"])}


# ---------------------------------------------------------------------------------------------------------------
# Icons: one medallion, one unique glyph per badge (24x24, drawn with strokes)
# ---------------------------------------------------------------------------------------------------------------
def _dots(*pts, r=1.1) -> str:
    return "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="currentColor" stroke="none"/>' for x, y in pts)


def _rays(n: int, r1: float, r2: float) -> str:
    import math
    out = []
    for i in range(n):
        a = 2 * math.pi * i / n
        out.append(f"M{12 + r1 * math.cos(a):.2f} {12 + r1 * math.sin(a):.2f}"
                   f"L{12 + r2 * math.cos(a):.2f} {12 + r2 * math.sin(a):.2f}")
    return f'<path d="{"".join(out)}"/>'


GLYPHS: dict[str, str] = {
    # Sabong
    "pit_regular": '<path d="M20 4C11 4 6 9 6 17v3M6 20c5-1 9-5 11-11M9 14h6"/>',
    "pit_veteran": '<path d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6zM9 11l3 3 3-3"/>',
    "pit_legend": '<path d="M3 8l4 4 5-7 5 7 4-4v10H3z"/>',
    "hot_hand": '<path d="M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-2 1-3 2-4 0 2 1 3 2 3 0-3-1-5 1-9z"/>',
    "on_fire": '<path d="M13 2L5 14h6l-1 8 8-12h-6z"/>',
    "untouchable": '<path d="M12 12c-2-3-4-5-6.5-5S2 9 2 12s1.5 5 3.5 5S10 15 12 12s4-5 6.5-5S22 9 22 12s-1.5 5-3.5 5S14 15 12 12z"/>',
    "century_wins": '<path d="M8 4h8v5a4 4 0 0 1-8 0zM8 6H4v1a4 4 0 0 0 4 4M16 6h4v1a4 4 0 0 1-4 4M12 13v4M8 20h8M10 17h4"/>',
    "winners_circle": '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
    "big_catch": '<circle cx="12" cy="12" r="9"/><path d="M9.5 7.5v9M9.5 7.5h3.5a2.8 2.8 0 0 1 0 5.6H9.5M8 10h6"/>',
    "jackpot_pit": '<path d="M6 4h12l4 5-10 12L2 9zM2 9h20M9 4L7 9l5 12 5-12-2-5"/>',
    "draw_hunter": '<circle cx="12" cy="12" r="7"/><path d="M12 2v5M12 17v5M2 12h5M17 12h5"/>' + _dots((12, 12)),
    "high_roller_pit": '<rect x="4" y="4" width="16" height="16" rx="3"/>' + _dots((8.5, 8.5), (15.5, 8.5), (12, 12), (8.5, 15.5), (15.5, 15.5), r=1.2),
    "marathon": '<path d="M7 3h10M7 21h10M8 3v4l4 5-4 5v4M16 3v4l-4 5 4 5v4"/>',
    "ringside": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "pit_whale": '<path d="M2 9c3-3 5 3 8 0s5 3 8 0 3 1 4 0M2 15c3-3 5 3 8 0s5 3 8 0 3 1 4 0"/>',
    # Number game
    "number_cruncher": '<path d="M9 3L7 21M17 3l-2 18M4 9h17M3 15h17"/>',
    "lotto_lifer": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5"/>',
    "lucky_strike": '<circle cx="9" cy="9" r="3.5"/><circle cx="15" cy="9" r="3.5"/><circle cx="9" cy="15" r="3.5"/><circle cx="15" cy="15" r="3.5"/><path d="M12 12l5 9"/>',
    "lucky_hand": '<path d="M12 2v6M12 16v6M2 12h6M16 12h6M5 5l4 4M15 15l4 4M19 5l-4 4M9 15l-4 4"/>',
    "fortunes_favorite": '<path d="M12 21s-8-5-8-11a4.5 4.5 0 0 1 8-2.5A4.5 4.5 0 0 1 20 10c0 6-8 11-8 11z"/>',
    "back_to_back": '<path d="M5 17l7-7 7 7M5 11l7-7 7 7"/>',
    "hat_trick": '<path d="M12 3l10 17H2z"/><circle cx="12" cy="14" r="2.5"/>',
    "casas_crown": '<circle cx="9" cy="9" r="5"/><circle cx="15" cy="9" r="5"/><circle cx="12" cy="15" r="5"/>',
    "bola_big_hit": '<path d="M12 2c3 2 5 6 5 10l-2 3H9l-2-3c0-4 2-8 5-10zM9 15l-3 4 4-1M15 15l3 4-4-1"/><circle cx="12" cy="9" r="1.5"/>',
    "bola_comet": '<circle cx="17" cy="7" r="4"/><path d="M13 11L3 21M12 5L5 12M19 12l-7 7"/>',
    "bola_whale": '<circle cx="12" cy="5" r="2"/><path d="M12 7v14M6 12H3a9 9 0 0 0 18 0h-3M9 11h6"/>',
    # Scatter
    "spin_cycle": '<path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/>',
    "reel_addict": '<circle cx="8" cy="17" r="3"/><circle cx="16" cy="16" r="3"/><path d="M8 14c0-5 3-9 6-11M16 13c0-4-1-7-2-10M14 3c3 0 5 1 7 3"/>',
    "reel_legend": '<rect x="3" y="4" width="16" height="16" rx="2"/><path d="M3 10h16M8 10v10M13 10v10M19 8h3v6"/>',
    "hundred_x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "five_hundred_x": '<path d="M12 2l8 10-8 10-8-10z"/>',
    "thousand_x": '<circle cx="12" cy="12" r="4"/>' + _rays(8, 6.5, 10),
    "max_win": '<path d="M2 20l7-12 4 6 3-4 6 10zM9 8V3l4 1.5L9 6"/>',
    "multiplier_master": '<ellipse cx="12" cy="12" rx="10" ry="4"/><ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(60 12 12)"/><ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(120 12 12)"/>' + _dots((12, 12), r=1.6),
    "multiplier_titan": '<path d="M12 2l3 7 7 1-5 5 1.5 7L12 18l-6.5 4L7 15 2 10l7-1z"/>',
    "retrigger_royalty": '<path d="M12 3c4 5 8 7 8 11a4 4 0 0 1-7 2.5c0 2 .5 3.5 2 5H9c1.5-1.5 2-3 2-5A4 4 0 0 1 4 14c0-4 4-6 8-11z"/>',
    "bonus_hunter": '<rect x="3" y="8" width="18" height="4"/><rect x="5" y="12" width="14" height="9"/><path d="M12 8v13M12 8C9 8 8 3 11 3c2 0 1 5 1 5s0-5 2-5c3 0 2 5-2 5"/>',
    "reel_streak": '<path d="M5 20V10a7 7 0 0 1 14 0v10h-4V10a3 3 0 0 0-6 0v10z"/>',
    "big_wins_25": '<path d="M20 14A8 8 0 1 1 10 4a6.5 6.5 0 0 0 10 10z"/>',
    "scatter_whale": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/><circle cx="12" cy="16" r="1.5"/>',
    # Across games
    "triple_threat": '<path d="M12 2l9 5v10l-9 5-9-5V7z"/><path d="M12 7l5 9H7z"/>',
    "all_rounder": '<circle cx="12" cy="12" r="9"/><path d="M16 8l-2.5 5.5L8 16l2.5-5.5z"/>',
    "million_club": '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M6 10v.01M18 14v.01"/>',
    "grand_master": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>',
}
assert set(GLYPHS) == set(BY_KEY), set(GLYPHS) ^ set(BY_KEY)

_SHIELD = "M24 2l19 8v14c0 11-8 19-19 22C13 43 5 35 5 24V10z"


def icon(key: str, size: int = 24, *, locked: bool = False) -> Markup:
    """The badge as inline SVG: a tier-coloured shield with the badge's own glyph. ``locked`` greys it out."""
    badge = BY_KEY.get(key)
    if badge is None:
        return Markup("")
    face, rim, label = TIERS[badge.tier]
    if locked:
        face, rim = "#2b303c", "#596072"
    title = escape(f"{badge.name}" + (" (locked)" if locked else f" · {label}"))
    glyph_opacity = "0.45" if locked else "1"
    return Markup(
        f'<svg class="badge-ico" viewBox="0 0 48 48" width="{size}" height="{size}" role="img" aria-label="{title}"'
        f' style="flex:none;vertical-align:middle"><title>{title}</title>'
        f'<path d="{_SHIELD}" fill="{face}" stroke="{rim}" stroke-width="2.4" stroke-linejoin="round"/>'
        f'<path d="M24 6.5l15 6.3v11c0 8.6-6 14.9-15 17.8" fill="none" stroke="#fff" stroke-opacity=".18" stroke-width="2"/>'
        f'<g transform="translate(12 12.5)" fill="none" stroke="#fff" stroke-opacity="{glyph_opacity}" color="#fff"'
        f' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" opacity="{glyph_opacity}">'
        f'{GLYPHS[key]}</g></svg>')


def strip(badges, size: int = 18) -> Markup:
    """The (up to three) icons shown next to a name."""
    if not badges:
        return Markup("")
    items = "".join(f'<span class="name-badge" title="{escape(b.name)}">{icon(b.key, size)}</span>'
                    for b in list(badges)[:MAX_SHOWN])
    return Markup(f'<span class="name-badges" style="display:inline-flex;gap:2px;margin-left:6px;'
                  f'vertical-align:middle;align-items:center">{items}</span>')
