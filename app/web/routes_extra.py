"""Counter claim (QR), public live board, staff chat, game history and report archives."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationError
from app.core.rbac import Perm, hides_totals
from app.db.models import ChatMessage, Fight, Ticket, TicketStatus
from app.db.session import get_db
from app.services import betting, events, reports
from app.web.deps import Ctx, base_ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()


# ---------------------------------------------------------------------------
# Claim a ticket at the counter
# ---------------------------------------------------------------------------

@router.get("/claim")
async def claim_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_CLAIM)
    code = (request.query_params.get("code") or "").strip()
    ticket = fight = None
    if code:
        ticket = betting.assert_ticket_visible(ctx.db, ctx.user, betting.find_by_scan(ctx.db, code))
        fight = events.get_fight(ctx.db, ticket.fight_id)
    recent = list(ctx.db.scalars(
        select(Ticket).where(Ticket.claimed_by_id == ctx.user.id).order_by(Ticket.claimed_at.desc()).limit(8)
    ).all())
    return render(ctx, "claim.html", active="claim", ticket=ticket, fight=fight, code=code,
                  state=betting.claim_status(ticket) if ticket else None, recent=recent)


@router.post("/claim/{ticket_id}")
async def claim_do(ticket_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BETS_CLAIM)
    ticket = betting.claim_ticket(ctx.db, ctx.user, ticket_id=ticket_id)
    return flash_redirect("/claim", f"Ticket {ticket.code} claimed. Pay out {ticket.payout_cents / 100:,.2f}.")


# ---------------------------------------------------------------------------
# Public live board (no sign-in; no player or agent names)
# ---------------------------------------------------------------------------

@router.get("/live", include_in_schema=False)
async def live_board(request: Request, db: Session = Depends(get_db)):
    ctx = base_ctx(request, db)
    if not ctx.store.get("live.board_enabled"):
        raise NotFound("The live board is switched off.")
    board = reports.live_board(db, ctx.store)
    response = render(ctx, "live.html", board=board)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Staff chat
# ---------------------------------------------------------------------------

def _chat_json(rows: list[ChatMessage]) -> list[dict]:
    return [{"id": m.id, "user": m.user.username, "role": m.user.role_label,
             "body": m.body, "at": m.created_at.strftime("%H:%M")} for m in rows]


@router.get("/chat")
async def chat_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CHAT_USE)
    rows = list(ctx.db.scalars(select(ChatMessage).order_by(ChatMessage.id.desc()).limit(60)).all())
    return render(ctx, "chat.html", active="chat", messages=_chat_json(list(reversed(rows))))


@router.get("/chat/messages")
async def chat_messages(after: int = 0, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CHAT_USE)
    rows = list(ctx.db.scalars(select(ChatMessage).where(ChatMessage.id > max(after, 0))
                               .order_by(ChatMessage.id).limit(100)).all())
    return JSONResponse({"messages": _chat_json(rows)}, headers={"Cache-Control": "no-store"})


@router.post("/chat/send")
async def chat_send(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CHAT_USE)
    if "json" in request.headers.get("content-type", ""):
        try:
            data = await request.json()
        except ValueError as exc:
            raise ValidationError("Invalid message.") from exc
        data = data if isinstance(data, dict) else {}
    else:
        data = await form_data(request)
    body = " ".join(str(data.get("body") or "").split())[:300]
    if not body:
        raise ValidationError("Type a message first.")
    ctx.db.add(ChatMessage(user_id=ctx.user.id, body=body))
    ctx.db.flush()
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return flash_redirect("/chat")


# ---------------------------------------------------------------------------
# Game history & report archives
# ---------------------------------------------------------------------------

@router.get("/history")
async def history_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_VIEW)
    fights = reports.history(ctx.db, limit=150)
    hide = hides_totals(ctx.user)
    totals = {}
    if not hide:
        for fight in fights:
            totals[fight.id] = betting.pool_snapshot(ctx.db, fight)
    event_by_id = {e.id: e for e in (events.get_event(ctx.db, i) for i in {f.event_id for f in fights})}
    return render(ctx, "history.html", active="history", fights=fights, events=event_by_id,
                  hide_totals=hide, totals=totals, role_of=events.role_of)


@router.get("/archives")
async def archives_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.REPORTS_VIEW)
    rows = reports.archived_events(ctx.db)
    return render(ctx, "archives.html", active="archives",
                  rows=[(e, reports.event_summary(ctx.db, e.id)) for e in rows])


@router.get("/events/{event_id}/report.csv")
async def event_report_csv(event_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.REPORTS_VIEW)
    event = events.get_event(ctx.db, event_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Event", _safe(event.title), "Date", event.event_date.isoformat(), "Status", event.status.value])
    writer.writerow([])
    writer.writerow(["Game", "Result", "Llamado/Dehado", "Meron", "Wala", "Draw", "Staked", "Paid out",
                     "House (tong + over)", "Unclaimed cash payouts", "Status"])
    for fight in events.fights_of(ctx.db, event.id):
        fig = reports.fight_figures(ctx.db, fight)
        rows = ctx.db.execute(select(Ticket.side, Ticket.stake_cents, Ticket.payout_cents).where(
            Ticket.fight_id == fight.id,
            Ticket.status.in_([TicketStatus.ACTIVE, TicketStatus.SETTLED, TicketStatus.REFUNDED]))).all()
        by_side = {"meron": 0, "wala": 0, "draw": 0}
        total_stake = total_paid = 0
        for side, stake, payout in rows:
            by_side[str(side)] += stake
            total_stake += stake
            total_paid += payout
        writer.writerow([fight.number, fight.result.value if fight.result else "",
                         events.role_of(fight, fight.result.value if fight.result else None) or "",
                         f"{by_side['meron'] / 100:.2f}", f"{by_side['wala'] / 100:.2f}",
                         f"{by_side['draw'] / 100:.2f}", f"{total_stake / 100:.2f}",
                         f"{total_paid / 100:.2f}", f"{fig['system_over'] / 100:.2f}",
                         f"{fig['unclaimed'] / 100:.2f}", fight.status.value])
    summary = reports.event_summary(ctx.db, event.id)
    writer.writerow([])
    for label, key in (("Total staked", "staked"), ("Paid out", "payouts"), ("House take", "house"),
                       ("Agent commissions", "commission"), ("Event expenses", "expenses"),
                       ("Net income", "net")):
        writer.writerow([label, f"{summary[key] / 100:.2f}"])
    for expense in events.list_expenses(ctx.db, event.id):
        writer.writerow(["Expense", _safe(expense.label), f"{expense.amount_cents / 100:.2f}", _safe(expense.note or "")])
    text = out.getvalue()
    return Response(text, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="event-{event.id}-report.csv"',
        "Cache-Control": "no-store"})


def _safe(value: str) -> str:
    """Neutralise spreadsheet formulas in free-text cells (=, +, -, @ at the start)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value
