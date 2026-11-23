from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.core.errors import Forbidden
from app.core.money import to_cents
from app.core.rbac import Perm, assert_user_in_scope, hides_totals
from app.db.models import CommissionStatus, RequestDirection
from app.services import bola, cashdesk, commissions, reports, snapshots
from app.web.paging import paginate
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


TIER_LABEL = {"master": "Master Agent", "agent": "Agent", "sub_agent": "Sub Agent"}


def _range(params) -> tuple[dt.date, dt.date]:
    """?start=&end= as Manila dates. Nothing given = the last 7 days; an unreadable date = today;
    start > end is swapped; at most a year."""
    today = dt.datetime.now(dt.timezone.utc).astimezone(bola.MANILA).date()
    if not params.get("start") and not params.get("end"):
        return today - dt.timedelta(days=6), today

    def one(key: str) -> dt.date:
        try:
            return dt.date.fromisoformat(params[key]) if params.get(key) else today
        except ValueError:
            return today
    start, end = one("start"), one("end")
    if start > end:
        start, end = end, start
    return max(start, end - dt.timedelta(days=366)), end


def _need_statement(ctx: Ctx) -> None:
    ctx.need(Perm.COMMISSIONS_VIEW)
    if hides_totals(ctx.user):
        raise Forbidden("Your role cannot see totals.")


def _game(params) -> str:
    return params.get("game") if params.get("game") in ("sabong", "bola") else "all"


def _status(params):
    value = params.get("status")
    return CommissionStatus(value) if value in ("unclaimed", "claimed", "reversed") else None


@router.get("/commissions")
async def commissions_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Statements and commission in one place: the account's own money, a statement row for every
    account in its network (both games), and every commission line in that network."""
    _need_statement(ctx)
    if request.url.path == "/commissions":          # moved: Records > Commissions (the account page stays)
        from app.web.routes_records import legacy_redirect
        return legacy_redirect(request, "commissions")
    params = request.query_params
    start, end = _range(params)
    game, wanted = _game(params), _status(params)
    bola_on = ctx.can(Perm.BOLA_VIEW)
    if not bola_on and game == "bola":
        game = "all"
    stmt = reports.agent_statement(ctx.db, ctx.user, start=start, end=end,
                                   game="sabong" if (not bola_on and game == "all") else game)
    pager, lines = paginate(request, 300, lambda limit, offset: commissions.list_scope(
        ctx.db, ctx.user, game="sabong" if (not bola_on and game == "all") else game,
        status=wanted, start=start, end=end, limit=limit, offset=offset))
    store = ctx.store
    bar: dict = {}
    if request.url.path == "/records":         # shown as the Commissions tab of Records
        from app import games as _games
        from app.games import TAB_LABELS
        from app.web.routes_records import allowed_tabs
        bar = dict(records_bar=True, tab="commissions", tabs=allowed_tabs(ctx), tab_labels=TAB_LABELS,
                   game_choices=_games.visible_games(ctx),
                   show_game=False)
    return render(ctx, "commissions.html", active="records" if bar else "commissions", stmt=stmt, lines=lines, pager=pager, **bar,
                  game=game, status=params.get("status") or "", bola_on=bola_on,
                  totals=commissions.totals_for(ctx.db, ctx.user), tier_label=TIER_LABEL,
                  defaults={g: {"volume": int(store.get(k[0])), "sop": int(store.get(k[1])),
                                "pool": int(store.get(pk)), "basis": store.get(bk)}
                            for g, k, pk, bk in (
                                ("sabong", ("commission.cavo_cut_bps", "commission.kubrador_cut_bps"),
                                 "commission.pool_bps", "commission.basis"),
                                ("bola", ("bola.commission.agent_cut_bps", "bola.commission.sub_agent_cut_bps"),
                                 "bola.commission.pool_bps", "bola.commission.basis"))})


@router.get("/commissions.csv")
async def commissions_csv(request: Request, ctx: Ctx = Depends(require_ctx)):
    from app.web.routes_extra import _safe
    _need_statement(ctx)
    start, end = _range(request.query_params)
    game = _game(request.query_params)
    stmt = reports.agent_statement(ctx.db, ctx.user, start=start, end=end, game=game)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Agent statement", game, start.isoformat(), end.isoformat()])
    writer.writerow(["Account", "Role", "Upline", "Tickets", "Staked", "Cancelled", "Cancelled amount",
                     "Players won", "Paid to players", "Meron/Wala commission", "Bola commission",
                     "Commission", "Unclaimed", "Claimed"])
    for r in stmt["rows"]:
        writer.writerow([_safe(r["name"]), r["role"], _safe(r["upline"] or ""), r["tickets"],
                         f"{r['staked'] / 100:.2f}", r["canceled"], f"{r['canceled_cents'] / 100:.2f}",
                         r["winners"], f"{r['paid'] / 100:.2f}", f"{r['commission_sabong'] / 100:.2f}",
                         f"{r['commission_bola'] / 100:.2f}", f"{r['commission'] / 100:.2f}",
                         f"{r['unclaimed'] / 100:.2f}", f"{r['claimed'] / 100:.2f}"])
    t = stmt["totals"]
    writer.writerow(["Total", "", "", t["tickets"], f"{t['staked'] / 100:.2f}", t["canceled"],
                     f"{t['canceled_cents'] / 100:.2f}", t["winners"], f"{t['paid'] / 100:.2f}",
                     f"{t['commission_sabong'] / 100:.2f}", f"{t['commission_bola'] / 100:.2f}",
                     f"{t['commission'] / 100:.2f}", f"{t['unclaimed'] / 100:.2f}",
                     f"{t['claimed'] / 100:.2f}"])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="statements-{start}-{end}.csv"',
        "Cache-Control": "no-store"})


@router.get("/commissions/{agent_id}")
async def commissions_account_page(agent_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """One account's statement: totals, every game it sold or earned on, and its commission lines."""
    _need_statement(ctx)
    agent = assert_user_in_scope(ctx.db, ctx.user, agent_id)
    params = request.query_params
    start, end = _range(params)
    game = _game(params)
    stmt = reports.agent_statement(ctx.db, ctx.user, start=start, end=end, game=game, agent_id=agent_id)
    pager, lines = paginate(request, 300, lambda limit, offset: commissions.list_scope(
        ctx.db, ctx.user, game=game, agent_id=agent_id, status=_status(params), start=start, end=end,
        limit=limit, offset=offset))
    return render(ctx, "commission_account.html", active="commissions", agent=agent, stmt=stmt, game=game,
                  games=reports.agent_games(ctx.db, ctx.user, agent_id, start=start, end=end, game=game),
                  lines=lines, pager=pager,
                  tier_label=TIER_LABEL, status=params.get("status") or "")


