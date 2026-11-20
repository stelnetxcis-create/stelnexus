from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.core.errors import Forbidden, NotFound, ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals
from app.db.models import EventStatus, PayoutModel, Result
from app.services import betting, events, locations, proofs, reports, settlement
from app.services import players as player_service
from app.services import store as store_svc
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

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
                  stats=events.lobby_stats(ctx.db, [e.id for e in rows]),
                  today=dt.date.today().isoformat(),
                  default_fights=int(ctx.store.get("event.default_fight_count")),
                  locations=locations.list_locations(ctx.db, active_only=True),
                  require_location=bool(ctx.store.get("event.require_location")))


@router.post("/events/create")
async def events_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_CREATE)
    data = await form_data(request)
    try:
        event_date = dt.date.fromisoformat(data.get("event_date", ""))
    except ValueError as exc:
        raise ValidationError("Use a valid event date (YYYY-MM-DD).") from exc
    raw_model = (data.get("payout_model") or "").strip()
    model = PayoutModel(raw_model) if raw_model else None      # blank = the location's / global default
    cut = _int(data.get("house_cut_bps"), -1)
    event = events.create_event(
        ctx.db, ctx.user, title=data.get("title", ""), event_date=event_date, settings=ctx.store,
        fight_count=_int(data.get("fight_count"), 0) or None, payout_model=model,
        house_cut_bps=cut if cut >= 0 else None,
        location_id=_int(data.get("location_id")) or None,
    )
    where = f" at {event.location.name}" if event.location else ""
    return flash_redirect(f"/events/{event.id}", f"Event created{where} with {event.fight_count} games.")


