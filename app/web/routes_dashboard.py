from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.rbac import Perm
from app.db.models import RunStatus, SettlementRun
from app.services import reports, settlement, events
from app.web.deps import Ctx, render, require_ctx

router = APIRouter()


@router.get("/dashboard")
async def dashboard(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.DASHBOARD)
    stats = reports.dashboard(ctx.db, ctx.user)
    pending = settlement.pending_runs(ctx.db) if ctx.user.is_admin else []
    recent_events = events.list_events(ctx.db, limit=5)
    top_agents = reports.agent_performance(ctx.db) if ctx.user.is_admin else []
    return render(ctx, "dashboard.html", active="dashboard", stats=stats,
                  pending=pending, recent_events=recent_events, top_agents=top_agents)
