"""Public live board, the livestream chat (experimental), game history and report archives."""
from __future__ import annotations

import csv
import datetime as dt
import io
import threading
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationError
from app.core.rbac import Perm, bola_visible_to, hides_totals, permissions_of
from app.db.models import ChatMessage, DrawStatus, Fight, FightStatus, Ticket, TicketStatus, User
from app.db.models import Session as SessionRow
from app.db.session import get_db
from app.services import betting, events, reports
from app.web.deps import Ctx, base_ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()


# ---------------------------------------------------------------------------
# Public live board (no sign-in; no player or agent names)
# ---------------------------------------------------------------------------

@router.get("/live", include_in_schema=False)
async def live_board(request: Request, db: Session = Depends(get_db)):
    ctx = base_ctx(request, db)
    if not ctx.store.get("live.board_enabled"):
        raise NotFound("The live board is switched off.")
    board = reports.live_board(db, ctx.store)
    from app.services import bola as _bola
    response = render(ctx, "live.html", board=board, local=_bola.local)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Livestream chat (experimental)
#
# Same room, same permission (`chat.use`: the Admin, and a Pawn the Admin granted it to),
# same stored messages -- presented as a live broadcast desk: who is in the room, what is
# on air right now, who is typing, and an audible cue for incoming messages.
#
# Experimental means exactly two things, and both are stated on the page:
#   * delivery is short-poll (every 4s), not a websocket, so a message can lag a few seconds;
#   * the typing indicator lives in this process's memory (the app runs one worker), so it is
#     best-effort presence, never a record of anything.
# Messages themselves are ordinary rows: persisted, permission-checked, and never anonymous.
# ---------------------------------------------------------------------------

_TYPING_TTL = 6.0
_typing_lock = threading.Lock()
_typing: dict[int, tuple[str, float]] = {}      # user_id -> (display name, monotonic expiry)

ROLE_TONE = {"admin": "gold", "master": "purple", "pawn": "blue", "declarator": "green",
             "cavo": "amber", "kubrador": "amber"}  # retired tiers: old rows only


def _chat_json(rows: list[ChatMessage], ctx: Ctx) -> list[dict]:
    out = []
    for m in rows:
        name = ctx.who(m.user)
        system = name == "System"
        out.append({
            "id": m.id, "user": name,
            "role": "Management" if system else m.user.role_label,
            "tone": "gold" if system else ROLE_TONE.get(str(m.user.role), "slate"),
            "initial": (name[:1] or "?").upper(),
            "body": m.body,
            "at": m.created_at.strftime("%H:%M"),
            "day": m.created_at.strftime("%Y-%m-%d"),
            "iso": m.created_at.isoformat(),
        })
    return out


def _typing_now(ctx: Ctx) -> list[str]:
    now = time.monotonic()
    with _typing_lock:
        for uid in [uid for uid, (_, exp) in _typing.items() if exp <= now]:
            _typing.pop(uid, None)
        return [name for uid, (name, _) in sorted(_typing.items()) if uid != ctx.user.id]


def _room(db: Session, ctx: Ctx) -> list[dict]:
    """Who is in the room: accounts that may use the chat and were seen recently.

    Uses the same session/presence window as the Online page, but never leaks the hidden
    Admin to anybody else (they appear as 'System', exactly like everywhere else).
    """
    window = dt.timedelta(minutes=int(ctx.store.get("presence.window_minutes")))
    now = dt.datetime.now(dt.timezone.utc)
    rows = db.execute(
        select(User, func.max(SessionRow.last_seen_at))
        .join(SessionRow, SessionRow.user_id == User.id)
        .where(SessionRow.revoked_at.is_(None), SessionRow.absolute_expires_at > now,
               SessionRow.last_seen_at >= now - window)
        .group_by(User.id).order_by(func.max(SessionRow.last_seen_at).desc())).all()
    people = []
    for user, seen in rows:
        if Perm.CHAT_USE not in permissions_of(user):
            continue
        name = ctx.who(user)
        people.append({"user": name, "role": "Management" if name == "System" else user.role_label,
                       "tone": "gold" if name == "System" else ROLE_TONE.get(str(user.role), "slate"),
                       "initial": (name[:1] or "?").upper(),
                       "seen": seen.strftime("%H:%M") if seen else "",
                       "me": user.id == ctx.user.id})
    return people[:12]


