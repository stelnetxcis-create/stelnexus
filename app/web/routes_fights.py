from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.core.config import get_settings
from app.core.errors import Forbidden, NotFound, ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals, sees_house
from app.db.models import EventStatus, PayoutModel, Result
from app.services import accounts, betting, events, locations, proofs, reports, settlement, stream_access, streams
from app.services import store as store_svc
from urllib.parse import urlencode

from app.web.conditional import wants_fragment
from app.web.deps import Ctx, event_url, flash_redirect, form_data, game_gate, render, require_ctx, set_return_to
from app.web.paging import paginate

router = APIRouter(dependencies=[Depends(game_gate("sabong"))])
_cfg = get_settings()


async def _form_with_proof(request: Request) -> tuple[dict[str, str], proofs.Proof | None]:
    """A multipart form: its text fields plus the validated result picture (None if none sent)."""
    form = await request.form()
    data = {key: value for key, value in form.items() if isinstance(value, str)}
    set_return_to(data.get("return_to"))
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
async def events_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Moved: Games > Sabong > Schedule (Games Standard 4.8). Kept as a redirect so old links work."""
    ctx.need(Perm.EVENTS_VIEW)
    query = request.url.query
    return RedirectResponse("/games/sabong/live?pick=1" + (f"&{query}" if query else ""), status_code=303)


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
    return flash_redirect("/games/sabong/live?pick=1&create=1", f"Location '{row.name}' registered. Pick it for the new event.")


@router.post("/events/{event_id}/stream")
async def event_stream(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Put the CURRENT game on air (its betting and chat close until it is declared; the next game opens after that)
    or back to pre-show. No fight_id = the current game."""
    ctx.need(Perm.FIGHTS_LOCK)
    data = await form_data(request)
    live = data.get("action") == "start"
    fid = data.get("fight_id", "")
    fight = events.set_stream_live(ctx.db, ctx.user, event_id, live, int(fid) if str(fid).isdigit() else None)
    back = event_url(event_id, fight.id)
    return flash_redirect(back, (f"Fight #{fight.number} is on air: betting and chat are closed until it is declared. "
                                 "The next game opens after that." if live
                                 else f"Fight #{fight.number} is back to pre-show: betting and chat are open."))


