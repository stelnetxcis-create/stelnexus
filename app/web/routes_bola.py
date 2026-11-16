"""Bola (Tumbok-Sahod) web routes: lobby, draw page with bet slip, tickets, results, operations.

Everything here is a thin layer over ``services/bola.py`` and ``services/bola_settlement.py``.
The whole feature stays dark until the ``bola.enabled`` setting is switched on by the Admin.
"""
from __future__ import annotations

import datetime as dt
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.errors import StelError, ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals
from app.db.models import BolaKind, TicketStatus
from app.services import bola, bola_settlement, locations, players
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()

KIND_LABELS = {
    BolaKind.PAIR: "Tumbok-Sahod (2 numbers, 1 line)",
    BolaKind.CASAS_3: "3 Casas (3 numbers, 3 lines)",
    BolaKind.CASAS_6: "6 Casas (4 numbers, 6 lines)",
    BolaKind.CASAS_10: "10 Casas (5 numbers, 10 lines)",
}


def _int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid number.") from exc


def _numbers(data: dict) -> list[int]:
    """Number inputs are n1..n5 (or one comma separated field); blanks are ignored."""
    raw = [data.get(f"n{i}", "") for i in range(1, 6)]
    if not any(raw) and data.get("numbers"):
        raw = str(data["numbers"]).replace(" ", "").split(",")
    return [_int(x) for x in raw if str(x).strip() != ""]


def _qr_svg(payload: str) -> str:
    import segno
    buffer = io.BytesIO()
    segno.make(payload, error="m").save(buffer, kind="svg", scale=3, border=1, xmldecl=False)
    return buffer.getvalue().decode()


def _footer(ctx: Ctx) -> str:
    try:
        return str(ctx.store.get("ticket.footer") or "")
    except Exception:  # setting not defined in this build
        return ""


def _need_view(ctx: Ctx) -> None:
    ctx.need(Perm.BOLA_VIEW)


