"""Conditional rights: what a right can be besides plain Yes / No.

A right can carry a *rule* (stored as a small dict, evaluated live, nothing hard-coded, no restart to change):

    ask      {"mode": "ask"}                                   may START the action; it is held until the King or a
                                                               Bishop approves it (see services/access.py)
    temp     {"mode": "temp", "from"?: iso, "until": iso,      allowed between the two moments, then the ``after``
              "after": "deny" | "ask"}                         state (default: denied). Expires by itself.
    window   {"mode": "window", "days": [0..6],                allowed on those weekdays (0 = Monday) between the two
              "start": "HH:MM", "end": "HH:MM",                local times (overnight windows work: 22:00-02:00);
              "outside": "deny" | "ask"}                       outside the window the ``outside`` state applies.
    limit    {"mode": "limit", "max": n,                       allowed n times per day / week / ever; after that the
              "per": "day" | "week" | "always",                ``over`` state applies. Counted in perm_usage.
              "over": "deny" | "ask"}

Everything here is a pure function of (rule, now): no database, easy to test. Times are stored in UTC ("...Z");
windows are read in the site's local time zone (the ``TZ`` the app runs in, Asia/Manila by default).
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

MODES = ("ask", "temp", "window", "limit")
FALLBACKS = ("deny", "ask")
PERIODS = ("day", "week", "always")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Quick durations offered next to the "until" box: label -> seconds.
DURATIONS: dict[str, int] = {
    "15m": 15 * 60, "1h": 3600, "4h": 4 * 3600, "8h": 8 * 3600, "24h": 24 * 3600, "3d": 3 * 86400, "7d": 7 * 86400,
}
MAX_LIMIT = 100000


def local_tz() -> dt.tzinfo:
    try:
        return ZoneInfo(os.environ.get("TZ") or "Asia/Manila")
    except Exception:                                    # noqa: BLE001 - an unknown TZ must not break authorization
        return dt.timezone.utc


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_moment(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _hhmm(value) -> str | None:
    try:
        hour, minute = str(value).split(":")[:2]
        hour, minute = int(hour), int(minute)
    except (ValueError, TypeError):
        return None
    return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def clean_rule(raw, *, now: dt.datetime | None = None) -> dict | None:
    """A rule the evaluator can trust, or None when it is malformed or makes no sense (e.g. already over and
    never starting). Unknown keys are dropped, so nothing odd can be stored."""
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    if mode == "ask":
        return {"mode": "ask"}
    if mode == "temp":
        until = parse_moment(raw.get("until"))
        if until is None:
            return None
        start = parse_moment(raw.get("from"))
        if start is not None and start >= until:
            return None
        out = {"mode": "temp", "until": iso(until), "after": raw.get("after") if raw.get("after") in FALLBACKS else "deny"}
        if start is not None:
            out["from"] = iso(start)
        return out
    if mode == "window":
        days = sorted({int(d) for d in (raw.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6})
        start, end = _hhmm(raw.get("start")), _hhmm(raw.get("end"))
        if not days or start is None or end is None or start == end:
            return None
        return {"mode": "window", "days": days, "start": start, "end": end,
                "outside": raw.get("outside") if raw.get("outside") in FALLBACKS else "deny"}
    if mode == "limit":
        try:
            maximum = int(raw.get("max"))
        except (TypeError, ValueError):
            return None
        if not 1 <= maximum <= MAX_LIMIT:
            return None
        return {"mode": "limit", "max": maximum, "per": raw.get("per") if raw.get("per") in PERIODS else "day",
                "over": raw.get("over") if raw.get("over") in FALLBACKS else "deny"}
    return None


def in_window(rule: dict, now: dt.datetime) -> bool:
    local = now.astimezone(local_tz())
    start, end = rule["start"], rule["end"]
    clock = f"{local.hour:02d}:{local.minute:02d}"
    days = set(rule.get("days") or [])
    if start < end:                                       # same-day window
        return local.weekday() in days and start <= clock < end
    # overnight window (22:00-02:00): the evening part belongs to the day it started on
    if clock >= start:
        return local.weekday() in days
    if clock < end:
        return ((local.weekday() - 1) % 7) in days
    return False


def rule_state(rule: dict, now: dt.datetime | None = None) -> str:
    """What this rule says right now: ``allow`` | ``ask`` | ``deny`` | ``limit`` (allowed, but the caller must
    count the use and apply ``over`` when it is spent)."""
    now = now or utcnow()
    mode = rule.get("mode")
    if mode == "ask":
        return "ask"
    if mode == "temp":
        until, start = parse_moment(rule.get("until")), parse_moment(rule.get("from"))
        if until is None or (start is not None and now < start):
            return "deny"
        return "allow" if now < until else rule.get("after", "deny")
    if mode == "window":
        return "allow" if in_window(rule, now) else rule.get("outside", "deny")
    if mode == "limit":
        return "limit"
    return "deny"


def period_key(rule: dict, now: dt.datetime | None = None) -> str:
    """The bucket a use is counted in: the local day, the ISO week, or one bucket forever."""
    local = (now or utcnow()).astimezone(local_tz())
    per = rule.get("per", "day")
    if per == "always":
        return "always"
    if per == "week":
        year, week, _ = local.isocalendar()
        return f"{year}-W{week:02d}"
    return local.date().isoformat()


def is_expired(rule: dict, now: dt.datetime | None = None) -> bool:
    """A temp rule whose time is over (the cleaner can drop it; it has no effect either way)."""
    if rule.get("mode") != "temp":
        return False
    until = parse_moment(rule.get("until"))
    return until is not None and (now or utcnow()) >= until


def _fmt_local(moment: dt.datetime) -> str:
    return moment.astimezone(local_tz()).strftime("%d %b %H:%M")


def describe(rule: dict) -> str:
    """One line a person can read: 'Ask first', 'Until 25 Nov 18:30, then denied', 'Mon-Fri 08:00-17:00', '20 per day'."""
    mode = rule.get("mode")
    if mode == "ask":
        return "Ask first"
    if mode == "temp":
        until, start = parse_moment(rule.get("until")), parse_moment(rule.get("from"))
        then = "then asks first" if rule.get("after") == "ask" else "then denied"
        text = f"Until {_fmt_local(until)}, {then}" if until else "Temporary"
        return (f"From {_fmt_local(start)} " + text.lower()) if start else text
    if mode == "window":
        days = rule.get("days") or []
        if days == [0, 1, 2, 3, 4, 5, 6]:
            label = "Every day"
        elif days == [0, 1, 2, 3, 4]:
            label = "Mon-Fri"
        else:
            label = ", ".join(DAY_NAMES[d] for d in days)
        outside = "asks first outside" if rule.get("outside") == "ask" else "denied outside"
        return f"{label} {rule.get('start')}-{rule.get('end')} ({outside})"
    if mode == "limit":
        per = {"day": "per day", "week": "per week", "always": "in total"}[rule.get("per", "day")]
        over = "then asks first" if rule.get("over") == "ask" else "then denied"
        return f"{rule.get('max')} {per}, {over}"
    return "—"


MODE_LABEL = {"allow": "Yes", "deny": "No", "ask": "Ask first", "temp": "Temporary", "window": "Hours", "limit": "Limit"}
MODE_GLYPH = {"allow": "✓", "deny": "✕", "ask": "✋", "temp": "⏱", "window": "🕒", "limit": "#"}
