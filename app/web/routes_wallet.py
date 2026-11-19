"""The wallet hub: balance, load, cash out, transfer, money history and (for reviewers) approvals.

Everything about *your own money* lives here, opened from the balance in the top bar, instead of
being scattered over the side menu. Money only moves in three ways:
  load      a GCash / bank request with a reference number, approved by the Admin (or a Master)
  transfer  credit sent to another account, instantly, from your own balance
  cash out  an approved request; the balance is debited when it is approved
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.core.errors import ValidationError
from app.core.money import fmt, to_cents
from app.core.rbac import Perm
from app.db.models import GcashAccount, RequestDirection
from app.services import agents, cashdesk, gcash, players, wallet
from app.web.deps import Ctx, audit_action, flash_redirect, form_data, render, require_ctx

router = APIRouter()

PAGE = 25


def _tabs(ctx: Ctx) -> list[tuple[str, str]]:
    tabs = [("overview", "Overview")]
    if ctx.can(Perm.REQUESTS_CREATE):
        tabs += [("load", "Load"), ("cashout", "Cash out")]
    if ctx.can(Perm.WALLET_TRANSFER):
        tabs.append(("transfer", "Transfer"))
    tabs.append(("history", "History"))
    if ctx.can(Perm.REQUESTS_REVIEW):
        tabs.append(("approvals", "Approvals"))
    return tabs


def _int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _gc_numbers(db, rows, *, only_pending_loads: bool) -> dict:
    """account id -> full number, for the requests on screen. A requester only needs the number of a
    pending load (that is where they send the money); reviewers see it on every row."""
    ids = {r.gcash_account_id for r in rows if r.gcash_account_id and (
        not only_pending_loads or (r.direction.value == "in" and r.status.value == "pending"))}
    return {a.id: gcash.number_of(a) for a in db.scalars(
        select(GcashAccount).where(GcashAccount.id.in_(ids))).all()} if ids else {}


@router.get("/wallet")
async def wallet_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.WALLET_VIEW)
    q = request.query_params
    tabs = _tabs(ctx)
    tab = q.get("t", "overview")
    if tab not in {key for key, _ in tabs}:
        tab = "overview"
    period = q.get("period", "all")
    if period not in wallet.PERIODS:
        period = "all"
    flow = q.get("flow", "all")
    if flow not in wallet.FLOWS:
        flow = "all"
    page = max(_int(q.get("page"), 0), 0)
    user, db = ctx.user, ctx.db

    data: dict = {
        "balance": agents.wallet_balance(db, user),
        "commission": agents.commission_balance(db, user),
        "tabs": tabs, "tab": tab, "period": period, "flow": flow, "page": page,
        "withdrawals_on": bool(ctx.store.get("wallet.withdrawals_enabled")),
        "min_cash": int(ctx.store.get("wallet.min_cash_cents")),
        "max_cash": int(ctx.store.get("wallet.max_cash_cents")),
        "min_transfer": int(ctx.store.get("wallet.min_transfer_cents")),
        "max_transfer": int(ctx.store.get("wallet.max_transfer_cents")),
        "transfer_scope": ctx.store.get("wallet.transfer_scope"),
    }
    if tab == "overview":
        data["summary"] = wallet.summary(db, user, period=period)
        data["recent"] = wallet.history(db, user, limit=6)
        data["my_requests"] = cashdesk.my_requests(db, user, limit=6)
        data["gc_nums"] = _gc_numbers(db, data["my_requests"], only_pending_loads=True)
    elif tab in ("load", "cashout"):
        data["my_players"] = players.list_players(db, user, limit=300)
        send_to = gcash.peek(db, ctx.store)                  # pooled GCash: the number a load would go to
        data["send_to"] = ({"label": send_to.account.label, "number": gcash.number_of(send_to.account)}
                           if send_to else None)
        data["pool_on"] = gcash.pool_exists(db)
        data["tellers"] = []
        if not data["pool_on"]:                              # no pool yet: the old per-teller list
            from app.core.security import pii as _pii
            from app.db.models import Role as _Role, Status as _Status, User as _User
            data["tellers"] = [(t.full_name, _pii.decrypt(t.gcash_enc)) for t in db.scalars(
                select(_User).where(_User.role == _Role.TELLER, _User.status == _Status.ACTIVE,
                                    _User.gcash_enc.is_not(None)).order_by(_User.full_name)).all()]
        data["my_requests"] = [r for r in cashdesk.my_requests(db, user, limit=30)
                               if r.direction.value == ("in" if tab == "load" else "out")][:8]
        data["gc_nums"] = _gc_numbers(db, data["my_requests"], only_pending_loads=True)
    elif tab == "transfer":
        data["recipients"] = wallet.recipients(db, user, ctx.store)
        data["recent_transfers"] = [r for r in wallet.history(db, user, limit=60)
                                    if r.label.startswith("Transfer")][:8]
    elif tab == "history":
        data["summary"] = wallet.summary(db, user, period=period)
        rows = wallet.history(db, user, flow=flow, period=period, limit=PAGE + 1, offset=page * PAGE)
        data["rows"], data["more"] = rows[:PAGE], len(rows) > PAGE
    elif tab == "approvals":
        view = q.get("v", "in")
        view = view if view in ("in", "out", "hist") else "in"
        data["view"] = view
        data["queue"] = cashdesk.list_requests(db, user, view=view)
        data["gc_nums"] = _gc_numbers(db, data["queue"], only_pending_loads=False)
    return render(ctx, "wallet.html", active="wallet", **data)


@router.post("/wallet/request")
async def wallet_request(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.REQUESTS_CREATE)
    data = await form_data(request)
    try:
        direction = RequestDirection(data.get("direction", "in"))
    except ValueError as exc:
        raise ValidationError("Choose load or cash out.") from exc
    subject = data.get("subject", "me")
    common = dict(direction=direction, amount_cents=to_cents(data.get("amount", "0")),
                  settings=ctx.store, method=data.get("method", "gcash"),
                  reference=data.get("reference"), idempotency_key=data.get("idempotency_key") or None)
    if subject.startswith("player:"):
        req = cashdesk.create_request(ctx.db, ctx.user, player_id=_int(subject.split(":", 1)[1]), **common)
    else:
        req = cashdesk.create_request(ctx.db, ctx.user, agent_id=ctx.user.id, target="wallet", **common)
    tab = "load" if direction == RequestDirection.IN else "cashout"
    message = "Request sent. Your balance changes when it is approved."
    if direction == RequestDirection.IN and req.gcash_account is not None:
        message = (f"Request sent. Send {fmt(req.amount_cents)} to GCash {gcash.number_of(req.gcash_account)} "
                   f"({req.gcash_account.label}). Your balance changes when it is approved.")
    return flash_redirect(f"/wallet?t={tab}", message)


@router.post("/wallet/transfer")
async def wallet_transfer(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.WALLET_TRANSFER)
    data = await form_data(request)
    amount = to_cents(data.get("amount", "0"))
    txn = wallet.transfer(ctx.db, ctx.user, amount_cents=amount, settings=ctx.store,
                          target_id=_int(data.get("target_id")) or None,
                          username=data.get("username"), note=data.get("note"),
                          idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect("/wallet?t=transfer", f"Sent. Reference {txn.uuid[:8].upper()}.")


@router.post("/wallet/{request_id}/review")
async def wallet_review(request_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.REQUESTS_REVIEW)
    data = await form_data(request)
    approved = data.get("decision") == "approve"
    cashdesk.review(ctx.db, ctx.user, request_id=request_id, approve=approved,
                    settings=ctx.store, note=data.get("note"))
    return flash_redirect("/wallet?t=approvals&v=hist", "Request approved." if approved else "Request rejected.")


@router.post("/wallet/withdrawals")
async def withdrawals_switch(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Emergency stop: block every cash-out / withdrawal (and approvals of them)."""
    ctx.need(Perm.WITHDRAWALS_SWITCH)
    data = await form_data(request)
    enabled = data.get("enabled", "1") == "1"
    old, new = ctx.store.set("wallet.withdrawals_enabled", "true" if enabled else "false", ctx.user.id)
    audit_action(ctx, "settings.update", entity_type="setting", entity_id="wallet.withdrawals_enabled",
                 before={"value": old}, after={"value": new}, reason="emergency switch")
    return flash_redirect("/wallet?t=approvals", "Withdrawals are ON again." if enabled
                          else "Withdrawals are OFF. No cash-out can be requested or approved.",
                          "success" if enabled else "warning")


# --- the old Cash In/Out address: kept so bookmarks and notifications still land somewhere ---------

@router.get("/cashin_out", include_in_schema=False)
async def legacy_cashin_out(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.WALLET_VIEW)
    view = request.query_params.get("t", "in")
    if ctx.can(Perm.REQUESTS_REVIEW):
        return flash_redirect(f"/wallet?t=approvals&v={view if view in ('in', 'out', 'hist') else 'in'}")
    return flash_redirect("/wallet")
