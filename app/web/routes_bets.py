from __future__ import annotations

import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals
from app.db.models import Side, TicketStatus
from app.services import accounts, betting, events
from app.web.paging import paginate
from app.web.routes_live import live_row
from app.web.deps import Ctx, flash_redirect, form_data, game_gate, render, require_ctx

router = APIRouter(dependencies=[Depends(game_gate("sabong"))])


def _int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid number.") from exc


@router.get("/betlogs")
async def betlogs(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Moved: Records > Tickets (Games Standard 4.8). Kept as a redirect so old links work."""
    ctx.need(Perm.BETS_VIEW)
    from app.web.routes_records import legacy_redirect
    return legacy_redirect(request, "tickets", "sabong", rename={"event_id": "session", "fight_id": "round"})


@router.get("/cancelT")
async def cancel_t(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CANCEL_LOG)        # dev-only
    from app.web.routes_records import legacy_redirect
    return legacy_redirect(request, "cancelled", "sabong")


@router.post("/bets/{ticket_id}/cancel")
async def cancel_bet(ticket_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_CANCEL)
    data = await form_data(request)
    ticket = betting.cancel_ticket(ctx.db, ctx.user, ticket_id=ticket_id,
                                   reason=data.get("reason", ""), settings=ctx.store)
    return flash_redirect("/records?tab=cancelled&game=sabong" if ctx.can(Perm.CANCEL_LOG) else "/records?tab=tickets&game=sabong",
                          f"Ticket {ticket.code} cancelled and refunded.")


@router.get("/tickets/{ticket_id}")
async def ticket_view(ticket_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_VIEW)
    ticket = betting.assert_ticket_visible(ctx.db, ctx.user, betting.get_ticket(ctx.db, ticket_id))
    fight = events.get_fight(ctx.db, ticket.fight_id)
    event = events.get_event(ctx.db, ticket.event_id)
    return render(ctx, "ticket.html", active="betlogs", ticket=ticket, fight=fight, event=event,
                  qr=_qr_svg(ticket.code))


@router.get("/tickets/{ticket_id}/qr.png")
async def ticket_qr(ticket_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_VIEW)
    ticket = betting.assert_ticket_visible(ctx.db, ctx.user, betting.get_ticket(ctx.db, ticket_id))
    import segno
    buffer = io.BytesIO()
    segno.make(ticket.code, error="m").save(buffer, kind="png", scale=4, border=1)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")


def _qr_svg(payload: str) -> str:
    import segno
    buffer = io.BytesIO()
    # scale=1 + omitsize gives a viewBox, so CSS can size it; a fixed width/height with no viewBox leaves the
    # code at its native size in the corner of a bigger box
    segno.make(payload, error="m").save(buffer, kind="svg", scale=1, border=1, xmldecl=False,
                                        omitsize=True, svgclass="qr", lineclass="qrline")
    return buffer.getvalue().decode()


# ---- early bets from the livestream page (JSON, used by the floating Bet panel) ----------------------------------
@router.post("/bets/live/place")
async def early_place(request: Request, ctx: Ctx = Depends(require_ctx)):
    """The livestream Bet panel: the logged-in account bets for itself. Same rules as every bet (all of
    betting.place_bet), plus: refused once the game is on air."""
    ctx.need(Perm.BETS_PLACE)
    if hides_totals(ctx.user):
        raise ValidationError("Betting from the livestream is not available for your account.")
    data = await form_data(request)
    try:
        side = Side(data.get("side", ""))
    except ValueError as exc:
        raise ValidationError("Choose Meron, Wala or Draw.") from exc
    fight = events.get_fight(ctx.db, _int(data.get("fight_id")))
    ticket = betting.place_bet(
        ctx.db, ctx.user, fight_id=fight.id, side=side, stake_cents=to_cents(data.get("amount", "0")),
        settings=ctx.store, player_id=None, is_diy=False,   # the logged-in account bets for itself, from its own credit
        idempotency_key=data.get("idempotency_key") or None, ip=ctx.ip, source="live")
    return JSONResponse({"ok": True, "ticket": {
        "id": ticket.id, "code": ticket.code, "side": side.value, "stake": int(ticket.stake_cents),
        "fight": fight.number, "fight_id": fight.id},
        "balance": int(accounts.wallet_for_user(ctx.db, ctx.user).balance_cents),
        # the game's fresh pools and live multiples, so the payout on screen moves with this reply
        "row": live_row(ctx.db, ctx.store, ctx.user, fight)})


@router.post("/bets/live/undo/{ticket_id}")
async def early_undo(ticket_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_CANCEL)
    ticket = betting.cancel_ticket(ctx.db, ctx.user, ticket_id=ticket_id,
                                   reason="Undone from the livestream page", settings=ctx.store)
    return JSONResponse({"ok": True, "code": ticket.code,
                         "balance": int(accounts.wallet_for_user(ctx.db, ctx.user).balance_cents)
                         if ctx.user.has_wallet else None,
                         "row": live_row(ctx.db, ctx.store, ctx.user, events.get_fight(ctx.db, ticket.fight_id))})
