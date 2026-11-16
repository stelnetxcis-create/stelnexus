"""Bola (Tumbok-Sahod) web routes: lobby, draw page with bet slip, tickets, results, operations.

Everything here is a thin layer over ``services/bola.py`` and ``services/bola_settlement.py``.
The whole feature stays dark until the ``bola.enabled`` setting is switched on by the Admin.
"""
from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from app.core.config import get_settings
from app.core.errors import Forbidden, NotFound, StelError, ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals
from app.db.models import BolaKind, TicketStatus
from app.services import bola, bola_settlement, locations, players, proofs, reports
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()
_cfg = get_settings()


async def _form_with_proof(request: Request) -> tuple[dict[str, str], proofs.Proof | None]:
    """A multipart form: its text fields plus the validated result picture (None if none sent)."""
    form = await request.form()
    data = {key: value for key, value in form.items() if isinstance(value, str)}
    upload = form.get("proof")
    raw = None
    if upload is not None and not isinstance(upload, str) and getattr(upload, "filename", ""):
        raw = await upload.read(_cfg.MAX_UPLOAD_BYTES + 1)
    return data, proofs.check_upload(raw)

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
    can_manage = ctx.can(Perm.BOLA_MANAGE)
    expenses = None
    if can_manage or (ctx.can(Perm.BOLA_EXPOSURE) and not hide):
        expenses = bola.list_expenses(ctx.db, draw.id)
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
        runs=bola_settlement.runs_for(ctx.db, draw.id), expenses=expenses,
        can_manage=can_manage, can_declare=ctx.can(Perm.BOLA_DECLARE),
        can_redeclare=ctx.can(Perm.BOLA_REDECLARE), can_approve=ctx.can(Perm.BOLA_APPROVE),
        can_place=ctx.can(Perm.BOLA_PLACE), can_cancel=ctx.can(Perm.BOLA_CANCEL),
        can_report=ctx.can(Perm.BOLA_EXPOSURE) and not hide,
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
    return render(ctx, "bola_betlogs.html", active="bola_logs", rows=rows,
                  filters={"q": p.get("q", ""), "status": status or "", "draw_id": p.get("draw_id", "")})


@router.get("/bola/cancelled")
async def bola_cancelled(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Every cancelled ticket in the viewer's network: who cancelled it, when and why."""
    _need_view(ctx)
    if hides_totals(ctx.user):
        raise Forbidden("Your role cannot see tickets.")
    from sqlalchemy import select
    from app.db.models import User
    rows = bola.list_tickets(ctx.db, ctx.user, canceled_only=True,
                             search=request.query_params.get("q", "")[:40] or None, limit=300)
    ids = {t.canceled_by_id for t in rows if t.canceled_by_id}
    users = {u.id: u for u in ctx.db.scalars(select(User).where(User.id.in_(ids))).all()} if ids else {}
    names = {i: ("System" if u.is_hidden else u.username) for i, u in users.items()}
    return render(ctx, "bola_cancelled.html", active="bola_cancelled", rows=rows, names=names,
                  q=request.query_params.get("q", ""), local=bola.local)


# ------------------------------------------------------------------ agent statements
def _statement_range(p) -> tuple[dt.date, dt.date]:
    """?start=&end= as Manila dates; both default to today; at most a year; start <= end."""
    today = dt.datetime.now(dt.timezone.utc).astimezone(bola.MANILA).date()

    def one(key: str) -> dt.date:
        try:
            return dt.date.fromisoformat(p[key]) if p.get(key) else today
        except ValueError:
            return today
    start, end = one("start"), one("end")
    if start > end:
        start, end = end, start
    return max(start, end - dt.timedelta(days=366)), end


def _need_statement(ctx: Ctx) -> None:
    _need_view(ctx)
    if hides_totals(ctx.user):
        raise Forbidden("Your role cannot see totals.")


@router.get("/bola/agents")
async def bola_agent_statements(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_statement(ctx)
    start, end = _statement_range(request.query_params)
    return render(ctx, "bola_agents.html", active="bola_agents",
                  stmt=reports.bola_agent_statement(ctx.db, ctx.user, start=start, end=end))


@router.get("/bola/agents.csv")
async def bola_agent_statements_csv(request: Request, ctx: Ctx = Depends(require_ctx)):
    from app.web.routes_extra import _safe
    _need_statement(ctx)
    start, end = _statement_range(request.query_params)
    stmt = reports.bola_agent_statement(ctx.db, ctx.user, start=start, end=end)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Bola agent statement", start.isoformat(), end.isoformat()])
    writer.writerow(["Agent", "Role", "Tickets", "Staked", "Cancelled", "Cancelled amount",
                     "Players won", "Paid to players", "Commission", "Unclaimed", "Claimed"])
    for r in stmt["rows"]:
        writer.writerow([_safe(r["name"]), r["role"], r["tickets"], f"{r['staked'] / 100:.2f}",
                         r["canceled"], f"{r['canceled_cents'] / 100:.2f}", r["winners"],
                         f"{r['paid'] / 100:.2f}", f"{r['commission'] / 100:.2f}",
                         f"{r['unclaimed'] / 100:.2f}", f"{r['claimed'] / 100:.2f}"])
    t = stmt["totals"]
    writer.writerow(["Total", "", t["tickets"], f"{t['staked'] / 100:.2f}", t["canceled"],
                     f"{t['canceled_cents'] / 100:.2f}", t["winners"], f"{t['paid'] / 100:.2f}",
                     f"{t['commission'] / 100:.2f}", f"{t['unclaimed'] / 100:.2f}",
                     f"{t['claimed'] / 100:.2f}"])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="bola-agents-{start}-{end}.csv"',
        "Cache-Control": "no-store"})


