"""Cuts: Volume % (Agent) and SOP % (Sub Agent), per account and per room. Admin, or a Pawn the Admin gave access to.

Lives in the Admin Panel (/admin/cuts). Master no longer sees or changes cuts. The per-room part is the same system the
Agent Panel uses (app.services.commissions.set_room_cut): there an Agent sets what its own Sub Agents earn.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.errors import NotFound, ValidationError
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
                           "bola_volume": int(s.get("bola.commission.agent_cut_bps")),
                           "bola_sop": int(s.get("bola.commission.sub_agent_cut_bps")),
                           "max_volume": int(s.get("commission.max_cavo_cut_bps")),
                           "max_sop": int(s.get("commission.max_kubrador_cut_bps"))},
                 unlimited=ctx.user.is_admin, can_edit=ctx.can(Perm.CUTS_MANAGE),
                 rooms=commissions.room_rows(ctx.db, s), room_overrides=commissions.override_rows(ctx.db),
                 games=[(g, commissions.GAME_LABELS[g]) for g in commissions.GAMES])


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


@router.post("/admin/cuts/rooms")
async def cuts_room_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Set or clear the cut of one room of one game: the room's default, or one Agent's row for it."""
    ctx.need(Perm.CUTS_MANAGE)
    data = await form_data(request)
    try:
        location_id = int(data.get("location_id", ""))
        agent_raw = (data.get("agent_id") or "").strip()
        agent_id = int(agent_raw) if agent_raw else None
    except ValueError:
        raise ValidationError("Pick a room and, if you want one, an Agent.")
    commissions.set_room_cut(
        ctx.db, ctx.user, game=data.get("game", ""), location_id=location_id, agent_id=agent_id,
        volume=data.get("volume") if "volume" in data else None,
        sop=data.get("sop") if "sop" in data else None,
        settings=ctx.store, reason=(data.get("reason") or None))
    return flash_redirect("/admin/cuts#rooms", "Room cut saved.")


@router.get("/cuts", include_in_schema=False)
async def legacy_cuts(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CUTS_VIEW)
    return flash_redirect("/admin/cuts")
