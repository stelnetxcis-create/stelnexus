"""Read the old panel's MySQL dump (``online_bukis.sql``) and bring the parts worth keeping across.

Scope (plan section 6), deliberately narrow:

* ``tbl_area``  -> a Location per area + a ``BolaSchedule`` row per draw time (``ball_1/2/3``).
  A time of 00:00:00 meant "no draw" in the old panel and is skipped.
* ``tbl_settings.limit_amount_per_combi`` -> ``bola.limit_per_line_cents``.
* ``tbl_event`` rows that actually ran (both balls present) -> read-only history: a SETTLED,
  ``legacy=True`` BolaDraw with the two balls and **no tickets and no ledger postings**.

Never imported: passwords, credits, users, tickets, ``documents/``. Tickets were never ledger-backed;
importing them would put unbacked money rows next to real ones. Recreate people through the normal
invite flow.

The parser reads the dump as text only. Nothing is executed. The importer will not guess: if the
columns it needs are not in the dump it says which ones it found and skips that part.

Everything is idempotent: re-running ``--apply`` creates nothing twice.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.settings_registry import SettingsStore
from app.db.models import BolaDraw, BolaSchedule, DrawStatus, Location, User
from app.services import bola, locations

# ----------------------------------------------------------------------------- SQL dump parsing

_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\(", re.I)
_INSERT_RE = re.compile(r"INSERT\s+(?:IGNORE\s+)?INTO\s+`?(\w+)`?\s*(\([^)]*\))?\s*VALUES", re.I)
_KEYWORDS = ("PRIMARY", "KEY", "UNIQUE", "CONSTRAINT", "FULLTEXT", "INDEX")


@dataclass
class Table:
    name: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)


def _create_columns(text: str, start: int) -> list[str]:
    """Column names from the body of a CREATE TABLE that opens at ``start`` (just past the paren).
    Works for one-column-per-line dumps and for single-line ones."""
    depth, i, in_str, parts, cur = 1, start, None, [], []
    while i < len(text) and depth:
        ch = text[i]
        if in_str:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            if depth:
                cur.append(ch)
        elif ch == "," and depth == 1:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    cols = []
    for part in parts:
        m = re.match(r"\s*`(\w+)`\s+\S+", part)
        if m and not part.strip().upper().startswith(_KEYWORDS):
            cols.append(m.group(1))
    return cols


def _scan_tuples(text: str, pos: int) -> tuple[list[list], int]:
    """Parse ``(v, v, ...), (v, ...)`` from ``pos`` up to the terminating ``;``."""
    rows: list[list] = []
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch == ";":
            return rows, pos + 1
        if ch != "(":
            pos += 1
            continue
        pos += 1
        row: list = []
        token = []
        while pos < n:
            ch = text[pos]
            if ch == "'":
                pos += 1
                buf = []
                while pos < n:
                    c = text[pos]
                    if c == "\\" and pos + 1 < n:
                        nxt = text[pos + 1]
                        buf.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(nxt, nxt))
                        pos += 2
                    elif c == "'" and pos + 1 < n and text[pos + 1] == "'":
                        buf.append("'")
                        pos += 2
                    elif c == "'":
                        pos += 1
                        break
                    else:
                        buf.append(c)
                        pos += 1
                row.append("".join(buf))
                token = []
            elif ch in ",)":
                raw = "".join(token).strip()
                if raw:
                    row.append(None if raw.upper() == "NULL" else raw)
                token = []
                pos += 1
                if ch == ")":
                    break
            else:
                token.append(ch)
                pos += 1
        rows.append(row)
    return rows, pos


def parse_dump(text: str) -> dict[str, Table]:
    """Every table in the dump with its column names and rows (as dicts)."""
    tables: dict[str, Table] = {}
    for m in _CREATE_RE.finditer(text):
        tables.setdefault(m.group(1), Table(m.group(1))).columns = _create_columns(text, m.end())
    pos = 0
    while True:
        m = _INSERT_RE.search(text, pos)
        if m is None:
            break
        name = m.group(1)
        table = tables.setdefault(name, Table(name))
        cols = [c.strip().strip("`") for c in m.group(2)[1:-1].split(",")] if m.group(2) else table.columns
        rows, pos = _scan_tuples(text, m.end())
        for values in rows:
            if cols and len(values) == len(cols):
                table.rows.append(dict(zip(cols, values)))
    return tables


# ----------------------------------------------------------------------------- the plan

_AREA_NAME_COLS = ("area", "area_name", "name", "title", "location")
_EVENT_DATE_COLS = ("event_date", "date_created", "date", "created_at", "date_added", "datetime", "ts")


@dataclass
class AreaPlan:
    legacy_id: str
    name: str
    times: dict[int, dt.time]            # slot -> Manila wall time


@dataclass
class DrawPlan:
    legacy_id: str
    area_name: str
    day: dt.date
    slot: int
    first: int
    second: int


@dataclass
class ImportPlan:
    areas: list[AreaPlan] = field(default_factory=list)
    draws: list[DrawPlan] = field(default_factory=list)
    limit_per_line_cents: int | None = None
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [f"areas:            {len(self.areas)}",
                 f"draw times:       {sum(len(a.times) for a in self.areas)}",
                 f"past draws:       {len(self.draws)}   (read-only history, no tickets, no money)",
                 f"line limit:       {self.limit_per_line_cents / 100:,.2f}" if self.limit_per_line_cents
                 else "line limit:       (not found, setting left alone)"]
        for a in self.areas:
            times = ", ".join(f"slot {s} {t.strftime('%H:%M')}" for s, t in sorted(a.times.items())) or "no draw times"
            lines.append(f"  - {a.name}: {times}")
        for key, n in sorted(self.skipped.items()):
            lines.append(f"skipped {n}: {key}")
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        return "\n".join(lines)


def _time(raw) -> dt.time | None:
    try:
        parts = [int(p) for p in str(raw).strip().split(":")[:3]]
        wall = dt.time(*parts)
    except (TypeError, ValueError):
        return None
    return None if wall == dt.time(0, 0) else wall     # 00:00 = "no draw"


def _ball(raw) -> int | None:
    try:
        n = int(Decimal(str(raw)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return n if 1 <= n <= 90 else None


def _day(raw) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def build_plan(tables: dict[str, Table]) -> ImportPlan:
    plan = ImportPlan()
    areas = tables.get("tbl_area")
    by_legacy_id: dict[str, str] = {}
    if areas is None or not areas.rows:
        plan.warnings.append("No tbl_area rows in the dump: no areas or draw times will be created.")
    else:
        name_col = next((c for c in _AREA_NAME_COLS if c in areas.rows[0]), None)
        if name_col is None:
            plan.warnings.append(f"tbl_area has no recognisable name column. Found: {', '.join(areas.rows[0])}.")
        else:
            seen: set[str] = set()
            for row in areas.rows:
                name = " ".join(str(row.get(name_col) or "").split())
                if not name or name.lower() in seen:
                    plan.skipped["duplicate or empty area names"] = plan.skipped.get("duplicate or empty area names", 0) + 1
                    continue
                if not locations.NAME_RE.match(name):
                    plan.skipped["areas whose name the app does not allow"] = plan.skipped.get(
                        "areas whose name the app does not allow", 0) + 1
                    plan.warnings.append(f"Area {name!r} was skipped: names may use letters, numbers, "
                                         "spaces and . , ' & / # ( ) - only. Rename it in the dump or create it by hand.")
                    continue
                seen.add(name.lower())
                times = {i: t for i in (1, 2, 3) if (t := _time(row.get(f"ball_{i}"))) is not None}
                legacy_id = str(row.get("id", ""))
                by_legacy_id[legacy_id] = name
                plan.areas.append(AreaPlan(legacy_id, name, times))

    settings = tables.get("tbl_settings")
    if settings is not None:
        for row in settings.rows:
            if row.get("limit_amount_per_combi") not in (None, ""):
                try:
                    plan.limit_per_line_cents = int(Decimal(str(row["limit_amount_per_combi"])) * 100)
                except InvalidOperation:
                    plan.warnings.append("limit_amount_per_combi is not a number; left alone.")
                break

    events = tables.get("tbl_event")
    if events is None or not events.rows:
        return plan
    sample = events.rows[0]
    date_col = next((c for c in _EVENT_DATE_COLS if c in sample), None)
    needed = {"id", "id_area", "ball_1", "ball_2"} - set(sample)
    if needed or date_col is None:
        why = f"missing columns {', '.join(sorted(needed))}" if needed else "no date column"
        plan.warnings.append(f"tbl_event: {why}; past draws are not imported. Found: {', '.join(sample)}.")
        return plan
    used: set[tuple[str, dt.date, int]] = set()
    for row in events.rows:
        first, second = _ball(row.get("ball_1")), _ball(row.get("ball_2"))
        day = _day(row.get(date_col))
        area = by_legacy_id.get(str(row.get("id_area", "")))
        if first is None or second is None or first == second:
            plan.skipped["events that never ran (no valid balls)"] = plan.skipped.get(
                "events that never ran (no valid balls)", 0) + 1
            continue
        if day is None or area is None:
            plan.skipped["events with no usable date or area"] = plan.skipped.get(
                "events with no usable date or area", 0) + 1
            continue
        wanted = _ball(row.get("ball_sched")) if "ball_sched" in row else None
        free = [s for s in range(1, 7) if (area, day, s) not in used]
        slot = wanted if wanted in free else (free[0] if free else 0)
        if not slot:
            plan.skipped["events beyond 6 per area per day"] = plan.skipped.get(
                "events beyond 6 per area per day", 0) + 1
            continue
        used.add((area, day, slot))
        plan.draws.append(DrawPlan(str(row["id"]), area, day, slot, first, second))
    return plan


def read_plan(path: str) -> ImportPlan:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return build_plan(parse_dump(fh.read()))


# ----------------------------------------------------------------------------- applying it

def apply_plan(db: Session, actor: User, plan: ImportPlan, store: SettingsStore) -> dict[str, int]:
    made = {"areas": 0, "schedules": 0, "draws": 0}
    loc_by_name: dict[str, Location] = {}
    for area in plan.areas:
        loc = db.scalar(select(Location).where(Location.name.ilike(area.name)))
        if loc is None:
            loc = locations.create_location(db, actor, name=area.name)
            made["areas"] += 1
        loc_by_name[area.name] = loc
        for slot, wall in area.times.items():
            exists = db.scalar(select(BolaSchedule).where(BolaSchedule.location_id == loc.id,
                                                          BolaSchedule.slot == slot))
            if exists is None:
                bola.upsert_schedule(db, actor, location_id=loc.id, slot=slot, draw_time=wall)
                made["schedules"] += 1
    for item in plan.draws:
        loc = loc_by_name.get(item.area_name)
        if loc is None:
            continue
        if db.scalar(select(BolaDraw.id).where(BolaDraw.location_id == loc.id,
                                               BolaDraw.draw_date == item.day,
                                               BolaDraw.slot == item.slot)):
            continue
        sched = db.scalar(select(BolaSchedule).where(BolaSchedule.location_id == loc.id,
                                                     BolaSchedule.slot == item.slot))
        wall = sched.draw_time if sched else dt.time(12, 0)
        when = bola.local_instant(item.day, wall)
        db.add(BolaDraw(location_id=loc.id, draw_date=item.day, slot=item.slot, scheduled_at=when,
                        cutoff_at=bola.cutoff_for(when, store), status=DrawStatus.SETTLED,
                        ball_first=item.first, ball_second=item.second, declared_at=when,
                        note=f"legacy #{item.legacy_id}"[:120], legacy=True))
        made["draws"] += 1
    db.flush()
    if plan.limit_per_line_cents:
        store.set("bola.limit_per_line_cents", str(plan.limit_per_line_cents), actor.id)
    audit.record(db, actor, "bola.legacy.import", entity_type="bola_import", entity_id=0,
                 after={**made, "limit_per_line_cents": plan.limit_per_line_cents})
    return made
