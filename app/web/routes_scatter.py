"""Scatter (the slot) web routes: the game page and the spin endpoint.

Thin on purpose. ``services/scatter.py`` decides whether a spin may happen and moves the money;
``games/scatter_engine.py`` decides what the spin is. The browser receives the finished round and plays it back.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.errors import Forbidden, LockedState
from app.core.rbac import Perm
from app.games import scatter_engine as engine
from app.services import accounts, badges, scatter
from app.web.deps import Ctx, form_data, game_gate, render, require_ctx

log = logging.getLogger("stelnexus.scatter")
router = APIRouter(dependencies=[Depends(game_gate("scatter"))])


def _need_view(ctx: Ctx) -> None:
    ctx.need(Perm.SCATTER_VIEW)
    if not scatter.enabled(ctx.store):
        raise LockedState("Scatter is switched off right now.")


@router.get("/scatter")
async def scatter_page(ctx: Ctx = Depends(require_ctx)):
    _need_view(ctx)
    can_play = ctx.can(Perm.SCATTER_PLAY) and ctx.user.has_wallet
    balance = accounts.wallet_for_user(ctx.db, ctx.user).balance_cents if ctx.user.has_wallet else 0
    recent = [{"code": r.code, "bet": r.stake_cents, "win": r.win_cents, "x": r.win_x100,
               "free": r.free_spins} for r in scatter.recent(ctx.db, ctx.user, 10)]
    cfg = scatter.config(ctx.store)
    return render(ctx, "scatter.html", active="scatter", cfg=cfg, can_play=can_play, balance=balance,
                  recent=recent, paytable=engine.PAYTABLE, names=engine.SYMBOL_NAMES,
                  scatter_pay=engine.SCATTER_PAY)


@router.post("/scatter/spin")
async def scatter_spin(request: Request, ctx: Ctx = Depends(require_ctx)):
    """One round, resolved completely. JSON in the shape the page's player expects."""
    ctx.need(Perm.SCATTER_PLAY)
    data = await form_data(request)
    try:
        bet = int(data.get("bet", ""))
    except ValueError:
        bet = None
    rnd, result, balance = scatter.spin(
        ctx.db, ctx.user, bet_cents=bet, settings=ctx.store,
        idempotency_key=(data.get("key") or "")[:100] or None, ip=ctx.ip)
    if rnd.win_x100 >= 10_000 or rnd.free_spins or rnd.capped:         # a notable round may unlock a badge right away
        try:
            with ctx.db.begin_nested():                                # never let a badge problem undo a spin
                badges.sync(ctx.db, ctx.user, ctx.store, games={"scatter"})
        except Exception:                                              # noqa: BLE001
            log.exception("badge sync after a scatter round failed")
    return JSONResponse({"ok": True, "code": rnd.code, "bet": rnd.stake_cents, "win": rnd.win_cents,
                         "balance": balance, "result": result},
                        headers={"Cache-Control": "no-store"})