@router.get("/bola/agents/{agent_id}")
async def bola_agent_statement_page(agent_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    from app.core.rbac import assert_user_in_scope
    _need_statement(ctx)
    agent = assert_user_in_scope(ctx.db, ctx.user, agent_id)
    start, end = _statement_range(request.query_params)
    stmt = reports.bola_agent_statement(ctx.db, ctx.user, start=start, end=end, agent_id=agent_id)
    return render(ctx, "bola_agent.html", active="bola_agents", agent=agent, stmt=stmt,
                  draws=reports.bola_agent_draws(ctx.db, ctx.user, agent_id, start=start, end=end),
                  local=bola.local)


# ------------------------------------------------------------------ reports
def _need_exposure(ctx: Ctx) -> None:
    """Money figures: Master/Admin (BOLA_EXPOSURE), never a Declarator."""
    ctx.need(Perm.BOLA_EXPOSURE)
    if hides_totals(ctx.user):
        raise Forbidden("Your role cannot see totals.")


@router.get("/bola/reports")
async def bola_reports_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_exposure(ctx)
    p = request.query_params
    today = dt.datetime.now(dt.timezone.utc).astimezone(bola.MANILA).date()
    try:
        day = dt.date.fromisoformat(p["date"]) if p.get("date") else today
    except ValueError:
        day = today
    try:
        year, month = (int(x) for x in (p.get("month") or today.strftime("%Y-%m")).split("-"))
        if not (1 <= month <= 12 and 2000 <= year <= 2100):
            raise ValueError
    except (ValueError, TypeError):
        year, month = today.year, today.month
    return render(ctx, "bola_reports.html", active="bola_reports",
                  daily=reports.bola_daily(ctx.db, day),
                  monthly=reports.bola_monthly(ctx.db, year, month),
                  local=bola.local)


@router.get("/bola/draws/{draw_id}/report.csv")
async def bola_draw_report_csv(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    from app.web.routes_extra import _safe
    _need_exposure(ctx)
    draw = bola.get_draw(ctx.db, draw_id)
    fig = reports.bola_draw_report(ctx.db, draw, ctx.store)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Draw", draw.id, "Area", _safe(draw.location.name), "Date", draw.draw_date.isoformat(),
                     "Slot", draw.slot, "Status", draw.status.value])
    writer.writerow(["Result", draw.result_label or "", "Scheduled (Manila)",
                     bola.local(draw.scheduled_at).strftime("%Y-%m-%d %H:%M"),
                     "Cut-off", bola.local(draw.cutoff_at).strftime("%H:%M"),
                     "Declared", bola.local(draw.declared_at).strftime("%Y-%m-%d %H:%M") if draw.declared_at else ""])
    writer.writerow([])
    writer.writerow(["Code", "Type", "Numbers", "Lines", "Tumbok/line", "Sahod/line", "Stake",
                     "Payout", "Status", "Kubrador", "Cavo", "Player", "Note"])
    for t in bola.list_tickets(ctx.db, ctx.user, draw_id=draw.id, limit=1000):
        writer.writerow([t.code, t.kind.value, " ".join(str(n) for n in (t.numbers or [])),
                         t.line_count, f"{t.tumbok_cents / 100:.2f}", f"{t.sahod_cents / 100:.2f}",
                         f"{t.stake_cents / 100:.2f}", f"{t.payout_cents / 100:.2f}", t.status.value,
                         _safe(t.kubrador or ""), _safe(t.cavo or ""),
                         _safe(t.player.full_name if t.player else ""), _safe(t.note or "")])
    writer.writerow([])
    for label, key in (("Tickets", "tickets"), ("Winners", "winners"), ("Total staked", "staked"),
                       ("Settled stake", "settled_stake"), ("Paid out", "paid"), ("House take", "house"),
                       ("Commissions", "commission"), ("Expenses", "expenses"), ("Net income", "net")):
        writer.writerow([label, fig[key] if key in ("tickets", "winners") else f"{fig[key] / 100:.2f}"])
    for tier, amount in sorted(fig["by_tier"].items()):
        writer.writerow(["Commission tier", tier, f"{amount / 100:.2f}"])
    for expense in fig["expense_rows"]:
        writer.writerow(["Expense", _safe(expense.label), f"{expense.amount_cents / 100:.2f}",
                         _safe(expense.note or "")])
    worst = fig["worst"]
    if worst["liability_cents"]:
        writer.writerow(["Worst outcome at close", f"{worst['first']}-{worst['second']}",
                         f"{worst['liability_cents'] / 100:.2f}"])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="bola-draw-{draw.id}-report.csv"',
        "Cache-Control": "no-store"})


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


