from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals
from app.db.models import EventStatus, PayoutModel, Result
from app.services import betting, events, reports, settlement
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


def _int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid identifier.") from exc


@router.get("/events")
async def events_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_VIEW)
    rows = events.list_events(ctx.db, limit=100)
    return render(ctx, "events.html", active="events", rows=rows,
                  today=dt.date.today().isoformat(),
                  default_fights=int(ctx.store.get("event.default_fight_count")))


@router.post("/events/create")
async def events_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    try:
        event_date = dt.date.fromisoformat(data.get("event_date", ""))
    except ValueError as exc:
        raise ValidationError("Use a valid event date (YYYY-MM-DD).") from exc
    model = PayoutModel(data.get("payout_model", "pool"))
    cut = _int(data.get("house_cut_bps"), -1)
    event = events.create_event(
        ctx.db, ctx.user, title=data.get("title", ""), event_date=event_date, settings=ctx.store,
        fight_count=_int(data.get("fight_count"), 0) or None, payout_model=model,
        house_cut_bps=cut if cut >= 0 else None,
    )
    return flash_redirect(f"/events/{event.id}", f"Event created with {event.fight_count} games.")


@router.post("/events/{event_id}/status")
async def events_status(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    events.set_event_status(ctx.db, ctx.user, event_id, EventStatus(data.get("status", "open")))
    return flash_redirect(f"/events/{event_id}", "Event status updated.")


@router.get("/events/{event_id}")
async def event_detail(event_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_VIEW)
    event = events.get_event(ctx.db, event_id)
    fights = events.fights_of(ctx.db, event_id)
    pools = {fight.id: betting.pool_snapshot(ctx.db, fight) for fight in fights}
    from app.services import players as player_service
    from app.db.models import Station
    from sqlalchemy import select
    hide = hides_totals(ctx.user)
    my_players = [] if hide else player_service.list_players(ctx.db, ctx.user, limit=300)
    stations = [] if hide else list(ctx.db.scalars(select(Station).where(Station.is_active.is_(True))).all())
    show_money = ctx.can(Perm.CASHFLOW_VIEW) and not hide
    return render(ctx, "event_detail.html", active="events", event=event, fights=fights,
                  pools=pools, my_players=my_players, stations=stations, hide_totals=hide,
                  house_cut=events.house_cut_for(ctx.db, event, ctx.store),
                  expenses=events.list_expenses(ctx.db, event.id) if show_money else [],
                  summary=reports.event_summary(ctx.db, event.id) if show_money else None,
                  role_of=events.role_of)


@router.get("/change_name", include_in_schema=False)
async def change_name_legacy(request: Request):
    """Old URL (a leftover of change_name.php). Kept so bookmarks, the PWA shortcut and
    stored notification links still land on the Game View."""
    query = request.url.query
    return RedirectResponse("/games" + (f"?{query}" if query else ""), status_code=308)


@router.get("/games")
async def game_view(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Game View: entry names, declare, recompute, redeclare."""
    ctx.need(Perm.FIGHTS_VIEW)
    params = request.query_params
    event_id = _int(params.get("event_id") or params.get("id"))
    fight_id = _int(params.get("fight_id"))
    number = _int(params.get("gs") or params.get("fight"))

    if not event_id:
        open_events = events.list_events(ctx.db, limit=50)
        return render(ctx, "games.html", active="games", event=None, fight=None,
                      events=open_events, fights=[], pending=settlement.pending_runs(ctx.db)
                      if ctx.user.is_admin else [])

    event = events.get_event(ctx.db, event_id)
    fights = events.fights_of(ctx.db, event_id)
    fight = None
    if fight_id:
        fight = events.get_fight(ctx.db, fight_id)
        if fight.event_id != event.id:
            raise ValidationError("That game does not belong to this event.")
    elif number:
        fight = events.get_fight_by_number(ctx.db, event.id, number)
    elif fights:
        fight = fights[0]

    plan = None
    snapshot = None
    runs = []
    tickets = []
    figures = None
    multipliers = None
    hide = hides_totals(ctx.user)
    if fight is not None:
        runs = [] if hide else settlement.runs_for(ctx.db, fight.id)
        if not hide:
            snapshot = betting.pool_snapshot(ctx.db, fight)
            tickets = betting.list_tickets(ctx.db, ctx.user, fight_id=fight.id, limit=300)
            figures = reports.fight_figures(ctx.db, fight)
            multipliers = betting.side_multipliers(
                snapshot, events.house_cut_for(ctx.db, event, ctx.store), ctx.store)
        if fight.status.value in ("open", "locked") and ctx.user.is_admin:
            plan = settlement.build_plan(ctx.db, fight, Result.MERON, ctx.store).as_dict()
    return render(ctx, "games.html", active="games", event=event, fight=fight,
                  fights=fights, events=[], snapshot=snapshot, runs=runs, tickets=tickets,
                  preview=plan, figures=figures, multipliers=multipliers, hide_totals=hide,
                  role_of=events.role_of, claim_status=betting.claim_status,
                  pending=settlement.pending_runs(ctx.db) if ctx.user.is_admin else [])


@router.post("/games/rename")
async def rename(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_RENAME)
    data = await form_data(request)
    fight = events.rename_entries(ctx.db, ctx.user, _int(data.get("fight_id")),
                                  meron=data.get("meron", ""), wala=data.get("wala", ""))
    return flash_redirect(f"/games?event_id={fight.event_id}&fight_id={fight.id}", "Entry names updated.")


@router.post("/games/{fight_id}/lock")
async def lock(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_LOCK)
    data = await form_data(request)
    fight = events.lock_fight(ctx.db, ctx.user, fight_id, locked=data.get("locked", "1") == "1")
    return flash_redirect(f"/games?event_id={fight.event_id}&fight_id={fight.id}",
                          "Betting closed for this game." if data.get("locked", "1") == "1" else "Betting re-opened.")


@router.post("/games/{fight_id}/odds")
async def odds(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    fight = events.set_fixed_odds(ctx.db, ctx.user, fight_id,
                                  meron_bps=_int(data.get("meron_bps")), wala_bps=_int(data.get("wala_bps")))
    return flash_redirect(f"/games?event_id={fight.event_id}&fight_id={fight.id}", "Odds updated.")


@router.post("/games/{fight_id}/betmode")
async def betmode(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    fight = events.set_bet_mode(ctx.db, ctx.user, fight_id, data.get("mode", "all"))
    text = {"all": "All sides open.", "meron": "Only MERON is open.", "wala": "Only WALA is open."}[fight.bet_mode]
    return flash_redirect(f"/games?event_id={fight.event_id}&fight_id={fight.id}", text)


@router.post("/games/{fight_id}/llamado")
async def llamado(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    fight = events.set_llamado(ctx.db, ctx.user, fight_id, data.get("side"))
    return flash_redirect(f"/games?event_id={fight.event_id}&fight_id={fight.id}",
                          f"Llamado set to {fight.llamado_side.upper()}." if fight.llamado_side
                          else "Llamado cleared.")


@router.post("/events/{event_id}/cancel")
async def event_cancel(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    result = events.cancel_event(ctx.db, ctx.user, event_id, reason=data.get("reason", ""),
                                 settings=ctx.store)
    return flash_redirect(f"/events/{event_id}",
                          f"Event cancelled. {result['refunded']} game(s) refunded, "
                          f"{result['voided']} empty game(s) voided, {result['skipped']} already settled.")


@router.post("/events/{event_id}/expenses")
async def expense_add(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    events.add_expense(ctx.db, ctx.user, event_id, label=data.get("label", ""),
                       amount_cents=to_cents(data.get("amount", "0")), note=data.get("note"))
    return flash_redirect(f"/events/{event_id}", "Expense recorded.")


@router.post("/expenses/{expense_id}/delete")
async def expense_delete(expense_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    row = events.delete_expense(ctx.db, ctx.user, expense_id)
    return flash_redirect(f"/events/{row.event_id}", "Expense removed.")


@router.post("/games/{fight_id}/declare")
async def declare(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_DECLARE)
    data = await form_data(request)
    fight = events.get_fight(ctx.db, fight_id)
    result = events.resolve_result(fight, data.get("result", ""))
    run = settlement.declare(ctx.db, ctx.user, fight_id=fight_id, result=result, settings=ctx.store,
                             reason=data.get("reason"))
    where = f"/games?event_id={fight.event_id}&fight_id={fight_id}"
    if (run.totals or {}).get("auto_cancel"):
        floor = int(ctx.store.get("payout.min_odds_bps")) / 10000
        return flash_redirect(where, f"Game {fight.number}: odds were below {floor:.2f}x, so it was "
                              "cancelled and every stake refunded.", "warning")
    if hides_totals(ctx.user):
        return flash_redirect(where, f"Game {fight.number} declared {result.value} and settled.")
    label = events.role_of(fight, result.value)
    return flash_redirect(where, f"Game {fight.number} declared {result.value}"
                          f"{' (' + label + ')' if label else ''} and settled.")


@router.post("/games/{fight_id}/correct")
async def correct(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_REDECLARE)
    data = await form_data(request)
    action = data.get("action", "redeclare")
    result = Result(data["result"]) if data.get("result") else Result.MERON
    fight = events.get_fight(ctx.db, fight_id)
    run = settlement.request_correction(
        ctx.db, ctx.user, fight_id=fight_id, result=result, reason=data.get("reason", ""),
        settings=ctx.store, action=action,
    )
    message = ("Correction applied." if run.status.value == "applied"
               else "Correction queued — a second admin must approve it.")
    return flash_redirect(f"/games?event_id={fight.event_id}&fight_id={fight_id}", message)


@router.post("/settlement/{run_id}/approve")
async def approve_run(run_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_APPROVE)
    run = settlement.approve(ctx.db, ctx.user, run_id=run_id, settings=ctx.store)
    return flash_redirect(f"/games?event_id={run.fight.event_id}&fight_id={run.fight_id}",
                          "Correction approved and applied.")


@router.post("/settlement/{run_id}/reject")
async def reject_run(run_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_APPROVE)
    data = await form_data(request)
    run = settlement.reject(ctx.db, ctx.user, run_id=run_id, note=data.get("note", "no reason given"))
    return flash_redirect(f"/games?event_id={run.fight.event_id}&fight_id={run.fight_id}",
                          "Correction rejected.")
