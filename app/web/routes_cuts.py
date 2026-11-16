"""Cuts: Volume % (Cavo) and SOP % (Kubrador). Admin, or a Pawn the Admin gave access to.

Lives in the Admin Panel (/admin/cuts). Master no longer sees or changes cuts.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.errors import NotFound
from app.core.rbac import Perm
from app.db.models import User
from app.services import commissions
from app.web.deps import Ctx, flash_redirect, form_data, require_ctx
from app.web.routes_admin import panel

router = APIRouter()


@router.get("/admin/cuts")
async def cuts_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CUTS_VIEW)
    s = ctx.store
    return panel(ctx, "cuts", "cuts.html", rows=commissions.cut_rows(ctx.db, ctx.user, s),
                 defaults={"volume": int(s.get("commission.cavo_cut_bps")),
                           "sop": int(s.get("commission.kubrador_cut_bps")),
                           "max_volume": int(s.get("commission.max_cavo_cut_bps")),
                           "max_sop": int(s.get("commission.max_kubrador_cut_bps"))},
                 unlimited=ctx.user.is_admin, can_edit=ctx.can(Perm.CUTS_MANAGE))


@router.post("/admin/cuts/{user_id}")
@router.post("/cuts/{user_id}", include_in_schema=False)
async def cuts_save(user_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CUTS_MANAGE)
    data = await form_data(request)
    target = ctx.db.get(User, user_id)
    if target is None:
        raise NotFound("Account not found.")
    commissions.set_cut(ctx.db, ctx.user, target,
                        volume=data.get("volume") if "volume" in data else None,
                        sop=data.get("sop") if "sop" in data else None,
                        settings=ctx.store, reason=(data.get("reason") or None))
    return flash_redirect("/admin/cuts", f"Cut updated for {target.username}.")


@router.get("/cuts", include_in_schema=False)
async def legacy_cuts(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CUTS_VIEW)
    return flash_redirect("/admin/cuts")