def _on_air(db: Session, ctx: Ctx) -> dict:
    """The broadcast strip: what is running now and the last results off the wire.

    Read-only aggregates. No player names, no stakes -- the livestream chat is an operations room, not a report.
    """
    from app.db.models import BolaDraw
    from app.services import bola as _bola

    open_fights = int(db.scalar(select(func.count(Fight.id))
                                .where(Fight.status == FightStatus.OPEN)) or 0)
    ticker: list[dict] = []
    for fight in db.scalars(select(Fight).where(Fight.result.is_not(None))
                            .order_by(Fight.declared_at.desc().nulls_last(), Fight.id.desc())
                            .limit(4)).all():
        ticker.append({"kind": "Sabong", "label": f"Fight #{fight.number}",
                       "value": (fight.result.value if fight.result else "").upper(),
                       "tone": "meron" if str(fight.result) == "meron" else
                               ("wala" if str(fight.result) == "wala" else "amber"),
                       "at": fight.declared_at.strftime("%H:%M") if fight.declared_at else ""})
    next_draw = None
    if bola_visible_to(ctx.user) and ctx.store.get("bola.enabled"):
        draw = _bola.next_draw(db)
        if draw is not None:
            next_draw = {"id": draw.id, "area": draw.location.name if draw.location else "—",
                         "slot": draw.slot, "status": str(draw.status),
                         "at": _bola.local(draw.scheduled_at).strftime("%H:%M"),
                         "iso": draw.scheduled_at.isoformat(),
                         "cutoff_iso": draw.cutoff_at.isoformat(),
                         "bettable": _bola.bettable(draw)}
        for draw in db.scalars(select(BolaDraw).where(BolaDraw.status == DrawStatus.SETTLED)
                               .order_by(BolaDraw.declared_at.desc().nulls_last(), BolaDraw.id.desc())
                               .limit(3)).all():
            ticker.append({"kind": "Bola", "label": (draw.location.name if draw.location else "Draw"),
                           "value": draw.result_label or "—", "tone": "gold",
                           "at": draw.declared_at.strftime("%H:%M") if draw.declared_at else ""})
    ticker.sort(key=lambda item: item["at"], reverse=True)
    return {"open_fights": open_fights, "next_draw": next_draw, "ticker": ticker[:6]}


@router.get("/chat")
async def chat_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CHAT_USE)
    rows = list(ctx.db.scalars(select(ChatMessage).where(ChatMessage.fight_id.is_(None))
                               .order_by(ChatMessage.id.desc()).limit(60)).all())
    return render(ctx, "chat.html", active="chat", messages=_chat_json(list(reversed(rows)), ctx),
                  room=_room(ctx.db, ctx), on_air=_on_air(ctx.db, ctx), typing=_typing_now(ctx))


@router.get("/chat/messages")
async def chat_messages(after: int = 0, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CHAT_USE)
    rows = list(ctx.db.scalars(select(ChatMessage).where(ChatMessage.fight_id.is_(None), ChatMessage.id > max(after, 0))
                               .order_by(ChatMessage.id).limit(100)).all())
    return JSONResponse({"messages": _chat_json(rows, ctx), "typing": _typing_now(ctx),
                         "room": _room(ctx.db, ctx), "on_air": _on_air(ctx.db, ctx)},
                        headers={"Cache-Control": "no-store"})


@router.post("/chat/typing")
async def chat_typing(ctx: Ctx = Depends(require_ctx)):
    """Best-effort 'someone is composing' ping. In-memory, expires by itself, never stored."""
    ctx.need(Perm.CHAT_USE)
    name = ctx.who(ctx.user)
    with _typing_lock:
        if len(_typing) > 64:                     # bounded, like the rate limiter
            now = time.monotonic()
            for uid in [uid for uid, (_, exp) in _typing.items() if exp <= now]:
                _typing.pop(uid, None)
        _typing[ctx.user.id] = (name, time.monotonic() + _TYPING_TTL)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


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
    message = ChatMessage(user_id=ctx.user.id, body=body)
    ctx.db.add(message)
    ctx.db.flush()
    with _typing_lock:
        _typing.pop(ctx.user.id, None)            # sent: stop showing them as typing
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "id": message.id})
    return flash_redirect("/chat")


# ---------------------------------------------------------------------------
# Previous game history & report archives
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
                     "House (tong + over)", "Status"])
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
                         fight.status.value])
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