@router.post("/locations/quick")
async def location_quick(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Register a venue straight from the events page. Editing it later is the Admin Panel's job."""
    ctx.need(Perm.LOCATIONS_CREATE)
    data = await form_data(request)
    row = locations.create_location(ctx.db, ctx.user, name=data.get("name", ""),
                                    address=data.get("address"), city=data.get("city"))
    return flash_redirect("/events?create=1", f"Location '{row.name}' registered. Pick it for the new event.")


@router.post("/events/{event_id}/stream")
async def event_stream(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Start the stream (early betting from the livestream page closes) or put the event back to pre-show."""
    ctx.need(Perm.FIGHTS_LOCK)
    data = await form_data(request)
    live = data.get("action") == "start"
    events.set_stream_live(ctx.db, ctx.user, event_id, live)
    fid = data.get("fight_id", "")
    back = f"/change_name?event_id={event_id}" + (f"&fight_id={int(fid)}" if str(fid).isdigit() else "")
    return flash_redirect(back, "Stream started: early betting is closed." if live else "Back to pre-show: early betting is open.")


@router.post("/events/{event_id}/status")
async def events_status(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    data = await form_data(request)
    if not ctx.can(Perm.EVENTS_MANAGE):
        ctx.need(Perm.EVENTS_OPEN)           # a Declarator may only set an event to OPEN
        if data.get("status", "open") != "open":
            raise Forbidden("You can only open an event.")
    previous = events.get_event(ctx.db, event_id).status.value
    events.set_event_status(ctx.db, ctx.user, event_id, EventStatus(data.get("status", "open")))
    undo = {"action": f"/events/{event_id}/status", "fields": {"status": previous}} if previous != "canceled" else None
    return flash_redirect(f"/events/{event_id}", "Event status updated.", undo=undo)


@router.get("/events/{event_id}")
async def event_detail(event_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_VIEW)
    event = events.get_event(ctx.db, event_id)
    fights = events.fights_of(ctx.db, event_id)
    pools = {fight.id: betting.pool_snapshot(ctx.db, fight) for fight in fights}
    from app.services import players as player_service
    hide = hides_totals(ctx.user)
    my_players = [] if hide else player_service.list_players(ctx.db, ctx.user, limit=300)
    show_money = ctx.can(Perm.CASHFLOW_VIEW) and not hide
    return render(ctx, "event_detail.html", active="events", event=event, fights=fights,
                  pools=pools, my_players=my_players, hide_totals=hide,
                  house_cut=events.house_cut_for(ctx.db, event, ctx.store),
                  expenses=events.list_expenses(ctx.db, event.id) if show_money else [],
                  summary=reports.event_summary(ctx.db, event.id) if show_money else None,
                  role_of=events.role_of)


@router.get("/change_name")
async def change_name(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Fight view: entry names, declare, recompute, redeclare (was change_name.php)."""
    ctx.need(Perm.FIGHTS_VIEW)
    params = request.query_params
    event_id = _int(params.get("event_id") or params.get("id"))
    fight_id = _int(params.get("fight_id"))
    number = _int(params.get("gs") or params.get("fight"))

    if not event_id:
        open_events = events.list_events(ctx.db, limit=50)
        return render(ctx, "change_name.html", active="change_name", event=None, fight=None,
                      events=open_events, fights=[], stats=events.lobby_stats(ctx.db, [e.id for e in open_events]),
                      pending=settlement.pending_runs(ctx.db) if ctx.user.is_top else [])

    event = events.get_event(ctx.db, event_id)
    fights = events.fights_of(ctx.db, event_id)
    fight = None
    if fight_id:
        fight = events.get_fight(ctx.db, fight_id)
        if fight.event_id != event.id:
            raise ValidationError("That fight does not belong to this event.")
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
        if fight.status.value in ("open", "locked") and ctx.user.is_top:
            plan = settlement.build_plan(ctx.db, fight, Result.MERON, ctx.store).as_dict()
    # Early-bet panel inside the video: accounts that can place bets, only while the event is open and the stream
    # has not started. (The staff Seller Desk panel is no longer shown here; /sell is unchanged.)
    bet_panel = None
    if (fight is not None and ctx.can(Perm.BETS_PLACE) and not hide
            and event.status == EventStatus.OPEN and event.live_started_at is None):
        bet_panel = {
            "players": [{"id": p.id, "name": p.full_name, "ref": p.username or p.card_number or ""}
                        for p in player_service.list_players(ctx.db, ctx.user, limit=300)],
            "draw_enabled": bool(ctx.store.get("fight.draw_bets_enabled")),
            "can_undo": ctx.can(Perm.BETS_CANCEL),
            "undo_window": int(ctx.store.get("fight.cancel_window_seconds") or 0),
        }
    return render(ctx, "change_name.html", active="change_name", event=event, fight=fight, bet_panel=bet_panel,
                  fights=fights, events=[], snapshot=snapshot, runs=runs, tickets=tickets,
                  preview=plan, figures=figures, multipliers=multipliers, hide_totals=hide,
                  role_of=events.role_of, proof_required=proofs.required_for(ctx.user, ctx.store),
                  chat_access=store_svc.access(ctx.db, ctx.user, fight) if fight is not None else None,
                  chat_tokens=store_svc.tokens_for_fight(ctx.db, ctx.user, fight) if fight is not None else 0,
                  stream_pass=store_svc.has_stream_perks(ctx.db, ctx.user) if fight is not None else False,
                  pending=settlement.pending_runs(ctx.db) if ctx.user.is_top else [])


@router.post("/change_name/rename")
async def rename(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_RENAME)
    data = await form_data(request)
    fight = events.rename_entries(ctx.db, ctx.user, _int(data.get("fight_id")),
                                  meron=data.get("meron", ""), wala=data.get("wala", ""))
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight.id}", "Entry names updated.")


@router.post("/fights/{fight_id}/lock")
async def lock(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_LOCK)
    data = await form_data(request)
    locking = data.get("locked", "1") == "1"
    fight = events.lock_fight(ctx.db, ctx.user, fight_id, locked=locking)
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight.id}",
                          "Betting closed for this fight." if locking else "Betting re-opened.",
                          undo={"action": f"/fights/{fight.id}/lock", "fields": {"locked": "0" if locking else "1"}})


@router.post("/fights/{fight_id}/odds")
async def odds(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    fight = events.set_fixed_odds(ctx.db, ctx.user, fight_id,
                                  meron_bps=_int(data.get("meron_bps")), wala_bps=_int(data.get("wala_bps")))
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight.id}", "Odds updated.")


@router.post("/fights/{fight_id}/betmode")
async def betmode(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    previous_mode = events.get_fight(ctx.db, fight_id).bet_mode
    fight = events.set_bet_mode(ctx.db, ctx.user, fight_id, data.get("mode", "all"))
    text = {"all": "All sides open.", "meron": "Only MERON is open.", "wala": "Only WALA is open."}[fight.bet_mode]
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight.id}", text,
                          undo={"action": f"/fights/{fight.id}/betmode", "fields": {"mode": previous_mode}})


