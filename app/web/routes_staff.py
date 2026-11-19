"""The Staff Panel: one section per staff role, below the Admin Panel.

    /staff              overview (every section; only the ones you may open are clickable)
    /staff/teller       the GCash pool: numbers, limit tracker, rotation (Teller, Admin; Pawn read-only)
    /staff/declarator   placeholder for now

A Teller manages only its own numbers and sees only those. The Admin manages every number and sees
the whole pool. A Pawn the Admin switched it on for sees the whole pool but can change nothing.
Master has no access.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm
from app.db.models import Role, Status, User
from app.services import cashdesk, gcash
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx
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
    sections = [
        dict(label="Teller", url="/staff/teller", open=teller_open, value=teller_value,
             hint=teller_hint, tone="green", locked_hint="Tellers only"),
        dict(label="Declarator", url="/staff/declarator", open=decl_open, value="Open",
             hint="Coming soon", tone="", locked_hint="Declarators only"),
    ]
    return staff(ctx, "overview", "staff_home.html", sections=sections)


@router.get("/staff/declarator")
async def staff_declarator(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STAFF_DECLARATOR)
    return staff(ctx, "declarator", "staff_declarator.html")


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
