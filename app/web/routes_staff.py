"""The Staff Panel: one section per staff role, below the Admin Panel.

    /staff              overview (every section; only the ones you may open are clickable)
    /staff/teller       the GCash pool: numbers, limit tracker, rotation (Teller, Admin; Pawn read-only)
    /staff/declarator   the Declarator Desk: every live game and Bola draw on one board, declared in place

A Teller manages only its own numbers and sees only those. The Admin manages every number and sees
the whole pool. A Pawn the Admin switched it on for sees the whole pool but can change nothing.
Master has no access.

The Declarator Desk never shows money (see services/declarator.py). Its actions here are thin JSON
twins of the Fight View / Bola routes: same services, same permission checks, same audit trail, but
they answer with JSON so the desk can stay on the board instead of bouncing to another page.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm
from app.db.models import EventStatus, Role, Status, User
from app.services import bola, bola_settlement, cashdesk, declarator, events, gcash, proofs, settlement
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx
from app.web.routes_fights import _form_with_proof
from app.web.routes_live import _reply
from sqlalchemy import select

router = APIRouter()

SECTIONS = (
    # key, url, label, perm
    ("overview", "/staff", "Overview", Perm.STAFF_VIEW),
    ("teller", "/staff/teller", "Teller", Perm.STAFF_TELLER),
    ("declarator", "/staff/declarator", "Declarator", Perm.STAFF_DECLARATOR),
)


def staff(ctx: Ctx, section: str, template: str, **extra):
    tabs = [(key, url, label) for key, url, label, perm in SECTIONS
            if key == "overview" or ctx.can(perm)]
    return render(ctx, template, active="staff", staff_tabs=tabs, staff_active=section, **extra)


def _limit(raw: str | None) -> int | None:
    """A limit typed in pesos; blank follows the global default."""
    raw = (raw or "").strip()
    return to_cents(raw) if raw else None


@router.get("/staff")
async def staff_home(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_VIEW)
    teller_open, decl_open = ctx.can(Perm.STAFF_TELLER), ctx.can(Perm.STAFF_DECLARATOR)
    teller_value, teller_hint = "Open", "GCash numbers and limits"
    if teller_open:
        mine = None if ctx.user.role != Role.TELLER else ctx.user.id
        live = [a for a in gcash.list_accounts(ctx.db, owner_id=mine) if a.is_active]
        teller_value, teller_hint = f"{len(live)} live", "GCash numbers, limits and rotation"
    decl_value, decl_hint = "Open", "Every live game on one board"
    if decl_open:
        waiting = declarator.attention(ctx.db, sabong=ctx.can(Perm.FIGHTS_VIEW), with_bola=_bola_visible(ctx))
        decl_value = f"{waiting} waiting" if waiting else "All clear"
        decl_hint = "Games waiting for a result" if waiting else "No game is waiting for a result"
    sections = [
        dict(label="Teller", url="/staff/teller", open=teller_open, value=teller_value,
             hint=teller_hint, tone="green", locked_hint="Tellers only"),
        dict(label="Declarator", url="/staff/declarator", open=decl_open, value=decl_value,
             hint=decl_hint, tone="amber" if decl_open and decl_value.endswith("waiting") else "",
             locked_hint="Declarators only"),
    ]
    return staff(ctx, "overview", "staff_home.html", sections=sections)


# --------------------------------------------------------------------------- declarator desk

def _bola_visible(ctx: Ctx) -> bool:
    """Bola draws belong on the board only while the number game is on and this account may see it."""
    return ctx.can(Perm.BOLA_VIEW) and bool(ctx.store.get("bola.enabled"))


def _desk(ctx: Ctx) -> dict:
    can = {
        "lock": ctx.can(Perm.FIGHTS_LOCK), "declare": ctx.can(Perm.FIGHTS_DECLARE),
        "open_event": ctx.can(Perm.EVENTS_OPEN) or ctx.can(Perm.EVENTS_MANAGE),     # open or close an event
        "bola": ctx.can(Perm.BOLA_DECLARE),
        "bola_open": ctx.can(Perm.BOLA_OPEN) or ctx.can(Perm.BOLA_MANAGE),
    }
    return declarator.board(ctx.db, ctx.user, ctx.store, can=can, sabong=ctx.can(Perm.FIGHTS_VIEW),
                            with_bola=_bola_visible(ctx))


def _ok(message: str, level: str = "success") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "level": level}, headers={"Cache-Control": "no-store"})


@router.get("/staff/declarator")
async def staff_declarator(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_DECLARATOR)
    return staff(ctx, "declarator", "staff_declarator.html", boot=_desk(ctx))


@router.get("/_live/declarator")
async def live_declarator(request: Request, ctx: Ctx = Depends(require_ctx)):
    """The desk's poll: the whole board, with an ETag so an idle poll costs a few header bytes."""
    ctx.need(Perm.STAFF_DECLARATOR)
    return _reply(request, _desk(ctx))


