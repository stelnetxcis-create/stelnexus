"""Light polling endpoints for the console (no websockets, no new infrastructure).

The pages are still rendered on the server. These endpoints only answer "what changed?" so tiles can
flip from OPEN to LOCKED to MERON without a refresh. Every endpoint:

  * needs the same permission as the page it feeds,
  * hides money (pool sizes, stake) from accounts that must not see totals,
  * sends an ``ETag`` and answers ``304 Not Modified`` (no body) when the caller's ``If-None-Match`` is
    still current: an idle poll is a few header bytes and the browser skips all DOM work,
  * still accepts ``?rev=<token>`` and answers ``{"same": true}``, so a page that loaded the previous
    release of the script (it is cached by the service worker) keeps working until it reloads.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select

from app.core.rbac import Perm, hides_totals
from app.db.models import Fight, FightStatus, Ticket, TicketStatus
from app.services import events, reports
from app.web.conditional import etag_matches, not_modified
from app.web.deps import Ctx, require_ctx

router = APIRouter()

# "Hot" = one side holds at least this share of the Meron + Wala pool, with enough tickets to mean something.
HOT_SHARE = 0.70
HOT_MIN_TICKETS = 4


def _reply(request: Request, payload: dict) -> Response:
    """Attach a revision token; short-circuit when the caller already has this exact state."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    rev = hashlib.sha1(body.encode()).hexdigest()[:12]
    etag = f'"{rev}"'
    headers = {"Cache-Control": "no-store", "ETag": etag}
    if etag_matches(request, etag):
        return not_modified(etag)
    if request.query_params.get("rev") == rev:
        return JSONResponse({"same": True, "rev": rev}, headers=headers)
    payload["rev"] = rev
    return JSONResponse(payload, headers=headers)


def _pools(db, fight_ids: list[int]) -> dict[int, dict]:
    """Active stakes per fight and side in ONE query (the tiles of a 200-game event must not cost 200)."""
    out = {i: {"meron": 0, "wala": 0, "draw": 0, "cm": 0, "cw": 0} for i in fight_ids}
    if not fight_ids:
        return out
    rows = db.execute(
        select(Ticket.fight_id, Ticket.side, func.coalesce(func.sum(Ticket.stake_cents), 0), func.count(Ticket.id))
        .where(Ticket.fight_id.in_(fight_ids), Ticket.status == TicketStatus.ACTIVE)
        .group_by(Ticket.fight_id, Ticket.side)).all()
    for fight_id, side, amount, count in rows:
        key = str(getattr(side, "value", side))
        out[fight_id][key] = int(amount)
        if key == "meron":
            out[fight_id]["cm"] = int(count)
        elif key == "wala":
            out[fight_id]["cw"] = int(count)
    return out


def _hot(pool: dict) -> str | None:
    main = pool["meron"] + pool["wala"]
    if main <= 0 or pool["cm"] + pool["cw"] < HOT_MIN_TICKETS:
        return None
    if pool["meron"] / main >= HOT_SHARE:
        return "meron"
    if pool["wala"] / main >= HOT_SHARE:
        return "wala"
    return None


@router.get("/_live/event/{event_id}")
async def live_event(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    if not (ctx.can(Perm.EVENTS_VIEW) or ctx.can(Perm.FIGHTS_VIEW)):
        ctx.need(Perm.EVENTS_VIEW)
    event = events.get_event(ctx.db, event_id)
    fights = events.fights_of(ctx.db, event_id)
    hide = hides_totals(ctx.user)
    pools = {} if hide else _pools(ctx.db, [f.id for f in fights])
    rows = []
    for f in fights:
        row = {"id": f.id, "n": f.number, "st": f.status.value, "res": f.result.value if f.result else "",
               "mode": f.bet_mode, "lla": f.llamado_side or "", "nm": f.entry_name_meron, "nw": f.entry_name_wala,
               "cam": bool(f.proof_file),
               "at": f.declared_at.isoformat() if f.declared_at else ""}
        pool = pools.get(f.id)
        if pool is not None and f.status in (FightStatus.OPEN, FightStatus.LOCKED):
            row.update({"pm": pool["meron"], "pw": pool["wala"], "cm": pool["cm"], "cw": pool["cw"],
                        "hot": _hot(pool) or ""})
        rows.append(row)
    return _reply(request, {"event": {"id": event.id, "st": event.status.value, "live": event.live_started_at is not None}, "hide": hide, "fights": rows})


@router.get("/_live/lobby")
async def live_lobby(request: Request, ctx: Ctx = Depends(require_ctx)):
    if not (ctx.can(Perm.EVENTS_VIEW) or ctx.can(Perm.FIGHTS_VIEW)):
        ctx.need(Perm.EVENTS_VIEW)
    rows = events.list_events(ctx.db, limit=100)
    stats = events.lobby_stats(ctx.db, [e.id for e in rows])
    return _reply(request, {"events": [
        {"id": e.id, "st": e.status.value, "live": stats[e.id]["live"], "open": stats[e.id]["open"],
         "locked": stats[e.id]["locked"], "done": stats[e.id]["settled"] + stats[e.id]["void"],
         "total": stats[e.id]["total"], "pct": stats[e.id]["pct"]} for e in rows]})


@router.get("/_live/dashboard")
async def live_dashboard(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.DASHBOARD)
    db = ctx.db
    today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    open_n = int(db.scalar(select(func.count(Fight.id)).where(Fight.status == FightStatus.OPEN)) or 0)
    locked_n = int(db.scalar(select(func.count(Fight.id)).where(Fight.status == FightStatus.LOCKED)) or 0)
    out = {"open": open_n + locked_n, "locked": locked_n,
           "tickets": int(db.scalar(select(func.count(Ticket.id)).where(Ticket.placed_at >= today)) or 0),
           "live": [{"id": r["id"], "n": r["number"], "st": r["status"].value, "ev": r["event_id"]}
                    for r in reports.live_fights(db)]}
    if ctx.can(Perm.CASHFLOW_VIEW) and not hides_totals(ctx.user):
        out["stake"] = int(db.scalar(select(func.coalesce(func.sum(Ticket.stake_cents), 0)).where(
            Ticket.placed_at >= today, Ticket.status != TicketStatus.CANCELED)) or 0)
    return _reply(request, out)
