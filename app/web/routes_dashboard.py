from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core import ttlcache
from app.core.config import get_settings
from app.core.rbac import Perm, bola_dev_only
from sqlalchemy import func, select
from app.core.rbac import downline_ids, scope_ids
from app.db.models import Player, RunStatus, SettlementRun
from app.services import bola as _bola
from app.services import agents as agents_svc
from app.services import reports, settlement, events, snapshots
from app.web.deps import Ctx, render, require_ctx
from app.web.routes_games import game_cards

router = APIRouter()


@router.get("/dashboard")
async def dashboard(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.DASHBOARD)
    ttl = get_settings().DASHBOARD_CACHE_SECONDS
    # House-wide KPIs are identical for every viewer (the figure set is decided by show_money below),
    # so one cached copy serves everyone for a few seconds instead of ~30 queries per page view.
    # The all-time load / cash-out and Bola totals come from the precomputed snapshot (refresh-reports
    # timer); everything else in the KPIs is cheap and stays live behind the short TTL cache.
    stats = ttlcache.cached(("dashboard", "stats"), ttl, lambda: reports.dashboard(
        ctx.db, ctx.user, flows=snapshots.flows(ctx.db).payload))
    pending = settlement.pending_runs(ctx.db) if ctx.user.is_top else []
    recent_events = events.list_events(ctx.db, limit=5)
    top_agents = (ttlcache.cached(("dashboard", "top_agents"), ttl, lambda: snapshots.top_agents(ctx.db).payload)
                  if ctx.user.is_top else [])
    # House money (stakes, balances, revenue, load / cash out) is dev-only: it needs cashflow.view.
    # Master keeps the operational side of the dashboard without any house totals.
    show_money = ctx.can(Perm.CASHFLOW_VIEW)
    mine = None
    if not ctx.user.is_top:       # agent / sub agent: only their own numbers, never house-wide money
        def _network() -> dict:
            ids = scope_ids(ctx.db, ctx.user)
            return {"downline": len(downline_ids(ctx.db, ctx.user)),
                    "players": int(ctx.db.scalar(select(func.count(Player.id)).where(Player.agent_id.in_(ids))) or 0)}
        # network size is cached per user; the credit balance stays live (one cheap indexed read)
        mine = {"credit": agents_svc.wallet_balance(ctx.db, ctx.user),
                **ttlcache.cached(("dashboard", "network", ctx.user.id), ttl, _network)}

    # "Needs attention": the few things a person with this role can act on right now.
    attention = []
    if ctx.user.is_top:
        if ctx.can(Perm.REQUESTS_REVIEW):
            attention.append({"label": "Cash requests to review", "count": stats["pending_requests"],
                              "href": "/wallet?t=approvals", "tone": "amber"})
        if ctx.can(Perm.FIGHTS_APPROVE):
            attention.append({"label": "Corrections awaiting approval", "count": len(pending),
                              "href": "/dashboard#corrections", "tone": "amber"})
        attention.append({"label": "Locked fights waiting for a result", "count": stats["locked_fights"],
                          "href": "/games/sabong/live", "tone": "red"})
    bola_card = reports.bola_dashboard(ctx.db, ctx.user, ctx.store) if ctx.can(Perm.BOLA_VIEW) else None
    if bola_card and ctx.user.is_top and ctx.can(Perm.BOLA_APPROVE) and bola_card["pending_corrections"]:
        attention.append({"label": "Bola corrections awaiting approval",
                          "count": bola_card["pending_corrections"], "href": "/bola", "tone": "amber"})
    return render(ctx, "dashboard.html", active="dashboard", stats=stats, show_money=show_money,
                  pending=pending, recent_events=recent_events, top_agents=top_agents, mine=mine,
                  attention=attention, bola_card=bola_card, bola_local=_bola.local,
                  game_cards=game_cards(ctx))