@router.post("/staff/declarator/fights/{fight_id}/declare")
async def desk_declare(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_DECLARATOR)
    ctx.need(Perm.FIGHTS_DECLARE)
    data, proof = await _form_with_proof(request)
    if proof is None and proofs.required_for(ctx.user, ctx.store):
        raise ValidationError("Add a picture of the result before you declare this game.")
    fight = events.get_fight(ctx.db, fight_id)
    result = events.resolve_result(fight, data.get("result", ""))
    run = settlement.declare(ctx.db, ctx.user, fight_id=fight_id, result=result, settings=ctx.store,
                             reason=data.get("reason"))
    if proof is not None:
        proofs.attach(ctx.db, ctx.user, fight, proof)
    if (run.totals or {}).get("auto_cancel"):
        floor = int(ctx.store.get("payout.min_odds_bps")) / 10000
        return _ok(f"Game {fight.number}: odds were below {floor:.2f}x, so it was cancelled and every stake "
                   "refunded.", "warning")
    return _ok(f"Game {fight.number} declared {result.value.upper()} and settled.")


@router.post("/staff/declarator/fights/{fight_id}/lock")
async def desk_lock(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_DECLARATOR)
    ctx.need(Perm.FIGHTS_LOCK)
    data = await form_data(request)
    locking = data.get("locked", "1") == "1"
    fight = events.lock_fight(ctx.db, ctx.user, fight_id, locked=locking)
    return _ok(f"Game {fight.number}: betting closed." if locking else f"Game {fight.number}: betting re-opened.",
               "success" if not locking else "warning")


