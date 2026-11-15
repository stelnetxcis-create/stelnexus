from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm
from app.db.models import CommissionStatus, RequestDirection
from app.services import booklets, cashdesk, commissions, reports
from app.web.deps import Ctx, audit_action, form_data, flash_redirect, render, require_ctx

router = APIRouter()


@router.get("/commissions")
async def commissions_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.COMMISSIONS_VIEW)
    status = request.query_params.get("status")
    rows = commissions.list_for(ctx.db, ctx.user,
                                status=CommissionStatus(status) if status in ("unclaimed", "claimed", "reversed") else None)
    return render(ctx, "commissions.html", active="commissions", rows=rows,
                  totals=commissions.totals_for(ctx.db, ctx.user),
                  rates={tier: int(ctx.store.get(f"commission.rate_{tier}_bps"))
                         for tier in ("master", "gold", "sub", "admin")},
                  basis=ctx.store.get("commission.basis"))


@router.post("/commissions/convert")
async def commissions_convert(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.COMMISSIONS_CLAIM)
    data = await form_data(request)
    amount = to_cents(data["amount"]) if data.get("amount") else None
    commissions.claim_to_wallet(ctx.db, ctx.user, amount_cents=amount,
                                idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect("/commissions", "Commission converted to load credit.")


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


@router.post("/cashin_out/withdrawals")
async def withdrawals_switch(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Emergency stop: block every cash-out / withdrawal (and approvals of them)."""
    ctx.need(Perm.SETTINGS_MANAGE)
    data = await form_data(request)
    enabled = data.get("enabled", "1") == "1"
    old, new = ctx.store.set("wallet.withdrawals_enabled", "true" if enabled else "false", ctx.user.id)
    audit_action(ctx, "settings.update", entity_type="setting", entity_id="wallet.withdrawals_enabled",
                 before={"value": old}, after={"value": new}, reason="emergency switch")
    return flash_redirect("/cashin_out", "Withdrawals are ON again." if enabled
                          else "Withdrawals are OFF. No cash-out can be requested or approved.",
                          "success" if enabled else "warning")


@router.get("/cashflow")
async def cashflow_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CASHFLOW_VIEW)
    return render(ctx, "cashflow.html", active="cashflow", figures=reports.cashflow(ctx.db),
                  stations=reports.stations(ctx.db), series=reports.daily_series(ctx.db, 14),
                  integrity=reports.integrity(ctx.db) if ctx.user.is_admin else None)


@router.post("/cashflow/remit")
async def cashflow_remit(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STATIONS_MANAGE)
    data = await form_data(request)
    try:
        station_id = int(data.get("station_id", "0"))
    except ValueError as exc:
        raise ValidationError("Choose a station.") from exc
    cashdesk.remit(ctx.db, ctx.user, station_id=station_id,
                   amount_cents=to_cents(data.get("amount", "0")), note=data.get("note"),
                   idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect("/cashflow", "Credit remitted.")


@router.get("/booklets")
async def booklets_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOOKLETS_VIEW)
    return render(ctx, "booklets.html", active="booklets", rows=booklets.list_booklets(ctx.db),
                  stats=booklets.stats(ctx.db),
                  default_pages=int(ctx.store.get("booklet.default_pages")))


@router.post("/booklets/generate")
async def booklets_generate(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOOKLETS_MANAGE)
    data = await form_data(request)
    pages = int(data["pages"]) if data.get("pages") else None
    booklet = booklets.generate(ctx.db, ctx.user, pages=pages, settings=ctx.store)
    return flash_redirect("/booklets", f"Booklet {booklet.code} generated with {booklet.pages_total} pages.")
