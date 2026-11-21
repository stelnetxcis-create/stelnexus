"""JSON API for the mobile / PWA client.

Auth: ``Authorization: Bearer snx_...`` (tokens are created at /profile or via
POST /api/v1/auth/token). Every endpoint applies the same RBAC and downline
scoping as the HTML console -- the services do the enforcing, not this layer.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AuthError, Forbidden, ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm, hides_totals, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import BolaKind, DrawStatus, EventStatus, Side, TicketStatus, User
from app.db.session import get_db
from app.services import agents, auth, betting, bola, events, players, reports, snapshots

router = APIRouter(tags=["api"])


@dataclass
class ApiCtx:
    db: Session
    store: SettingsStore
    user: User
    ip: str | None

    def need(self, perm: Perm) -> None:
        if perm not in permissions_of(self.user):
            raise Forbidden("Your role is not allowed to perform this action.")


def _client_ip(request: Request) -> str | None:
    return request.scope.get("state", {}).get("client_ip")


def api_ctx(request: Request, db: Session = Depends(get_db)) -> ApiCtx:
    header = request.headers.get("authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else None
    user = auth.resolve_api_token(db, raw)
    if user is None:
        raise AuthError("Missing, invalid or expired API token.")
    return ApiCtx(db=db, store=SettingsStore(db), user=user, ip=_client_ip(request))


# --- serializers -----------------------------------------------------------

def _user(u: User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": str(u.role),
            "ref_code": u.ref_code, "must_change_password": u.must_change_password}


def _fight(f) -> dict:
    return {"id": f.id, "event_id": f.event_id, "number": f.number, "status": str(f.status),
            "result": str(f.result) if f.result else None,
            "meron": f.entry_name_meron, "wala": f.entry_name_wala,
            "llamado": f.llamado_side, "bet_mode": f.bet_mode}


def _event(e) -> dict:
    return {"id": e.id, "title": e.title, "date": e.event_date, "status": str(e.status)}


def _ticket(t) -> dict:
    return {"id": t.id, "code": t.code, "event_id": t.event_id, "fight_id": t.fight_id,
            "side": str(t.side), "stake_cents": t.stake_cents, "est_payout_cents": t.est_payout_cents,
            "payout_cents": t.payout_cents, "status": str(t.status), "placed_at": t.placed_at,
            "player_id": t.player_id, "is_diy": t.is_diy}


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("Body must be valid JSON.") from exc
    if not isinstance(data, dict):
        raise ValidationError("Body must be a JSON object.")
    return data


# --- auth ------------------------------------------------------------------

@router.post("/auth/token")
async def issue_token(request: Request, db: Session = Depends(get_db)):
    """Exchange username/password for a bearer token (rate limited + lockout)."""
    data = await _json(request)
    store = SettingsStore(db)
    user = auth.authenticate(db, username=str(data.get("username", "")),
                             password=str(data.get("password", "")),
                             ip=_client_ip(request), settings=store)
    auth.end_other_logins(db, user, settings=store)       # one device per account
    raw, row = auth.create_api_token(db, user, name=str(data.get("device", "mobile")),
                                     days=int(data.get("days", 30) or 30))
    return {"token": raw, "expires_at": jsonable_encoder(row.expires_at), "user": _user(user)}


@router.get("/me")
async def me(c: ApiCtx = Depends(api_ctx)):
    return {**_user(c.user), "wallet_cents": agents.wallet_balance(c.db, c.user),
            "commission_cents": agents.commission_balance(c.db, c.user),
            "permissions": sorted(p.value for p in permissions_of(c.user))}


# --- events / fights -------------------------------------------------------

@router.get("/events")
async def list_events(status: str | None = None, limit: int = 50, offset: int = 0,
                      c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.EVENTS_VIEW)
    try:
        st = EventStatus(status) if status else None
    except ValueError as exc:
        raise ValidationError("Unknown event status.") from exc
    return {"events": [_event(e) for e in events.list_events(c.db, limit=limit, offset=offset, status=st)]}


@router.get("/events/{event_id}/fights")
async def event_fights(event_id: int, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.FIGHTS_VIEW)
    event = events.get_event(c.db, event_id)
    out = []
    for fight in events.fights_of(c.db, event.id):
        out.append(_fight(fight) if hides_totals(c.user)
                   else {**_fight(fight), "pool": betting.pool_snapshot(c.db, fight)})
    return {"event": _event(event), "fights": out}


@router.get("/fights/{fight_id}")
async def fight_detail(fight_id: int, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.FIGHTS_VIEW)
    fight = events.get_fight(c.db, fight_id)
    if hides_totals(c.user):   # declarators never see stakes
        return _fight(fight)
    return {**_fight(fight), "pool": betting.pool_snapshot(c.db, fight)}


@router.get("/fights/{fight_id}/estimate")
async def estimate(fight_id: int, side: str, amount: str, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BETS_VIEW)
    try:
        s = Side(side)
    except ValueError as exc:
        raise ValidationError("Side must be meron, wala or draw.") from exc
    fight = events.get_fight(c.db, fight_id)
    cents = to_cents(amount)
    return {"stake_cents": cents,
            "est_payout_cents": betting.estimate_payout(c.db, fight, s, cents, c.store)}


# --- tickets ---------------------------------------------------------------

@router.post("/bets")
async def place_bet(request: Request, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BETS_PLACE)
    data = await _json(request)
    try:
        side = Side(str(data.get("side", "")))
        fight_id = int(data.get("fight_id"))
    except (ValueError, TypeError) as exc:
        raise ValidationError("fight_id and side (meron|wala|draw) are required.") from exc
    player_id = data.get("player_id")
    ticket = betting.place_bet(
        c.db, c.user, fight_id=fight_id, side=side,
        stake_cents=to_cents(str(data.get("amount", "0"))), settings=c.store,
        player_id=int(player_id) if player_id else None,
        is_diy=bool(data.get("is_diy", False)),
        idempotency_key=data.get("idempotency_key") or request.headers.get("idempotency-key"),
        ip=c.ip)
    return {"ticket": _ticket(ticket)}


@router.get("/tickets")
async def list_tickets(event_id: int | None = None, fight_id: int | None = None,
                       status: str | None = None, q: str | None = None,
                       limit: int = 100, offset: int = 0, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BETS_VIEW)
    try:
        st = TicketStatus(status) if status else None
    except ValueError as exc:
        raise ValidationError("Unknown ticket status.") from exc
    rows = betting.list_tickets(c.db, c.user, event_id=event_id, fight_id=fight_id, status=st,
                                search=q, limit=min(limit, 200), offset=max(offset, 0))
    return {"tickets": [_ticket(t) for t in rows]}


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: int, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BETS_VIEW)
    ticket = betting.assert_ticket_visible(c.db, c.user, betting.get_ticket(c.db, ticket_id))
    return {"ticket": _ticket(ticket)}


@router.get("/tickets/by-code/{code}")
async def ticket_by_code(code: str, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BETS_VIEW)
    ticket = betting.assert_ticket_visible(c.db, c.user, betting.get_ticket_by_code(c.db, code))
    return {"ticket": _ticket(ticket)}


@router.post("/tickets/{ticket_id}/cancel")
async def cancel_ticket(ticket_id: int, request: Request, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BETS_CANCEL)
    data = await _json(request)
    ticket = betting.cancel_ticket(c.db, c.user, ticket_id=ticket_id,
                                   reason=str(data.get("reason", "")), settings=c.store)
    return {"ticket": _ticket(ticket)}


# --- dashboard -------------------------------------------------------------

@router.get("/dashboard")
async def dashboard(c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.DASHBOARD)
    stats = reports.dashboard(c.db, c.user, flows=snapshots.flows(c.db).payload)
    if not c.user.is_top:
        # The HTML dashboard hides company-wide figures from non-admins; so does the API.
        stats = {k: stats[k] for k in ("open_fights", "events") if k in stats}
    return jsonable_encoder(stats)


# --- card tap lookup -------------------------------------------------------

@router.get("/players/by-card/{card_number}")
async def player_by_card(card_number: str, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.PLAYERS_VIEW)
    found = players.find_by_card(c.db, card_number)
    player = players.get_player(c.db, c.user, found.id)  # enforces downline scope
    return {"id": player.id, "full_name": player.full_name, "status": str(player.status),
            "balance_cents": players.wallet(c.db, player).balance_cents}


# --- bola (the two-number draw game) ----------------------------------------
# Same bearer auth, error envelope and idempotency contract as /bets. Amounts in
# request bodies are pesos ("7", "7.50"); every *_cents field in responses is
# integer centavos. The whole group answers 423-style LockedState while
# bola.enabled is off (the services enforce it, not this layer).

def _bola_draw(d, *, detail: bool = False) -> dict:
    out = {"id": d.id, "location_id": d.location_id,
           "area": d.location.name if d.location else None,
           "draw_date": d.draw_date, "slot": d.slot,
           "scheduled_at": d.scheduled_at, "cutoff_at": d.cutoff_at,
           "status": str(d.status), "bettable": bola.bettable(d),
           "ball_first": d.ball_first, "ball_second": d.ball_second,
           "result": d.result_label, "declared_at": d.declared_at}
    if detail:
        out["settlement_version"] = d.settlement_version
        out["has_proof"] = bool(d.proof_file)
        out["note"] = d.note
    return out


def _bola_ticket(t, *, lines: list | None = None) -> dict:
    out = {"id": t.id, "code": t.code, "draw_id": t.draw_id, "kind": str(t.kind),
           "numbers": t.numbers, "tumbok_cents": t.tumbok_cents, "sahod_cents": t.sahod_cents,
           "line_count": t.line_count, "stake_cents": t.stake_cents,
           "payout_cents": t.payout_cents, "est_max_payout_cents": t.est_max_payout_cents,
           "status": str(t.status), "placed_at": t.placed_at, "settled_at": t.settled_at,
           "player_id": t.player_id, "agent_id": t.agent_id,
           "kubrador": t.kubrador, "cavo": t.cavo,
           "sub_agent": t.kubrador, "agent": t.cavo, "note": t.note}
    if lines is not None:
        out["lines"] = [{"first": l.first, "second": l.second,
                         "tumbok_cents": l.tumbok_cents, "sahod_cents": l.sahod_cents} for l in lines]
    return out


def _bola_odds(c: ApiCtx) -> dict:
    return {"enabled": bool(c.store.get("bola.enabled")),
            "max_number": bola.max_number(c.store),
            "multiplier_x100": bola.multiplier(c.store),
            "outcomes": bola.ordered_outcomes(c.store),
            "rtp_percent": bola.rtp_percent(c.store),
            "show_odds": bool(c.store.get("bola.show_odds_to_players"))}


@router.get("/bola/draws")
async def bola_draws(date: str | None = None, location_id: int | None = None,
                     status: str | None = None, upcoming: bool = False,
                     limit: int = 100, offset: int = 0, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_VIEW)
    try:
        day = dt.date.fromisoformat(date) if date else None
        st = DrawStatus(status) if status else None
    except ValueError as exc:
        raise ValidationError("date must be YYYY-MM-DD and status a known draw status.") from exc
    rows = bola.list_draws(c.db, date=day, location_id=location_id, status=st,
                           upcoming=upcoming, limit=min(limit, 200), offset=offset)
    return {"draws": [_bola_draw(d) for d in rows], "odds": _bola_odds(c)}


@router.get("/bola/draws/{draw_id}")
async def bola_draw_detail(draw_id: int, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_VIEW)
    draw = bola.get_draw(c.db, draw_id)
    out = {**_bola_draw(draw, detail=True), "odds": _bola_odds(c)}
    if Perm.BOLA_EXPOSURE in permissions_of(c.user) and not hides_totals(c.user):
        out["exposure"] = bola.exposure_summary(c.db, draw, c.store)
    return out


@router.get("/bola/draws/{draw_id}/preview")
async def bola_draw_preview(draw_id: int, kind: str, numbers: str,
                            tumbok: str = "0", sahod: str = "0", c: ApiCtx = Depends(api_ctx)):
    """Lines, total and per-line payouts for a slip. No state change, no wallet touch."""
    c.need(Perm.BOLA_PLACE)
    bola.get_draw(c.db, draw_id)
    try:
        nums = [int(x) for x in numbers.split(",") if x.strip()]
    except ValueError as exc:
        raise ValidationError("numbers must be a comma separated list like 12,5,36.") from exc
    return bola.slip_preview(kind, nums, to_cents(tumbok or "0"), to_cents(sahod or "0"), c.store)


@router.post("/bola/bets")
async def bola_place_bet(request: Request, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_PLACE)
    data = await _json(request)
    try:
        draw_id = int(data.get("draw_id"))
        kind = BolaKind(str(data.get("kind", "")))
    except (ValueError, TypeError) as exc:
        raise ValidationError("draw_id and kind (pair|casas3|casas6|casas10) are required.") from exc
    numbers = data.get("numbers")
    if not isinstance(numbers, list):
        raise ValidationError("numbers must be a JSON array like [12, 5, 36].")
    ticket = bola.place_bola_bet(
        c.db, c.user, draw_id=draw_id, kind=kind, numbers=numbers,
        tumbok_cents=to_cents(str(data.get("tumbok", "0") or "0")),
        sahod_cents=to_cents(str(data.get("sahod", "0") or "0")),
        player_id=None, settings=c.store,          # always the account's own credit
        idempotency_key=data.get("idempotency_key") or request.headers.get("idempotency-key"),
        note=str(data.get("note") or "") or None, ip=c.ip)
    return {"ticket": _bola_ticket(ticket, lines=bola.ticket_lines(c.db, ticket))}


@router.get("/bola/tickets")
async def bola_tickets(draw_id: int | None = None, status: str | None = None,
                       player_id: int | None = None, q: str | None = None,
                       limit: int = 100, offset: int = 0, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_VIEW)
    if hides_totals(c.user):
        raise Forbidden("Your role cannot see tickets.")
    try:
        st = TicketStatus(status) if status else None
    except ValueError as exc:
        raise ValidationError("Unknown ticket status.") from exc
    rows = bola.list_tickets(c.db, c.user, draw_id=draw_id, status=st, player_id=player_id,
                             search=q, limit=min(limit, 200), offset=max(offset, 0))
    return {"tickets": [_bola_ticket(t) for t in rows]}


@router.get("/bola/tickets/{ticket_id}")
async def bola_ticket_detail(ticket_id: int, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_VIEW)
    if hides_totals(c.user):
        raise Forbidden("Your role cannot see tickets.")
    ticket = bola.assert_ticket_visible(c.db, c.user, bola.get_ticket(c.db, ticket_id))
    return {"ticket": _bola_ticket(ticket, lines=bola.ticket_lines(c.db, ticket))}


@router.get("/bola/tickets/by-code/{code}")
async def bola_ticket_by_code(code: str, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_VIEW)
    if hides_totals(c.user):
        raise Forbidden("Your role cannot see tickets.")
    ticket = bola.assert_ticket_visible(c.db, c.user, bola.get_ticket_by_code(c.db, code))
    return {"ticket": _bola_ticket(ticket, lines=bola.ticket_lines(c.db, ticket))}


@router.post("/bola/tickets/{ticket_id}/cancel")
async def bola_cancel_ticket(ticket_id: int, request: Request, c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_CANCEL)
    data = await _json(request)
    ticket = bola.cancel_bola_ticket(c.db, c.user, ticket_id=ticket_id,
                                     reason=str(data.get("reason", "")), settings=c.store)
    return {"ticket": _bola_ticket(ticket)}


@router.get("/bola/results")
async def bola_results(location_id: int | None = None, limit: int = 60, offset: int = 0,
                       c: ApiCtx = Depends(api_ctx)):
    c.need(Perm.BOLA_VIEW)
    rows = bola.results(c.db, location_id=location_id, limit=min(limit, 200), offset=offset)
    return {"results": [_bola_draw(d) for d in rows]}
