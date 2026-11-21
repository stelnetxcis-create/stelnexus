"""The Declarator Desk: ONE live board of every game that needs a person, across all events and Bola draws.

What it is for
--------------
A Declarator's question is never "which event am I in?" but "what needs me right now?". This module
answers it. It looks at every open event (its current game: the lowest-numbered game not yet declared)
and at every Bola draw that is open, locked or about to start, and files each into a *lane*:

    air    the game is on air (staff pressed Start): waiting for its result
    due    waiting for a result and not on air: a locked game, or a Bola draw whose time has come
    open   the current game is in pre-show: betting and chat are open
    next   not yet: a Bola draw before its time, or an event still in draft (a Declarator may open it)

Money-free by design
--------------------
The board carries names, numbers, states and times. It never carries a stake, a pool, a ticket count or a
payout, for ANY role: the desk is a result-entry tool, and a Declarator must not see totals (see
``rbac.hides_totals``). Keeping the payload money-free for everyone (Admin included) means there is no
code path here that could leak a total by mistake.

The same dict feeds the page (embedded for the first paint) and ``GET /_live/declarator`` (the poll), so
what a Declarator sees on load and after every refresh can never disagree.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.rbac import is_top
from app.core.settings_registry import SettingsStore
from app.db.base import utcnow
from app.db.models import (
    BolaDraw, DrawStatus, Event, EventStatus, Fight, FightStatus, User,
)
from app.services import bola, events

MAX_EVENTS = 30          # open events on the board at once
MAX_DRAFTS = 10          # draft events a Declarator may open
MAX_DRAWS = 60           # Bola draws on the board at once
RECENT_HOURS = 24        # how far back "recently declared" looks
RECENT_LIMIT = 20
SOON = dt.timedelta(hours=3)    # a scheduled draw shows up this long before its time

# Order of the lanes on the board, most urgent first (the browser sorts by this).
LANES = ("air", "due", "open", "next")


def _ms(when: dt.datetime | None) -> int | None:
    return int(when.timestamp() * 1000) if when else None


def _game(f: Fight, cur_id: int | None) -> dict:
    """One game, as small as it can be (a 200-game event is polled every few seconds)."""
    return {
        "id": f.id, "n": f.number, "st": f.status.value,
        "ph": events.stream_phase(f, cur_id),
        "res": f.result.value if f.result else "",
        "nm": f.entry_name_meron, "nw": f.entry_name_wala,
        "lla": f.llamado_side or "",
        "sd": f.started_at is not None, "sa": _ms(f.started_at), "lk": _ms(f.locked_at),
        "at": _ms(f.declared_at), "cam": bool(f.proof_file),
    }


def _lane_of_game(cur: Fight) -> str:
    if cur.started_at is not None:
        return "air"
    if cur.status == FightStatus.LOCKED:
        return "due"
    return "open"


def _sabong(db: Session, now: dt.datetime) -> list[dict]:
    """Open events with their games, plus draft events a Declarator may open."""
    open_events = list(db.scalars(
        select(Event).options(selectinload(Event.location))
        .where(Event.status == EventStatus.OPEN)
        .order_by(Event.event_date.desc(), Event.id.desc()).limit(MAX_EVENTS)).all())
    drafts = list(db.scalars(
        select(Event).options(selectinload(Event.location))
        .where(Event.status == EventStatus.DRAFT,
               Event.event_date >= (now - dt.timedelta(days=1)).date())
        .order_by(Event.event_date.asc(), Event.id.asc()).limit(MAX_DRAFTS)).all())

    by_event: dict[int, list[Fight]] = {e.id: [] for e in open_events}
    if by_event:
        for f in db.scalars(select(Fight).where(Fight.event_id.in_(list(by_event)))
                            .order_by(Fight.event_id, Fight.number)).all():
            by_event[f.event_id].append(f)

    out: list[dict] = []
    for e in open_events:
        fights = by_event[e.id]
        cur = next((f for f in fights if f.status in (FightStatus.OPEN, FightStatus.LOCKED)), None)
        cur_id = cur.id if cur else None
        out.append({
            "id": e.id, "title": e.title, "date": e.event_date.isoformat(),
            "venue": e.location.name if e.location else "", "st": "open",
            "cur": cur_id, "lane": _lane_of_game(cur) if cur else "",
            "total": len(fights),
            "done": sum(1 for f in fights if f.status in (FightStatus.SETTLED, FightStatus.VOID)),
            "games": [_game(f, cur_id) for f in fights],
        })
    for e in drafts:
        out.append({
            "id": e.id, "title": e.title, "date": e.event_date.isoformat(),
            "venue": e.location.name if e.location else "", "st": "draft",
            "cur": None, "lane": "next", "total": e.fight_count, "done": 0, "games": [],
        })
    return out


def _draws(db: Session, now: dt.datetime) -> list[dict]:
    """Bola draws that are, or are about to be, waiting for the two balls."""
    horizon = now + SOON
    live = (DrawStatus.OPEN, DrawStatus.LOCKED)
    rows = db.scalars(
        select(BolaDraw).options(selectinload(BolaDraw.location))
        .where(BolaDraw.legacy.is_(False))
        .where((BolaDraw.status.in_(live))
               | ((BolaDraw.status == DrawStatus.SCHEDULED) & (BolaDraw.scheduled_at <= horizon)
                  & (BolaDraw.scheduled_at >= now - dt.timedelta(days=1))))
        .order_by(BolaDraw.scheduled_at.asc(), BolaDraw.id.asc()).limit(MAX_DRAWS)).all()
    out = []
    for d in rows:
        due = now >= d.scheduled_at and d.status in live
        out.append({
            "id": d.id, "title": d.title, "venue": d.location.name if d.location else "",
            "slot": d.slot, "st": d.status.value, "at": _ms(d.scheduled_at), "cut": _ms(d.cutoff_at),
            "due": due, "lane": "due" if due else "next", "cam": bool(d.proof_file),
        })
    return out


def _recent(db: Session, now: dt.datetime, *, sabong: bool, with_bola: bool) -> list[dict]:
    """What was declared lately: a quick way to check your own work, newest first."""
    since = now - dt.timedelta(hours=RECENT_HOURS)
    rows: list[dict] = []
    if sabong:
        for f, title in db.execute(
                select(Fight, Event.title).join(Event, Event.id == Fight.event_id)
                .where(Fight.status.in_((FightStatus.SETTLED, FightStatus.VOID)),
                       Fight.declared_at.is_not(None), Fight.declared_at >= since)
                .order_by(Fight.declared_at.desc()).limit(RECENT_LIMIT)).all():
            rows.append({"kind": "fight", "id": f.id, "ev": f.event_id, "ref": f"Game #{f.number}",
                         "title": title, "res": f.result.value if f.result else "",
                         "nm": f.entry_name_meron, "nw": f.entry_name_wala,
                         "at": _ms(f.declared_at), "by_id": f.declared_by_id})
    if with_bola:
        for d in db.scalars(
                select(BolaDraw).options(selectinload(BolaDraw.location))
                .where(BolaDraw.status.in_((DrawStatus.SETTLED, DrawStatus.VOID)),
                       BolaDraw.declared_at.is_not(None), BolaDraw.declared_at >= since)
                .order_by(BolaDraw.declared_at.desc()).limit(RECENT_LIMIT)).all():
            rows.append({"kind": "bola", "id": d.id, "ref": f"Slot {d.slot}", "title": d.title,
                         "res": d.result_label or ("void" if d.status == DrawStatus.VOID else ""),
                         "at": _ms(d.declared_at), "by_id": d.declared_by_id})
    rows.sort(key=lambda r: r["at"] or 0, reverse=True)
    rows = rows[:RECENT_LIMIT]
    ids = {r["by_id"] for r in rows if r["by_id"]}
    names: dict[int, str] = {}
    if ids:
        names = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_(ids))).all()}
    for r in rows:
        r["by"] = names.get(r.pop("by_id"), "")
    return rows


def _counts(evs: list[dict], draws: list[dict]) -> dict:
    counts = {lane: 0 for lane in LANES}
    for e in evs:
        if e["lane"]:
            counts[e["lane"]] += 1
    for d in draws:
        counts[d["lane"]] += 1
    counts["total"] = sum(counts.values())
    return counts


def board(db: Session, user: User, store: SettingsStore, *, can: dict[str, bool],
          sabong: bool, with_bola: bool, now: dt.datetime | None = None) -> dict:
    """Everything the desk shows. ``can`` carries what this account may do (the browser hides what it
    cannot; the server re-checks every action regardless). ``sabong`` / ``with_bola`` say which games
    this account may see at all."""
    now = now or utcnow()
    evs = _sabong(db, now) if sabong else []
    draws = _draws(db, now) if with_bola else []
    return {
        "can": {k: bool(v) for k, v in can.items()},
        "proof_required": bool(_proof_required(user, store)),
        "early_ok": bool(is_top(user)),                 # Admin / Master may declare a draw before its time
        "max_upload": get_settings().MAX_UPLOAD_BYTES,
        "bola_max": bola.max_number(store) if with_bola else 0,
        "bola_dupes": bool(store.get("bola.allow_duplicate_balls")) if with_bola else False,
        "events": evs, "draws": draws,
        "recent": _recent(db, now, sabong=sabong, with_bola=with_bola),
        "counts": _counts(evs, draws),
    }


def _proof_required(user: User, store: SettingsStore) -> bool:
    from app.services import proofs
    return proofs.required_for(user, store)


def attention(db: Session, *, sabong: bool, with_bola: bool, now: dt.datetime | None = None) -> int:
    """How many games are waiting for a result right now (the number on the Staff Panel tile)."""
    now = now or utcnow()
    evs = _sabong(db, now) if sabong else []
    draws = _draws(db, now) if with_bola else []
    counts = _counts(evs, draws)
    return counts["air"] + counts["due"]