@router.post("/events/{event_id}/status")
async def events_status(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    data = await form_data(request)
    if not ctx.can(Perm.EVENTS_MANAGE):
        ctx.need(Perm.EVENTS_OPEN)           # a Declarator may open or close an event, never draft or cancel it
        if data.get("status", "open") not in ("open", "closed"):
            raise Forbidden("You can only open or close an event.")
    previous = events.get_event(ctx.db, event_id).status.value
    events.set_event_status(ctx.db, ctx.user, event_id, EventStatus(data.get("status", "open")))
    undo = {"action": f"/events/{event_id}/status", "fields": {"status": previous}} if previous != "canceled" else None
    return flash_redirect(f"/events/{event_id}", "Event status updated.", undo=undo)


@router.get("/events/create")
async def events_create_page(ctx: Ctx = Depends(require_ctx)):
    """Old link: creating an event is the Create drawer on Games > Sabong > Schedule."""
    ctx.need(Perm.EVENTS_CREATE)
    return RedirectResponse("/games/sabong/live?pick=1&create=1", status_code=303)


@router.get("/events/{event_id}")
async def event_page(event_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """The one page of a Sabong event, the way /bola/draws/{id} is for a draw: watch and bet on the fight, declare it,
    and (by right) open / close / cancel the event, all here. ?fight_id= picks the fight."""
    ctx.need(Perm.FIGHTS_VIEW)
    return await _render_event(request, ctx, event_id)


async def sabong_live(request: Request, ctx: Ctx):
    """Games > Sabong > Live. Straight into the event when exactly one is open; otherwise the event cards."""
    ctx.need(Perm.FIGHTS_VIEW)
    q = request.query_params
    legacy = _int(q.get("event_id") or q.get("id"))
    if legacy:                                                # an old /change_name?event_id=... link
        return _legacy_event_redirect(request, legacy)
    listed = events.list_events(ctx.db, limit=50)
    if not q.get("pick"):
        open_now = [e for e in listed if e.status == EventStatus.OPEN]
        if len(open_now) == 1:
            return RedirectResponse(event_url(open_now[0].id), status_code=303)
    # No single open event to jump into: show the game's page. It is the same page managers use to create and filter
    # events (Today / Upcoming / Past / All, "Create event"), so playing and managing Sabong are one place.
    from app import games
    from app.games.base import SCHEDULE_FILTERS
    adapter = games.get("sabong")
    when = q.get("when")
    when = when if when in SCHEDULE_FILTERS else adapter.schedule_default
    extra = adapter.schedule_context(ctx, when)
    return render(ctx, "games_schedule.html", active="events", game=adapter, when=when, filters=SCHEDULE_FILTERS,
                  rows=[], spec=adapter.create_spec(ctx), **extra)


def _legacy_event_redirect(request: Request, event_id: int) -> RedirectResponse:
    rest = {k: v for k, v in request.query_params.items() if k not in ("event_id", "id")}
    return RedirectResponse(f"/events/{event_id}" + (f"?{urlencode(rest)}" if rest else ""), status_code=303)


@router.get("/change_name")
async def change_name(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Old URL of the Fight View. Kept so bookmarks, printed links and old notifications still land."""
    ctx.need(Perm.FIGHTS_VIEW)
    event_id = _int(request.query_params.get("event_id") or request.query_params.get("id"))
    if event_id:
        return _legacy_event_redirect(request, event_id)
    return RedirectResponse("/games/sabong/live?pick=1", status_code=303)


async def _render_event(request: Request, ctx: Ctx, event_id: int, template: str = "change_name.html",
                        extra: dict | None = None, active: str = "events"):
    params = request.query_params
    fight_id = _int(params.get("fight_id"))
    number = _int(params.get("gs") or params.get("fight"))
    frag = wants_fragment(request)
    event = events.get_event(ctx.db, event_id)
    fights = events.fights_of(ctx.db, event_id)
    # The event regions (head / body) never show a fight, so refreshing them skips every fight-level query.
    only_event = frag in ("event-head", "event-body")
    fight = None
    if not only_event:
        if fight_id:
            fight = events.get_fight(ctx.db, fight_id)
            if fight.event_id != event.id:
                raise ValidationError("That fight does not belong to this event.")
        elif number:
            fight = events.get_fight_by_number(ctx.db, event.id, number)
        elif fights:
            fight = events.current_fight(ctx.db, event.id) or fights[0]      # open on what is happening now

    plan = None
    snapshot = None
    runs = []
    tickets = []
    tickets_pager = None
    figures = None
    multipliers = None
    figs = None
    hide = hides_totals(ctx.user)
    if fight is not None:
        runs = [] if hide else settlement.runs_for(ctx.db, fight.id)
        if not hide:
            snapshot = betting.pool_snapshot(ctx.db, fight)
            tickets_pager, tickets = paginate(request, 200, lambda limit, offset: betting.list_tickets(
                ctx.db, ctx.user, fight_id=fight.id, limit=limit, offset=offset))
            figures = reports.fight_figures(ctx.db, fight)
            figs = None if event.payout_model.value == "fixed" else betting.pool_figures(snapshot["meron"], snapshot["wala"], events.house_cut_for(ctx.db, event, ctx.store),
                                        events.platform_cut_for(ctx.store))
            multipliers = betting.side_multipliers(
                snapshot, events.pool_cut_for(ctx.db, event, ctx.store), ctx.store, fight.llamado_side,
                house_bps=events.house_cut_for(ctx.db, event, ctx.store))
        if fight.status.value in ("open", "locked") and ctx.user.is_top:
            plan = settlement.build_plan(ctx.db, fight, Result.MERON, ctx.store).as_dict()
    # Bet panel inside the video: any account that holds credit bets for ITSELF from its own wallet (no player picker).
    # Shown while the event is open. It is always
    # rendered; the page script hides it per game (only for a game that is on air / closed) so that switching
    # to an upcoming game brings it back. The server enforces the same rule per game in betting.place_bet.
    bet_panel = None
    if (fight is not None and ctx.can(Perm.BETS_PLACE) and ctx.user.has_wallet and not hide
            and event.status == EventStatus.OPEN):
        bet_panel = {
            "balance_cents": int(accounts.wallet_for_user(ctx.db, ctx.user).balance_cents),
            "draw_enabled": bool(ctx.store.get("fight.draw_bets_enabled")),
            "can_undo": ctx.can(Perm.BETS_CANCEL),
            "undo_window": int(ctx.store.get("fight.cancel_window_seconds") or 0),
        }
    # The event-level half (what the betting sheet was): every fight's pool in ONE grouped query; the heavier parts
    # (player picker, expenses, summary) are skipped when only the fight panel is being refreshed.
    with_event_body = frag != "fight-panel"
    show_money = ctx.can(Perm.CASHFLOW_VIEW) and not hide
    return render(ctx, template, active=active, event=event, fight=fight, bet_panel=bet_panel,
                  fights=fights, events=[], snapshot=snapshot, runs=runs, tickets=tickets,
                  tickets_pager=tickets_pager, preview=plan, figures=figures, multipliers=multipliers, hide_totals=hide,
                  can_house=sees_house(ctx.user) and not hide,
                  house_cut_bps=events.house_cut_for(ctx.db, event, ctx.store), house_cut=events.house_cut_for(ctx.db, event, ctx.store),
                  platform_cut_bps=events.platform_cut_for(ctx.store), figs=figs,
                  role_of=events.role_of, proof_required=proofs.required_for(ctx.user, ctx.store),
                  fv_phase=events.stream_phase_of(ctx.db, fight) if fight is not None else None,
                  cur_number=(lambda c: c.number if c else None)(events.current_fight(ctx.db, event.id)),
                  chat_access=store_svc.access(ctx.db, ctx.user, fight) if fight is not None else None,
                  chat_tokens=store_svc.tokens_for_fight(ctx.db, ctx.user, fight) if fight is not None else 0,
                  stream_pass=store_svc.has_stream_perks(ctx.db, ctx.user) if fight is not None else False,
                  stream_payload=(stream_access.apply(ctx.db, ctx.user, ctx.store, streams.payload(ctx.db, event_id=event.id, fight_id=fight.id),
                                         fight_id=fight.id, event_id=event.id)
                  if fight is not None else None),
                  pending=settlement.pending_runs(ctx.db) if ctx.user.is_top else [],
                  pools=betting.pool_snapshots(ctx.db, [f.id for f in fights]),
                  active_fight_id=fight.id if fight is not None else 0,
                  expenses=events.list_expenses(ctx.db, event.id) if (show_money and with_event_body) else [],
                  summary=reports.event_summary(ctx.db, event.id) if (show_money and with_event_body) else None,
                  **(extra or {}))


@router.post("/change_name/rename")
async def rename(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_RENAME)
    data = await form_data(request)
    fight = events.rename_entries(ctx.db, ctx.user, _int(data.get("fight_id")),
                                  meron=data.get("meron", ""), wala=data.get("wala", ""))
    return flash_redirect(event_url(fight.event_id, fight.id), "Entry names updated.")


@router.post("/fights/{fight_id}/lock")
async def lock(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_LOCK)
    data = await form_data(request)
    locking = data.get("locked", "1") == "1"
    fight = events.lock_fight(ctx.db, ctx.user, fight_id, locked=locking)
    return flash_redirect(event_url(fight.event_id, fight.id),
                          "Betting closed for this fight." if locking else "Betting re-opened.",
                          undo={"action": f"/fights/{fight.id}/lock", "fields": {"locked": "0" if locking else "1"}})


@router.post("/fights/{fight_id}/odds")
async def odds(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    fight = events.set_fixed_odds(ctx.db, ctx.user, fight_id,
                                  meron_bps=_int(data.get("meron_bps")), wala_bps=_int(data.get("wala_bps")))
    return flash_redirect(event_url(fight.event_id, fight.id), "Odds updated.")


@router.post("/fights/{fight_id}/betmode")
async def betmode(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    previous_mode = events.get_fight(ctx.db, fight_id).bet_mode
    fight = events.set_bet_mode(ctx.db, ctx.user, fight_id, data.get("mode", "all"))
    text = {"all": "All sides open.", "meron": "Only MERON is open.", "wala": "Only WALA is open."}[fight.bet_mode]
    return flash_redirect(event_url(fight.event_id, fight.id), text,
                          undo={"action": f"/fights/{fight.id}/betmode", "fields": {"mode": previous_mode}})


@router.post("/fights/{fight_id}/llamado")
async def llamado(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    data = await form_data(request)
    previous_side = events.get_fight(ctx.db, fight_id).llamado_side or ""
    fight = events.set_llamado(ctx.db, ctx.user, fight_id, data.get("side"))
    return flash_redirect(event_url(fight.event_id, fight.id),
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
async def expense_delete(expense_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.EVENTS_MANAGE)
    await form_data(request)                    # only to read return_to
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
    where = event_url(fight.event_id, fight_id)
    if (run.totals or {}).get("auto_cancel"):
        floor = int(ctx.store.get("payout.min_odds_bps")) / 10000
        return flash_redirect(where, f"Fight {fight.number}: the winning side would pay below {floor:.2f}x even "
                              "with the cuts lowered, so it was cancelled and every stake refunded.", "warning")
    if (run.totals or {}).get("relief"):
        floor = int(ctx.store.get("payout.min_odds_bps")) / 10000
        note = f" The cuts were lowered so winners are paid at least {floor:.2f}x."
        if not hides_totals(ctx.user):
            return flash_redirect(where, f"Fight {fight.number} declared {result.value} and settled.{note}")
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
    return flash_redirect(event_url(fight.event_id, fight.id), "Result picture saved.")


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
    return flash_redirect(event_url(fight.event_id, fight_id), message)


@router.post("/settlement/{run_id}/approve")
async def approve_run(run_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_APPROVE)
    run = settlement.approve(ctx.db, ctx.user, run_id=run_id, settings=ctx.store)
    return flash_redirect(event_url(run.fight.event_id, run.fight_id),
                          "Correction approved and applied.")


@router.post("/settlement/{run_id}/reject")
async def reject_run(run_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.FIGHTS_APPROVE)
    data = await form_data(request)
    run = settlement.reject(ctx.db, ctx.user, run_id=run_id, note=data.get("note", "no reason given"))
    return flash_redirect(event_url(run.fight.event_id, run.fight_id),
                          "Correction rejected.")
