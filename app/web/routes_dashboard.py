from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.rbac import Perm, bola_dev_only
from sqlalchemy import func, select
from app.core.rbac import downline_ids, scope_ids
from app.db.models import Player, RunStatus, SettlementRun
from app.services import bola as _bola
from app.services import agents as agents_svc
from app.services import reports, settlement, events
from app.web.deps import Ctx, render, require_ctx

router = APIRouter()


def _sabong_tile(ctx: Ctx, open_games: int) -> dict | None:
    """Meron / Wala: the finished game. Sellers go to the events lobby; result-only roles
    (declarators, pawns) go to the fight view. No access to either -> no tile."""
    if ctx.can(Perm.EVENTS_VIEW):
        href = "/events"
    elif ctx.can(Perm.FIGHTS_VIEW):
        href = "/change_name"
    else:
        return None
    hint = f"{open_games} game{'' if open_games == 1 else 's'} open"
    return {"href": href, "hint": hint}


def _number_game_tile(ctx: Ctx, bola_card: dict | None) -> dict | None:
    """Number game tile.

    While the game is dev-only it ships dark: only the dev (the account that holds a Bola
    permission under the gate, i.e. the Admin) sees a 'Coming soon' tile; every other role gets
    no tile at all, so nothing about the game leaks. After launch the tile is a link when the
    game is on and this account can see it, and 'Coming soon' otherwise."""
    if bola_dev_only():
        return {"state": "soon", "href": None, "hint": "Coming soon"} if ctx.can(Perm.BOLA_VIEW) else None
    if bool(ctx.store.get("bola.enabled")) and ctx.can(Perm.BOLA_VIEW):
        n = int(bola_card["open"]) if bola_card else 0
        return {"state": "live", "href": "/bola", "hint": f"{n} draw{'' if n == 1 else 's'} open"}
    return {"state": "soon", "href": None, "hint": "Coming soon"}


@router.get("/dashboard")
async def dashboard(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.DASHBOARD)
    stats = reports.dashboard(ctx.db, ctx.user)
    pending = settlement.pending_runs(ctx.db) if ctx.user.is_top else []
    recent_events = events.list_events(ctx.db, limit=5)
    top_agents = reports.agent_performance(ctx.db) if ctx.user.is_top else []
    # House money (stakes, balances, revenue, load / cash out) is dev-only: it needs cashflow.view.
    # Master keeps the operational side of the dashboard without any house totals.
    show_money = ctx.can(Perm.CASHFLOW_VIEW)
    mine = None
    if not ctx.user.is_top:       # cavo / kubrador: only their own numbers, never house-wide money
        ids = scope_ids(ctx.db, ctx.user)
        mine = {"credit": agents_svc.wallet_balance(ctx.db, ctx.user),
                "downline": len(downline_ids(ctx.db, ctx.user)),
                "players": int(ctx.db.scalar(select(func.count(Player.id)).where(Player.agent_id.in_(ids))) or 0)}

    # "Needs attention": the few things a person with this role can act on right now.
    attention = []
    if ctx.user.is_top:
        if ctx.can(Perm.REQUESTS_REVIEW):
            attention.append({"label": "Cash requests to review", "count": stats["pending_requests"],
                              "href": "/wallet?t=approvals", "tone": "amber"})
        if ctx.can(Perm.FIGHTS_APPROVE):
            attention.append({"label": "Corrections awaiting approval", "count": len(pending),
                              "href": "/dashboard#corrections", "tone": "amber"})
        attention.append({"label": "Locked games waiting for a result", "count": stats["locked_fights"],
                          "href": "/change_name", "tone": "red"})
    bola_card = reports.bola_dashboard(ctx.db, ctx.user, ctx.store) if ctx.can(Perm.BOLA_VIEW) else None
    if bola_card and ctx.user.is_top and ctx.can(Perm.BOLA_APPROVE) and bola_card["pending_corrections"]:
        attention.append({"label": "Bola corrections awaiting approval",
                          "count": bola_card["pending_corrections"], "href": "/bola", "tone": "amber"})
    return render(ctx, "dashboard.html", active="dashboard", stats=stats, show_money=show_money,
                  pending=pending, recent_events=recent_events, top_agents=top_agents, mine=mine,
                  attention=attention, bola_card=bola_card, bola_local=_bola.local,
                  sabong_tile=_sabong_tile(ctx, stats["open_fights"]),
                  number_tile=_number_game_tile(ctx, bola_card))