@router.post("/commissions/convert")
async def commissions_convert(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.COMMISSIONS_CLAIM)
    data = await form_data(request)
    amount = to_cents(data["amount"]) if data.get("amount") else None
    commissions.claim_to_wallet(ctx.db, ctx.user, amount_cents=amount,
                                idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect("/commissions", "Commission converted to credit.")


@router.post("/commissions/withdraw")
async def commissions_withdraw(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.COMMISSIONS_CLAIM)
    data = await form_data(request)
    cashdesk.create_request(
        ctx.db, ctx.user, direction=RequestDirection.OUT,
        amount_cents=to_cents(data.get("amount", "0")), settings=ctx.store,
        agent_id=ctx.user.id, method=data.get("method", "gcash"),
        reference=data.get("reference"), idempotency_key=data.get("idempotency_key") or None,
    )
    return flash_redirect("/commissions", "Withdrawal requested. An admin will review it.")


@router.get("/cashflow")
async def cashflow_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.CASHFLOW_VIEW)
    flows = snapshots.flows(ctx.db)             # history-scanning totals: precomputed, "as of" shown
    integ = snapshots.integrity(ctx.db) if ctx.can(Perm.HIDDEN_VIEW) else None
    return render(ctx, "cashflow.html", active="cashflow",
                  figures=reports.cashflow(ctx.db, flows.payload),
                  series=reports.daily_series(ctx.db, 14),
                  integrity=integ.payload if integ else None,
                  flows_at=flows.computed_at, integrity_at=integ.computed_at if integ else None,
                  can_refresh=ctx.can(Perm.CASHFLOW_VIEW))


@router.post("/cashflow/refresh")
async def cashflow_refresh(ctx: Ctx = Depends(require_ctx)):
    """Recompute the precomputed figures now (the integrity scan only for those who may see it)."""
    ctx.need(Perm.CASHFLOW_VIEW)
    only = ["flows", "top"] + (["integrity"] if ctx.can(Perm.HIDDEN_VIEW) else [])
    snapshots.refresh_all(ctx.db, only)
    return flash_redirect("/cashflow", "Figures refreshed.")
