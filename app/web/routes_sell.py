"""Seller mode (EXPERIMENTAL, Admin/dev only: Perm.SELL_DESK): a full-screen cashier desk with big Meron / Wala buttons and a numpad.

It reuses the exact same service calls as the regular "Sell ticket" form (`betting.place_bet`,
`betting.cancel_ticket`), so every rule still applies: KYC, credit, bet mode, locked games, the
idempotency key and the cancel window. Nothing here can do more than the old form could.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals
from app.db.models import EventStatus, Side
from app.services import betting, events
from app.services import players as player_service
from app.web.deps import Ctx, form_data, render, require_ctx

router = APIRouter()


def _int(raw: str | None) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid identifier.") from exc


@router.get("/sell")
async def sell_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.SELL_DESK)
    if hides_totals(ctx.user):
        raise ValidationError("Seller mode is not available for your account.")
    rows = [e for e in events.list_events(ctx.db, limit=60) if e.status == EventStatus.OPEN]
    wanted = _int(request.query_params.get("event_id"))
    players = player_service.list_players(ctx.db, ctx.user, limit=300)
    return render(ctx, "sell.html", active="sell", events=rows,
                  event_id=wanted if any(e.id == wanted for e in rows) else (rows[0].id if rows else 0),
                  players=[{"id": p.id, "name": p.full_name, "ref": p.username or p.card_number or ""}
                           for p in players],
                  draw_enabled=bool(ctx.store.get("fight.draw_bets_enabled")),
                  can_undo=ctx.can(Perm.BETS_CANCEL),
                  undo_window=int(ctx.store.get("fight.cancel_window_seconds") or 0))


@router.post("/sell/place")
async def sell_place(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.SELL_DESK)
    if hides_totals(ctx.user):
        raise ValidationError("Seller mode is not available for your account.")
    data = await form_data(request)
    try:
        side = Side(data.get("side", ""))
    except ValueError as exc:
        raise ValidationError("Choose Meron, Wala or Draw.") from exc
    fight = events.get_fight(ctx.db, _int(data.get("fight_id")))
    ticket = betting.place_bet(
        ctx.db, ctx.user, fight_id=fight.id, side=side, stake_cents=to_cents(data.get("amount", "0")),
        settings=ctx.store, player_id=_int(data.get("player_id")) or None, is_diy=False,
        idempotency_key=data.get("idempotency_key") or None, ip=ctx.ip)
    return JSONResponse({"ok": True, "ticket": {
        "id": ticket.id, "code": ticket.code, "side": side.value, "stake": int(ticket.stake_cents),
        "fight": fight.number, "fight_id": fight.id}})


@router.post("/sell/undo/{ticket_id}")
async def sell_undo(ticket_id: int, ctx: Ctx = Depends(require_ctx)):
    """Take back a ticket the cashier just sold (mistyped side or amount). Refunds the stake in full."""
    ctx.need(Perm.SELL_DESK)
    ticket = betting.cancel_ticket(ctx.db, ctx.user, ticket_id=ticket_id,
                                   reason="Undone at the seller desk", settings=ctx.store)
    return JSONResponse({"ok": True, "code": ticket.code})
