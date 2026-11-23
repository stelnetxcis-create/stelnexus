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
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import ttlcache
from app.core.config import get_settings
from app.core.rbac import sees_house, Perm, hides_totals
from app.core.money import bps_of
from app.db.models import (AppSetting, Event, Fight, FightStatus, PayoutModel, RunStatus, SettlementRun, StreamAssignment,
                           StreamSource, Ticket, TicketStatus)
from app.services import betting, events, reports
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


def _pack(payload: dict) -> tuple[str, bytes]:
    """(revision token, final JSON body) for a payload: built once, then shared by every poller."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    rev = hashlib.sha1(body.encode()).hexdigest()[:12]
    return rev, JSONResponse({**payload, "rev": rev}).body


def _reply_packed(request: Request, rev: str, body: bytes) -> Response:
    """Same answers as ``_reply`` (304 / ``{"same": true}`` / full body), from a payload that is already built."""
    etag = f'"{rev}"'
    headers = {"Cache-Control": "no-store", "ETag": etag}
    if etag_matches(request, etag):
        return not_modified(etag)
    if request.query_params.get("rev") == rev:
        return JSONResponse({"same": True, "rev": rev}, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


# The shared live payload ("live" cache prefix) is cleared the moment a transaction that wrote a ticket, game,
# event or setting COMMITS, so a bet, a lock, a declared result or a changed house cut shows on the very next
# poll. LIVE_CACHE_SECONDS only covers writes made by another process.
_LIVE_MODELS = (Ticket, Fight, Event, AppSetting, StreamSource, StreamAssignment)


@sa_event.listens_for(Session, "before_flush")
def _live_cache_dirty(session: Session, _ctx, _instances) -> None:
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, _LIVE_MODELS):
            ttlcache.invalidate_after_commit(session, "live")
            return


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


_BETTABLE = (FightStatus.OPEN, FightStatus.LOCKED)


def _rows(db, store, event, fights: list[Fight], hide: bool, cur_id: int | None, cut_bps: int,
          house: bool = True) -> list[dict]:
    """One poll row per game. Pools are only read for games that can still take or hold bets: a settled game's
    pool is never shown, so a 200-game event no longer sums every old ticket on every poll."""
    live_ids = [f.id for f in fights if f.status in _BETTABLE]
    pools = {} if hide else _pools(db, live_ids)
    fixed = event.payout_model == PayoutModel.FIXED
    rows = []
    for f in fights:
        row = {"id": f.id, "n": f.number, "st": f.status.value, "res": f.result.value if f.result else "",
               "mode": f.bet_mode, "lla": f.llamado_side or "", "nm": f.entry_name_meron, "nw": f.entry_name_wala,
               "cam": bool(f.proof_file), "sd": f.started_at is not None,
               "ph": events.stream_phase(f, cur_id),
               "at": f.declared_at.isoformat() if f.declared_at else ""}
        pool = pools.get(f.id)
        if pool is not None and f.status in _BETTABLE:
            main = pool["meron"] + pool["wala"]
            row.update({"pm": pool["meron"], "pw": pool["wala"], "cm": pool["cm"], "cw": pool["cw"],
                        "hot": _hot(pool) or ""})
            # Live payout, same rules as settlement / the fight panel: pari-mutuel = (pool - house cut) shared by
            # the winning side; fixed-odds = the fight's odds. bps are 1/10000 (x1.85 = 18500).
            if fixed:
                dflt = int(store.get("payout.default_fixed_odds_bps"))
                mm, mw = f.fixed_odds_meron_bps or dflt, f.fixed_odds_wala_bps or dflt
                row.update({"mm": mm, "mw": mw, "tt": main, "np": None, "cut": 0, "fx": True,
                            "om": pool["meron"] * mm // 10000, "ow": pool["wala"] * mw // 10000})
            else:
                snap = {"meron": pool["meron"], "wala": pool["wala"], "main": main}
                plat_bps = events.platform_cut_for(store)
                mult = betting.side_multipliers(snap, betting.combined_bps(cut_bps, plat_bps), store, f.llamado_side)
                fig = betting.pool_figures(pool["meron"], pool["wala"], cut_bps, plat_bps)
                # Everyone sees the pools AFTER the platform cut, the house commission and the payout pool (that is
                # the whole calculation on screen). The gross stakes and the platform cut amount ride only on the
                # Admin's / a granted Pawn's poll.
                row.update({"pm": fig["meron"], "pw": fig["wala"], "mm": mult["meron"]["bps"], "mw": mult["wala"]["bps"],
                            "tt": fig["total"], "np": fig["net"], "cut": fig["house"], "fx": False,
                            "gt": fig["gross"] if house else None, "pc": fig["platform"] if house else None,
                            "lm": mult["meron"]["below_min"], "lw": mult["wala"]["below_min"],
                            "xm": mult["meron"]["capped"], "xw": mult["wala"]["capped"],
                            "am": mult["meron"]["max"], "aw": mult["wala"]["max"]})
        rows.append(row)
    return rows


def _live_event_pack(ctx: Ctx, event_id: int, hide: bool, house: bool) -> tuple[str, bytes]:
    event = events.get_event(ctx.db, event_id)
    fights = events.fights_of(ctx.db, event_id)
    cur = events.current_fight(ctx.db, event_id)
    cur_id = cur.id if cur else None
    cut_bps = events.house_cut_for(ctx.db, event, ctx.store)
    rows = _rows(ctx.db, ctx.store, event, fights, hide, cur_id, cut_bps, house)
    return _pack({"event": {"id": event.id, "st": event.status.value, "live": event.live_started_at is not None,
                            "cur": cur_id, "cutbps": cut_bps,
                            "pcbps": events.platform_cut_for(ctx.store)}, "hide": hide, "fights": rows})


@router.get("/_live/event/{event_id}")
async def live_event(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    if not (ctx.can(Perm.EVENTS_VIEW) or ctx.can(Perm.FIGHTS_VIEW)):
        ctx.need(Perm.EVENTS_VIEW)
    hide = hides_totals(ctx.user)
    house = sees_house(ctx.user) and not hide
    # Everything in the payload depends only on the event and on whether this account may see money, so
    # all viewers with the same answer share one build (see the invalidation note above).
    rev, body = ttlcache.cached(("live", event_id, hide, house), get_settings().LIVE_CACHE_SECONDS,
                                lambda: _live_event_pack(ctx, event_id, hide, house))
    return _reply_packed(request, rev, body)


def live_row(db, store, user, fight: Fight) -> dict:
    """The poll row of ONE game, computed in the caller's own transaction. The bet / undo endpoints send it
    back with their reply, so the payout on screen updates from the same answer that confirms the bet: no
    waiting for the next poll, and it is correct even before this transaction has committed."""
    db.flush()
    event = events.get_event(db, fight.event_id)
    hide = hides_totals(user)
    cur = events.current_fight(db, fight.event_id)
    cut_bps = events.house_cut_for(db, event, store)
    return _rows(db, store, event, [fight], hide, cur.id if cur else None, cut_bps,
                 sees_house(user) and not hide)[0]


@router.get("/_live/fight/{fight_id}/final")
async def live_fight_final(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """How a finished game ended, for the animated result scene over the video: result, final payout multiple,
    total bets, house cut, winners and what was paid out. Figures come from the applied settlement run (the real
    numbers), never recomputed. Accounts that must not see totals get the result only."""
    ctx.need(Perm.FIGHTS_VIEW)
    fight = events.get_fight(ctx.db, fight_id)
    out = {"id": fight.id, "n": fight.number, "res": fight.result.value if fight.result else "",
           "nm": fight.entry_name_meron, "nw": fight.entry_name_wala, "lla": fight.llamado_side or "",
           "at": fight.declared_at.isoformat() if fight.declared_at else "", "hide": hides_totals(ctx.user)}
    if not out["hide"]:
        run = ctx.db.scalar(select(SettlementRun).where(
            SettlementRun.fight_id == fight.id, SettlementRun.status == RunStatus.APPLIED
        ).order_by(SettlementRun.version.desc()))
        t = (run.totals or {}) if run else {}
        win_stake = int(t.get(out["res"], 0)) if out["res"] in ("meron", "wala") else 0
        paid = int(t.get("payout_total", 0))
        gross = int(t.get("meron", 0)) + int(t.get("wala", 0))
        out.update({"tt": gross - int(t.get("platform_cut", 0)), "np": int(t.get("net_pool", 0)),
                    "cut": int(t.get("house_cut", 0)), "paid": paid, "refund": int(t.get("refund_total", 0)),
                    "winners": int(t.get("winners", 0)), "tickets": int(t.get("tickets", 0)),
                    "auto_cancel": bool(t.get("auto_cancel", False)),
                    "mult": (paid * 10000 // win_stake) if win_stake else 0})
        if sees_house(ctx.user):
            out.update({"gt": gross, "pc": int(t.get("platform_cut", 0))})
    return _reply(request, out)


@router.get("/_live/wallet")
async def live_wallet(request: Request, ctx: Ctx = Depends(require_ctx)):
    """My credit right now. The page header polls this, so a credit or debit shows up without a refresh."""
    from app.core.money import fmt
    from app.web.deps import wallet_snapshot
    balance, pending = wallet_snapshot(ctx)
    return _reply(request, {"balance_cents": balance, "balance": None if balance is None else fmt(balance),
                            "pending": pending})


@router.get("/_live/lobby")
async def live_lobby(request: Request, ctx: Ctx = Depends(require_ctx)):
    if not (ctx.can(Perm.EVENTS_VIEW) or ctx.can(Perm.FIGHTS_VIEW)):
        ctx.need(Perm.EVENTS_VIEW)

    def _build() -> tuple[str, bytes]:
        rows = events.list_events(ctx.db, limit=100)
        stats = events.lobby_stats(ctx.db, [e.id for e in rows])
        return _pack({"events": [
            {"id": e.id, "st": e.status.value, "live": stats[e.id]["live"], "open": stats[e.id]["open"],
             "locked": stats[e.id]["locked"], "done": stats[e.id]["settled"] + stats[e.id]["void"],
             "total": stats[e.id]["total"], "pct": stats[e.id]["pct"]} for e in rows]})

    # Same for every viewer (access is checked above), so everyone shares one build. Cleared on commit of a
    # game / event / ticket write (see _live_cache_dirty); LIVE_CACHE_SECONDS only covers other processes.
    rev, body = ttlcache.cached(("live", "lobby"), get_settings().LIVE_CACHE_SECONDS, _build)
    return _reply_packed(request, rev, body)


@router.get("/_live/dashboard")
async def live_dashboard(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.DASHBOARD)
    db = ctx.db
    show_stake = ctx.can(Perm.CASHFLOW_VIEW) and not hides_totals(ctx.user)

    def _build() -> tuple[str, bytes]:
        today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        open_n = int(db.scalar(select(func.count(Fight.id)).where(Fight.status == FightStatus.OPEN)) or 0)
        locked_n = int(db.scalar(select(func.count(Fight.id)).where(Fight.status == FightStatus.LOCKED)) or 0)
        out = {"open": open_n + locked_n, "locked": locked_n,
               "tickets": int(db.scalar(select(func.count(Ticket.id)).where(Ticket.placed_at >= today)) or 0),
               "live": [{"id": r["id"], "n": r["number"], "st": r["status"].value, "ev": r["event_id"]}
                        for r in reports.live_fights(db)]}
        if show_stake:
            out["stake"] = int(db.scalar(select(func.coalesce(func.sum(Ticket.stake_cents), 0)).where(
                Ticket.placed_at >= today, Ticket.status != TicketStatus.CANCELED)) or 0)
        return _pack(out)

    # Two shapes only (with / without the day's stake): an account that must not see money can never be handed
    # the build that holds it. One build per LIVE_CACHE_SECONDS, or sooner when a game / ticket is committed.
    rev, body = ttlcache.cached(("live", "dashboard", show_stake), get_settings().LIVE_CACHE_SECONDS, _build)
    return _reply_packed(request, rev, body)