@router.post("/fights/{fight_id}/llamado")
async def llamado(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    previous_side = events.get_fight(ctx.db, fight_id).llamado_side or ""
    fight = events.set_llamado(ctx.db, ctx.user, fight_id, data.get("side"))
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight.id}",
                          f"Llamado set to {fight.llamado_side.upper()}." if fight.llamado_side
                          else "Llamado cleared.",
                          undo={"action": f"/fights/{fight.id}/llamado", "fields": {"side": previous_side}})


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


@router.post("/fights/{fight_id}/declare")
async def declare(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_DECLARE)
    data, proof = await _form_with_proof(request)
    if proof is None and proofs.required_for(ctx.user, ctx.store):
        raise ValidationError("Upload a picture of the result before you declare this game.")
    fight = events.get_fight(ctx.db, fight_id)
    result = events.resolve_result(fight, data.get("result", ""))
    run = settlement.declare(ctx.db, ctx.user, fight_id=fight_id, result=result, settings=ctx.store,
                             reason=data.get("reason"))
    if proof is not None:
        proofs.attach(ctx.db, ctx.user, fight, proof)
    where = f"/change_name?event_id={fight.event_id}&fight_id={fight_id}"
    if (run.totals or {}).get("auto_cancel"):
        floor = int(ctx.store.get("payout.min_odds_bps")) / 10000
        return flash_redirect(where, f"Game {fight.number}: odds were below {floor:.2f}x, so it was "
                              "cancelled and every stake refunded.", "warning")
    if hides_totals(ctx.user):
        return flash_redirect(where, f"Game {fight.number} declared {result.value} and settled.")
    label = events.role_of(fight, result.value)
    return flash_redirect(where, f"Game {fight.number} declared {result.value}"
                          f"{' (' + label + ')' if label else ''} and settled.")


@router.get("/fights/{fight_id}/proof")
async def proof_image(fight_id: int, ctx: Ctx = Depends(require_ctx)):
    """The picture attached to a game's result. Signed-in staff who may see games only."""
    ctx.need(Perm.FIGHTS_VIEW)
    fight = events.get_fight(ctx.db, fight_id)
    path = proofs.path_of(fight)
    if path is None:
        raise NotFound("This game has no result picture.")
    return FileResponse(path, media_type=fight.proof_mime or "application/octet-stream")


@router.post("/fights/{fight_id}/proof")
async def proof_attach(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Add or replace the result picture on a game (Admin / Master)."""
    ctx.need(Perm.FIGHTS_REDECLARE)
    _, proof = await _form_with_proof(request)
    if proof is None:
        raise ValidationError("Choose a picture to upload.")
    fight = events.get_fight(ctx.db, fight_id)
    proofs.attach(ctx.db, ctx.user, fight, proof)
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight.id}", "Result picture saved.")


@router.post("/fights/{fight_id}/correct")
async def correct(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_REDECLARE)
    data, proof = await _form_with_proof(request)
    action = data.get("action", "redeclare")
    result = Result(data["result"]) if data.get("result") else Result.MERON
    fight = events.get_fight(ctx.db, fight_id)
    run = settlement.request_correction(
        ctx.db, ctx.user, fight_id=fight_id, result=result, reason=data.get("reason", ""),
        settings=ctx.store, action=action,
    )
    if proof is not None:
        proofs.attach(ctx.db, ctx.user, fight, proof)
    message = ("Correction applied." if run.status.value == "applied"
               else "Correction queued — a second approver (Master or Admin) must approve it.")
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight_id}", message)


@router.post("/settlement/{run_id}/approve")
async def approve_run(run_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_APPROVE)
    run = settlement.approve(ctx.db, ctx.user, run_id=run_id, settings=ctx.store)
    return flash_redirect(f"/change_name?event_id={run.fight.event_id}&fight_id={run.fight_id}",
                          "Correction approved and applied.")


@router.post("/settlement/{run_id}/reject")
async def reject_run(run_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_APPROVE)
    data = await form_data(request)
    run = settlement.reject(ctx.db, ctx.user, run_id=run_id, note=data.get("note", "no reason given"))
    return flash_redirect(f"/change_name?event_id={run.fight.event_id}&fight_id={run.fight_id}",
                          "Correction rejected.")
