"""Achievements page (/achievements) and the profile setting that puts up to three earned badges next to a name."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.services import badges
from app.web.deps import Ctx, flash_redirect, render, require_ctx

router = APIRouter()


@router.get("/achievements")
async def achievements_page(ctx: Ctx = Depends(require_ctx)):
    badges.sync(ctx.db, ctx.user, ctx.store)                   # award anything newly reached before drawing
    board = badges.board(ctx.db, ctx.user, ctx.store)
    return render(ctx, "achievements.html", active="achievements", board=board, tiers=badges.TIERS,
                  tier_order=badges.TIER_ORDER, game_labels=badges.GAME_LABELS, game_order=badges.GAME_ORDER,
                  max_shown=badges.MAX_SHOWN)


@router.post("/profile/badges")
async def save_showcase(request: Request, ctx: Ctx = Depends(require_ctx)):
    form = await request.form()
    keys = [v for v in form.getlist("badge") if isinstance(v, str)]
    shown = badges.set_showcase(ctx.db, ctx.user, keys, ctx.store)
    nxt = "/achievements" if form.get("next") == "achievements" else "/profile"
    return flash_redirect(nxt, "Badges next to your name: " + (", ".join(b.name for b in shown) if shown else "none."))
