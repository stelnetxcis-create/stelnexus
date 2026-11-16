from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.money import to_cents
from app.core.rbac import Perm
from app.db.models import CommissionStatus, RequestDirection
from app.services import cashdesk, commissions, reports
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


@router.get("/commissions")
async def commissions_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.COMMISSIONS_VIEW)
    status = request.query_params.get("status")
    wanted = CommissionStatus(status) if status in ("unclaimed", "claimed", "reversed") else None
    sabong = commissions.list_for(ctx.db, ctx.user, status=wanted)
    for row in sabong:
        row.game_label = f"#{row.ticket.fight.number}"
    bola = commissions.list_bola_for(ctx.db, ctx.user, status=wanted) if ctx.can(Perm.BOLA_VIEW) else []
    for row in bola:
        row.game_label = f"Bola #{row.draw_id}"
    rows = sorted([*sabong, *bola], key=lambda r: r.created_at, reverse=True)
    return render(ctx, "commissions.html", active="commissions", rows=rows,
                  totals=commissions.totals_for(ctx.db, ctx.user),
                  defaults={"volume": int(ctx.store.get("commission.cavo_cut_bps")),
                            "sop": int(ctx.store.get("commission.kubrador_cut_bps"))},
                  basis=ctx.store.get("commission.basis"))


@router.post("/commissions/convert")
async def commissions_convert(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.COMMISSIONS_CLAIM)
    data = await form_data(request)
    amount = to_cents(data["amount"]) if data.get("amount") else None
    commissions.claim_to_wallet(ctx.db, ctx.user, amount_cents=amount,
                                idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect("/commissions", "Commission converted to credit.")


@router.post("/commissions/withdraw")
async def commissions_withdraw(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.COMMISSIONS_CLAIM)
    data = await form_data(request)
    cashdesk.create_request(
        ctx.db, ctx.user, direction=RequestDirection.OUT,
        amount_cents=to_cents(data.get("amount", "0")), settings=ctx.store,
        agent_id=ctx.user.id, method=data.get("method", "gcash"),
        reference=data.get("reference"), idempotency_key=data.get("idempotency_key") or None,
    )
    return flash_redirect("/commissions", "Withdrawal requested. An admin will review it.")


@router.get("/cashflow")
async def cashflow_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CASHFLOW_VIEW)
    return render(ctx, "cashflow.html", active="cashflow", figures=reports.cashflow(ctx.db),
                  series=reports.daily_series(ctx.db, 14),
                  integrity=reports.integrity(ctx.db) if ctx.can(Perm.HIDDEN_VIEW) else None)