# ------------------------------------------------------------------ per-draw expenses
@router.post("/bola/draws/{draw_id}/expenses")
async def bola_expense_add(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    data = await form_data(request)
    bola.add_expense(ctx.db, ctx.user, draw_id, label=data.get("label", ""),
                     amount_cents=to_cents(data.get("amount", "0") or "0"),
                     note=data.get("note") or None)
    return flash_redirect(f"/bola/draws/{draw_id}", "Expense recorded.")


@router.post("/bola/expenses/{expense_id}/delete")
async def bola_expense_delete(expense_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    row = bola.delete_expense(ctx.db, ctx.user, expense_id)
    return flash_redirect(f"/bola/draws/{row.draw_id}", "Expense removed.")


@router.post("/bola/draws/{draw_id}/declare")
async def bola_declare(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_DECLARE)
    data, proof = await _form_with_proof(request)
    if proof is None and proofs.required_for(ctx.user, ctx.store):
        raise ValidationError("Attach a picture of the result: your account must show one.")
    bola_settlement.declare(ctx.db, ctx.user, draw_id=draw_id, ball_first=_int(data.get("ball_first")),
                            ball_second=_int(data.get("ball_second")), settings=ctx.store,
                            reason=data.get("reason") or None)
    if proof is not None:
        proofs.attach(ctx.db, ctx.user, bola.get_draw(ctx.db, draw_id), proof)
    return flash_redirect(f"/bola/draws/{draw_id}", "Result declared and settled.")


@router.get("/bola/draws/{draw_id}/proof")
async def bola_proof_image(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    """The picture attached to a draw's result. Signed-in staff who may see draws only."""
    _need_view(ctx)
    draw = bola.get_draw(ctx.db, draw_id)
    path = proofs.path_of(draw)
    if path is None:
        raise NotFound("This draw has no result picture.")
    return FileResponse(path, media_type=draw.proof_mime or "application/octet-stream")


@router.post("/bola/draws/{draw_id}/proof")
async def bola_proof_attach(draw_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Add or replace the result picture on a draw (whoever may declare or correct results)."""
    if not (ctx.can(Perm.BOLA_DECLARE) or ctx.can(Perm.BOLA_REDECLARE)):
        raise Forbidden("Your role is not allowed to perform this action.")
    _, proof = await _form_with_proof(request)
    if proof is None:
        raise ValidationError("Choose a picture to upload.")
    draw = bola.get_draw(ctx.db, draw_id)
    proofs.attach(ctx.db, ctx.user, draw, proof)
    return flash_redirect(f"/bola/draws/{draw.id}", "Result picture saved.")


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