# ------------------------------------------------------------------ lobby / results
@router.get("/bola")
async def bola_lobby(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_view(ctx)
    day = request.query_params.get("date")
    try:
        chosen = dt.date.fromisoformat(day) if day else None
    except ValueError:
        chosen = None
    return render(ctx, "bola_lobby.html", active="bola", board=bola.lobby(ctx.db, day=chosen),
                  enabled=bool(ctx.store.get("bola.enabled")), local=bola.local,
                  areas=locations.list_locations(ctx.db, active_only=True),
                  can_manage=ctx.can(Perm.BOLA_MANAGE))


@router.get("/bola/results")
async def bola_results(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_view(ctx)
    loc = _int(request.query_params.get("location_id")) or None
    return render(ctx, "bola_results.html", active="bola", rows=bola.results(ctx.db, location_id=loc),
                  areas=locations.list_locations(ctx.db, active_only=True), location_id=loc,
                  local=bola.local)


# ------------------------------------------------------------------ draw page
@router.get("/bola/draws/{draw_id}")
async def bola_draw(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    _need_view(ctx)
    draw = bola.get_draw(ctx.db, draw_id)
    hide = hides_totals(ctx.user)
    exposure = heat = tickets = None
    if ctx.can(Perm.BOLA_EXPOSURE) and not hide:
        exposure = bola.exposure_summary(ctx.db, draw, ctx.store)
        cells = [c for row in bola.heatmap(ctx.db, draw, ctx.store)["cells"] for c in row if c["stake"]]
        heat = sorted(cells, key=lambda c: (-c["liability"], -c["stake"]))[:15]
    if not hide:
        tickets = bola.list_tickets(ctx.db, ctx.user, draw_id=draw.id, limit=300)
    my_players = [] if hide or not ctx.can(Perm.BOLA_PLACE) else players.list_players(
        ctx.db, ctx.user, limit=300)
    return render(
        ctx, "bola_draw.html", active="bola", draw=draw, tickets=tickets, exposure=exposure,
        heat=heat, my_players=my_players, hide_totals=hide, local=bola.local,
        bettable=bola.bettable(draw), enabled=bool(ctx.store.get("bola.enabled")),
        kinds=[(k.value, KIND_LABELS[k]) for k in BolaKind],
        odds={"multiplier": bola.multiplier(ctx.store) / 100, "outcomes": bola.ordered_outcomes(ctx.store),
              "rtp": round(bola.rtp_percent(ctx.store), 1),
              "show": bool(ctx.store.get("bola.show_odds_to_players")),
              "max_number": bola.max_number(ctx.store)},
        runs=bola_settlement.runs_for(ctx.db, draw.id),
        can_manage=ctx.can(Perm.BOLA_MANAGE), can_declare=ctx.can(Perm.BOLA_DECLARE),
        can_redeclare=ctx.can(Perm.BOLA_REDECLARE), can_approve=ctx.can(Perm.BOLA_APPROVE),
        can_place=ctx.can(Perm.BOLA_PLACE), can_cancel=ctx.can(Perm.BOLA_CANCEL),
    )


@router.get("/bola/draws/{draw_id}/preview")
async def bola_preview(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Bet slip preview for the JS: lines, total, per-line payouts. Changes nothing."""
    ctx.need(Perm.BOLA_PLACE)
    bola.get_draw(ctx.db, draw_id)
    q = request.query_params
    try:
        numbers = [int(x) for x in q.get("numbers", "").split(",") if x.strip()]
        data = bola.slip_preview(q.get("kind", ""), numbers, to_cents(q.get("tumbok", "0") or "0"),
                                 to_cents(q.get("sahod", "0") or "0"), ctx.store)
        return JSONResponse({"ok": True, **data})
    except (StelError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": getattr(exc, "message", str(exc))})


# ------------------------------------------------------------------ bets and tickets
@router.post("/bola/bets/place")
async def bola_place(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_PLACE)
    data = await form_data(request)
    draw_id = _int(data.get("draw_id"))
    try:
        kind = BolaKind(data.get("kind", ""))
    except ValueError as exc:
        raise ValidationError("Choose a ticket type.") from exc
    ticket = bola.place_bola_bet(
        ctx.db, ctx.user, draw_id=draw_id, kind=kind, numbers=_numbers(data),
        tumbok_cents=to_cents(data.get("tumbok", "0") or "0"),
        sahod_cents=to_cents(data.get("sahod", "0") or "0"),
        player_id=_int(data.get("player_id")), settings=ctx.store,
        idempotency_key=data.get("idempotency_key") or None,
        note=(data.get("note") or None), ip=ctx.ip)
    if data.get("print") == "1":
        return flash_redirect(f"/bola/tickets/{ticket.id}", f"Ticket {ticket.code} sold.")
    return flash_redirect(f"/bola/draws/{draw_id}", f"Ticket {ticket.code} sold.")


@router.post("/bola/bets/{ticket_id}/cancel")
async def bola_cancel(ticket_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_CANCEL)
    data = await form_data(request)
    ticket = bola.cancel_bola_ticket(ctx.db, ctx.user, ticket_id=ticket_id,
                                     reason=data.get("reason", ""), settings=ctx.store)
    return flash_redirect(f"/bola/draws/{ticket.draw_id}", f"Ticket {ticket.code} cancelled and refunded.")


@router.get("/bola/tickets/{ticket_id}")
async def bola_ticket(ticket_id: int, ctx: Ctx = Depends(require_ctx)):
    _need_view(ctx)
    ticket = bola.assert_ticket_visible(ctx.db, ctx.user, bola.get_ticket(ctx.db, ticket_id))
    return render(ctx, "bola_ticket.html", active="bola", ticket=ticket,
                  lines=bola.ticket_lines(ctx.db, ticket), draw=ticket.draw,
                  qr=_qr_svg(ticket.code), local=bola.local,
                  multiplier=bola.multiplier(ctx.store) / 100,
                  can_cancel=ctx.can(Perm.BOLA_CANCEL), footer=_footer(ctx), header=str(ctx.store.get("ticket.header") or ""))


@router.get("/bola/tickets/{ticket_id}/qr.png")
async def bola_ticket_qr(ticket_id: int, ctx: Ctx = Depends(require_ctx)):
    _need_view(ctx)
    ticket = bola.assert_ticket_visible(ctx.db, ctx.user, bola.get_ticket(ctx.db, ticket_id))
    import segno
    buffer = io.BytesIO()
    segno.make(ticket.code, error="m").save(buffer, kind="png", scale=4, border=1)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")


@router.get("/bola/betlogs")
async def bola_betlogs(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_view(ctx)
    if hides_totals(ctx.user):
        from app.core.errors import Forbidden
        raise Forbidden("Your role cannot see tickets.")
    p = request.query_params
    status = p.get("status") or None
    rows = bola.list_tickets(
        ctx.db, ctx.user, draw_id=_int(p.get("draw_id")) or None, search=p.get("q", "")[:40] or None,
        status=TicketStatus(status) if status in ("active", "canceled", "settled", "refunded") else None,
        limit=300)
    return render(ctx, "bola_betlogs.html", active="bola", rows=rows,
                  filters={"q": p.get("q", ""), "status": status or "", "draw_id": p.get("draw_id", "")})


# ------------------------------------------------------------------ operations
@router.post("/bola/draws/create")
async def bola_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    data = await form_data(request)
    try:
        day = dt.date.fromisoformat(data.get("draw_date", ""))
    except ValueError as exc:
        raise ValidationError("Enter the draw date.") from exc
    draw = bola.create_draw(ctx.db, ctx.user, location_id=_int(data.get("location_id")), draw_date=day,
                            slot=_int(data.get("slot"), 1), settings=ctx.store,
                            draw_time=data.get("draw_time") or None)
    return flash_redirect(f"/bola/draws/{draw.id}", "Draw created.")


@router.post("/bola/draws/generate")
async def bola_generate(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    data = await form_data(request)
    out = bola.generate_draws(ctx.db, ctx.user, settings=ctx.store, days=_int(data.get("days"), 1))
    return flash_redirect("/bola", f"Created {len(out['created'])} draw(s); {out['skipped']} already existed.")


@router.post("/bola/draws/{draw_id}/open")
async def bola_open(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    bola.open_draw(ctx.db, ctx.user, draw_id)
    return flash_redirect(f"/bola/draws/{draw_id}", "Betting is open.")


@router.post("/bola/draws/{draw_id}/lock")
async def bola_lock(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    bola.lock_draw(ctx.db, ctx.user, draw_id)
    return flash_redirect(f"/bola/draws/{draw_id}", "Betting is closed.")


@router.post("/bola/draws/{draw_id}/void")
async def bola_void(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    data = await form_data(request)
    bola_settlement.void_draw(ctx.db, ctx.user, draw_id=draw_id, reason=data.get("reason", ""),
                              settings=ctx.store)
    return flash_redirect(f"/bola/draws/{draw_id}", "Draw voided. Every stake was refunded.")


@router.post("/bola/draws/{draw_id}/declare")
async def bola_declare(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_DECLARE)
    data = await form_data(request)
    bola_settlement.declare(ctx.db, ctx.user, draw_id=draw_id, ball_first=_int(data.get("ball_first")),
                            ball_second=_int(data.get("ball_second")), settings=ctx.store,
                            reason=data.get("reason") or None)
    return flash_redirect(f"/bola/draws/{draw_id}", "Result declared and settled.")


@router.post("/bola/draws/{draw_id}/correct")
async def bola_correct(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_REDECLARE)
    data = await form_data(request)
    action = "recompute" if data.get("action") == "recompute" else "redeclare"
    run = bola_settlement.request_correction(
        ctx.db, ctx.user, draw_id=draw_id, reason=data.get("reason", ""), settings=ctx.store,
        ball_first=_int(data.get("ball_first")) or None,
        ball_second=_int(data.get("ball_second")) or None, action=action)
    note = "Correction applied." if str(run.status) == "applied" else "Correction sent for approval."
    return flash_redirect(f"/bola/draws/{draw_id}", note)


@router.post("/bola/runs/{run_id}/approve")
async def bola_approve(run_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_APPROVE)
    run = bola_settlement.approve(ctx.db, ctx.user, run_id=run_id, settings=ctx.store)
    return flash_redirect(f"/bola/draws/{run.draw_id}", "Correction approved and applied.")


@router.post("/bola/runs/{run_id}/reject")
async def bola_reject(run_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_APPROVE)
    data = await form_data(request)
    run = bola_settlement.reject(ctx.db, ctx.user, run_id=run_id, note=data.get("note", ""))
    return flash_redirect(f"/bola/draws/{run.draw_id}", "Correction rejected.")