@router.post("/staff/declarator/events/{event_id}/stream")
async def desk_stream(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Put a game on air, or send it back to pre-show (the Fight View's Start game button)."""
    ctx.need(Perm.STAFF_DECLARATOR)
    ctx.need(Perm.FIGHTS_LOCK)
    data = await form_data(request)
    live = data.get("action") == "start"
    fid = data.get("fight_id", "")
    fight = events.set_stream_live(ctx.db, ctx.user, event_id, live, int(fid) if str(fid).isdigit() else None)
    if live:
        return _ok(f"Game {fight.number} is on air. Betting and chat are closed until it is declared.")
    return _ok(f"Game {fight.number} is back to pre-show: betting and chat are open.", "warning")


@router.post("/staff/declarator/events/{event_id}/open")
async def desk_open_event(event_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_DECLARATOR)
    if not ctx.can(Perm.EVENTS_MANAGE):
        ctx.need(Perm.EVENTS_OPEN)
    event = events.set_event_status(ctx.db, ctx.user, event_id, EventStatus.OPEN)
    return _ok(f"{event.title} is open.")


@router.post("/staff/declarator/events/{event_id}/close")
async def desk_close_event(event_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_DECLARATOR)
    if not ctx.can(Perm.EVENTS_MANAGE):
        ctx.need(Perm.EVENTS_OPEN)
    event = events.set_event_status(ctx.db, ctx.user, event_id, EventStatus.CLOSED)   # refuses while bets are unsettled
    return _ok(f"{event.title} is closed.", "warning")


def _need_bola_open_close(ctx: Ctx) -> None:
    ctx.need(Perm.STAFF_DECLARATOR)
    if not ctx.can(Perm.BOLA_MANAGE):
        ctx.need(Perm.BOLA_OPEN)


@router.post("/staff/declarator/bola/{draw_id}/open")
async def desk_bola_open(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    _need_bola_open_close(ctx)
    draw = bola.open_draw(ctx.db, ctx.user, draw_id)
    return _ok(f"{draw.title}: betting is open.")


@router.post("/staff/declarator/bola/{draw_id}/lock")
async def desk_bola_lock(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    _need_bola_open_close(ctx)
    draw = bola.lock_draw(ctx.db, ctx.user, draw_id)
    return _ok(f"{draw.title}: betting is closed.", "warning")


@router.post("/staff/declarator/bola/{draw_id}/declare")
async def desk_bola_declare(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_DECLARATOR)
    ctx.need(Perm.BOLA_DECLARE)
    data, proof = await _form_with_proof(request)
    if proof is None and proofs.required_for(ctx.user, ctx.store):
        raise ValidationError("Add a picture of the result: your account must show one.")

    def _ball(key: str) -> int:
        try:
            return int(data.get(key) or 0)
        except ValueError as exc:
            raise ValidationError("Enter the two winning numbers.") from exc

    bola_settlement.declare(ctx.db, ctx.user, draw_id=draw_id, ball_first=_ball("ball_first"),
                            ball_second=_ball("ball_second"), settings=ctx.store,
                            reason=data.get("reason") or None)
    if proof is not None:
        proofs.attach(ctx.db, ctx.user, bola.get_draw(ctx.db, draw_id), proof)
    return _ok(f"Draw declared {_ball('ball_first')} / {_ball('ball_second')} and settled.")


# --------------------------------------------------------------------------- teller

@router.get("/staff/teller")
async def staff_teller(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    db, user, store = ctx.db, ctx.user, ctx.store
    is_teller = user.role == Role.TELLER
    accounts = gcash.list_accounts(db, owner_id=user.id if is_teller else None)
    rows = gcash.usage_for(db, accounts, store)
    can_manage = user.role in (Role.ADMIN, Role.TELLER)
    # A Teller and the Admin read full numbers; a Pawn only sees them masked.
    numbers = {a.id: (gcash.number_of(a) if can_manage else gcash.masked(a)) for a in accounts}

    nxt = gcash.peek(db, store)
    live = [u for u in rows if u.account.is_active]
    tellers = []
    if user.role == Role.ADMIN:
        tellers = list(db.scalars(select(User).where(User.role == Role.TELLER, User.status == Status.ACTIVE)
                                  .order_by(User.full_name)).all())
    return staff(
        ctx, "teller", "staff_teller.html",
        rows=rows, numbers=numbers, live_count=len(live),
        pool_scope="mine" if is_teller else "all",
        pool_day_used=sum(u.day_used for u in live), pool_day_room=sum(u.day_left for u in live),
        next_label=(nxt.account.label if nxt else None), next_id=(nxt.account.id if nxt else None),
        can_manage=can_manage, can_reorder=user.role == Role.ADMIN and len(rows) > 1, tellers=tellers,
        recent=gcash.recent_for(db, [a.id for a in accounts]),
        wallet_pending=cashdesk.pending_count(db, user) if ctx.can(Perm.REQUESTS_REVIEW) else 0,
        default_day=int(store.get("gcash.daily_limit_cents")),
        default_month=int(store.get("gcash.monthly_limit_cents")),
        rotate_pct=int(store.get("gcash.rotate_at_pct")), strategy=store.get("gcash.strategy"),
        hold_minutes=int(store.get("gcash.hold_minutes")))


@router.post("/staff/teller/accounts")
async def staff_add(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    data = await form_data(ctx.request)
    owner = int(data["owner_id"]) if data.get("owner_id", "").isdigit() else None
    if ctx.user.role == Role.ADMIN and owner is None:
        raise ValidationError("Choose which Teller this number belongs to.")
    account = gcash.add_account(ctx.db, ctx.user, number=data.get("number", ""), label=data.get("label", ""),
                                owner_id=owner, daily_limit_cents=_limit(data.get("daily")),
                                monthly_limit_cents=_limit(data.get("monthly")), note=data.get("note"))
    return flash_redirect("/staff/teller", f"{account.label} is in the rotation.")


@router.post("/staff/teller/accounts/{account_id}/update")
async def staff_update(account_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    data = await form_data(ctx.request)
    account = gcash.update_account(ctx.db, ctx.user, account_id, label=data.get("label", ""),
                                   note=data.get("note"), daily_limit_cents=_limit(data.get("daily")),
                                   monthly_limit_cents=_limit(data.get("monthly")))
    return flash_redirect("/staff/teller", f"{account.label} saved.")


@router.post("/staff/teller/accounts/{account_id}/pause")
async def staff_pause(account_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    account = gcash.set_active(ctx.db, ctx.user, account_id, False)
    return flash_redirect("/staff/teller", f"{account.label} is paused. New requests skip it.", "warning")


@router.post("/staff/teller/accounts/{account_id}/resume")
async def staff_resume(account_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    account = gcash.set_active(ctx.db, ctx.user, account_id, True)
    return flash_redirect("/staff/teller", f"{account.label} is back in the rotation.")


@router.post("/staff/teller/accounts/{account_id}/remove")
async def staff_remove(account_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    return flash_redirect("/staff/teller", gcash.remove_account(ctx.db, ctx.user, account_id))


@router.post("/staff/teller/accounts/{account_id}/move")
async def staff_move(account_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    if ctx.user.role != Role.ADMIN:
        raise ValidationError("Only the Admin sets the rotation order.")
    data = await form_data(ctx.request)
    gcash.move(ctx.db, ctx.user, account_id, -1 if data.get("dir") == "up" else 1)
    return flash_redirect("/staff/teller")


@router.post("/staff/teller/accounts/{account_id}/adjust")
async def staff_adjust(account_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_TELLER)
    data = await form_data(ctx.request)
    gcash.log_outside_usage(ctx.db, ctx.user, account_id, amount_cents=to_cents(data.get("amount", "0")),
                            note=data.get("note"))
    return flash_redirect("/staff/teller", "Usage added to the tracker.")
