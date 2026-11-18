from __future__ import annotations

import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm
from app.db.models import Side, TicketStatus
from app.services import betting, events
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


def _int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid number.") from exc


@router.get("/betlogs")
async def betlogs(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_VIEW)
    params = request.query_params
    status = params.get("status") or None
    rows = betting.list_tickets(
        ctx.db, ctx.user, event_id=_int(params.get("event_id")) or None,
        fight_id=_int(params.get("fight_id")) or None,
        status=TicketStatus(status) if status in ("active", "canceled", "settled", "refunded") else None,
        search=params.get("q", "")[:40] or None, limit=300,
    )
    return render(ctx, "betlogs.html", active="betlogs", rows=rows,
                  events=events.list_events(ctx.db, limit=50),
                  filters={"event_id": params.get("event_id", ""), "q": params.get("q", ""),
                           "status": status or ""})


@router.get("/cancelT")
async def cancel_t(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CANCEL_LOG)        # dev-only
    rows = betting.list_tickets(ctx.db, ctx.user, canceled_only=True, limit=300)
    return render(ctx, "cancelT.html", active="cancelT", rows=rows)


@router.post("/bets/place")
async def place_bet(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_PLACE)
    data = await form_data(request)
    fight_id = _int(data.get("fight_id"))
    fight = events.get_fight(ctx.db, fight_id)
    try:
        side = Side(data.get("side", ""))
    except ValueError as exc:
        raise ValidationError("Choose Meron, Wala or Draw.") from exc

    ticket = betting.place_bet(
        ctx.db, ctx.user, fight_id=fight_id, side=side,
        stake_cents=to_cents(data.get("amount", "0")), settings=ctx.store,
        player_id=_int(data.get("player_id")) or None,
        is_diy=data.get("is_diy") == "1",
        idempotency_key=data.get("idempotency_key") or None,
        numbers=data.get("numbers"), kubrador=data.get("kubrador"), cavo=data.get("cavo"),
        ip=ctx.ip,
    )
    if data.get("print") == "1":
        return flash_redirect(f"/tickets/{ticket.id}", f"Ticket {ticket.code} sold.")
    return flash_redirect(f"/events/{fight.event_id}", f"Ticket {ticket.code} sold.")


@router.post("/bets/{ticket_id}/cancel")
async def cancel_bet(ticket_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_CANCEL)
    data = await form_data(request)
    ticket = betting.cancel_ticket(ctx.db, ctx.user, ticket_id=ticket_id,
                                   reason=data.get("reason", ""), settings=ctx.store)
    return flash_redirect("/cancelT" if ctx.can(Perm.CANCEL_LOG) else "/betlogs",
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
    segno.make(payload, error="m").save(buffer, kind="svg", scale=3, border=1, xmldecl=False)
    return buffer.getvalue().decode()
