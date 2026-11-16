from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.rbac import Perm
from sqlalchemy import func, select
from app.core.rbac import downline_ids, scope_ids
from app.db.models import Player, RunStatus, SettlementRun
from app.services import agents as agents_svc
from app.services import reports, settlement, events
from app.web.deps import Ctx, render, require_ctx

router = APIRouter()


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
    return render(ctx, "dashboard.html", active="dashboard", stats=stats, show_money=show_money,
                  pending=pending, recent_events=recent_events, top_agents=top_agents, mine=mine,
                  attention=attention)
