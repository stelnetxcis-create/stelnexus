"""DEV tools: only the hidden Admin. Everything here acts as the real Admin (see deps._apply_view_as).

View-as lets the Admin look at the console exactly as another account sees it -- same menu, same
dashboard, same data scope -- without knowing their password. It is read-only: while it is active
every non-GET request is refused, so a preview can never change anything."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.errors import Forbidden, NotFound
from app.core.rbac import player_perms
from app.db.models import DrawStatus, EventStatus, Player, Role, Status, User
from app.web.deps import (
    VIEWAS_COOKIE, VIEWAS_MAX_AGE, Ctx, audit_action, flash_redirect, form_data, home_for,
    make_viewas_cookie_value, render, require_ctx,
)
from app.services import accounts, betting, bola, events, ledger, players
from app.core.config import get_settings
from app.core.middleware import cookie_header

router = APIRouter(prefix="/dev")
_cfg = get_settings()
_PREVIEWABLE = (Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.TELLER, Role.DECLARATOR, Role.PAWN)


def _need_dev(ctx: Ctx) -> User:
    if ctx.user is None or ctx.user.role != Role.ADMIN:
        raise Forbidden("DEV tools are for the Admin account only.")
    return ctx.user


@router.post("/view-as")
async def view_as(ctx: Ctx = Depends(require_ctx)):
    admin = _need_dev(ctx)
    data = await form_data(ctx.request)
    target: User | None = None
    if data.get("user_id", "").isdigit():
        target = ctx.db.get(User, int(data["user_id"]))
    elif data.get("role") in {r.value for r in _PREVIEWABLE}:
        target = ctx.db.scalar(select(User).where(User.role == Role(data["role"]), User.status == Status.ACTIVE)
                               .order_by(User.id).limit(1))
        if target is None:
            return flash_redirect(_back(ctx), f"There is no active {Role(data['role']).label} account to preview yet.", "warning")
    if target is None or target.role not in _PREVIEWABLE or target.status != Status.ACTIVE:
        return flash_redirect(_back(ctx), "Pick an active account to preview.", "warning")
    audit_action(ctx, "dev.view_as", entity_type="user", entity_id=target.id,
                 after={"username": target.username, "role": target.role.value})
    response = flash_redirect(home_for(target), f"Viewing as {target.username} ({target.role_label}) · read-only.", "info")
    response.headers.append("Set-Cookie", cookie_header(
        VIEWAS_COOKIE, make_viewas_cookie_value(admin.id, target.id), max_age=VIEWAS_MAX_AGE,
        http_only=True, secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
    return response


@router.get("/player")
async def open_player(player_id: int = 0, first: int = 0, ctx: Ctx = Depends(require_ctx)):
    """DEV menu -> read-only player preview. `first=1` (the Player chip) opens the first active player."""
    _need_dev(ctx)
    player = None
    if player_id:
        player = ctx.db.get(Player, player_id)
    elif first:
        player = ctx.db.scalar(select(Player).where(Player.status == Status.ACTIVE).order_by(Player.id).limit(1))
    if player is None:
        return flash_redirect(_back(ctx), "There is no player to preview yet.", "warning")
    audit_action(ctx, "dev.open_player", entity_type="player", entity_id=player.id,
                 after={"username": player.username, "full_name": player.full_name})
    return flash_redirect(f"/dev/player/{player.id}")


@router.get("/player/{player_id}")
async def player_preview(player_id: int, ctx: Ctx = Depends(require_ctx)):
    """Read-only 'what this player sees': credit, both games' bets, what is open to bet on, and why they
    could not bet right now. A Player is not a User row, so the shared console cannot be swapped in; this
    page reads only the player's own rows and never writes (GET only, no wallet is created)."""
    admin = _need_dev(ctx)
    player = ctx.db.get(Player, player_id)
    if player is None:
        raise NotFound("Player not found.")
    wallet = accounts.get_by_code(ctx.db, f"wallet:player:{player.id}")
    balance = int(wallet.balance_cents) if wallet is not None else 0
    entries = ledger.history(ctx.db, wallet.id, limit=15) if wallet is not None else []
    try:
        players.assert_can_bet(ctx.db, player, 0, ctx.store)
        bet_block = None
    except Forbidden as exc:
        bet_block = exc.message
    limit = player.daily_bet_limit_cents
    if limit is None:
        limit = int(ctx.store.get("player.daily_bet_limit_cents")) or None
    return render(
        ctx, "player_preview.html", active="", player=player, balance=balance, entries=entries,
        bet_block=bet_block, daily_limit=limit, staked_today=players.staked_today(ctx.db, player),
        sabong_tickets=betting.list_tickets(ctx.db, admin, player_id=player.id, limit=30),
        bola_tickets=bola.list_tickets(ctx.db, admin, player_id=player.id, limit=30),
        open_events=events.list_events(ctx.db, limit=20, status=EventStatus.OPEN),
        open_draws=bola.list_draws(ctx.db, status=DrawStatus.OPEN, limit=20),
        bola_enabled=bool(ctx.store.get("bola.enabled")),
        # The page chrome (menu, balance chip, account chip, bell) must show the PLAYER, not the Admin.
        perms={p.value for p in player_perms()}, my_balance=balance, wallet_pending=0, notifications=[],
        user=SimpleNamespace(full_name=player.full_name, username=player.username or player.full_name,
                             role_label="Player"),
    )


@router.post("/view-as/exit")
async def view_as_exit(ctx: Ctx = Depends(require_ctx)):
    _need_dev(ctx)
    response = flash_redirect("/dashboard", "Back to your own view.", "success")
    response.headers.append("Set-Cookie", cookie_header(
        VIEWAS_COOKIE, "", max_age=0, http_only=True, secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
    return response


def _back(ctx: Ctx) -> str:
    from app.main import _safe_back
    return _safe_back(ctx.request)


# ---------------------------------------------------------------------------
# Hard delete of test games (Admin only). Ledger history is kept; live stakes block it.
# ---------------------------------------------------------------------------

@router.post("/purge/event/{event_id}")
async def purge_event(event_id: int, ctx: Ctx = Depends(require_ctx)):
    from app.services import purge
    title = purge.purge_event(ctx.db, _need_dev(ctx), event_id)
    return flash_redirect("/events", f"Event “{title}” deleted.", "success")


@router.post("/purge/fight/{fight_id}")
async def purge_fight(fight_id: int, ctx: Ctx = Depends(require_ctx)):
    from app.services import purge
    event_id = purge.purge_fight(ctx.db, _need_dev(ctx), fight_id)
    return flash_redirect(f"/events/{event_id}" if event_id else "/events", "Game deleted.", "success")


@router.post("/purge/draw/{draw_id}")
async def purge_draw(draw_id: int, ctx: Ctx = Depends(require_ctx)):
    from app.services import purge
    label = purge.purge_draw(ctx.db, _need_dev(ctx), draw_id)
    return flash_redirect("/bola", f"Draw {label} deleted.", "success")


# ---------------------------------------------------------------------------
# Store effects: look an account up, then revoke what a purchase gave it (Admin only).
# ---------------------------------------------------------------------------

@router.get("/store")
async def store_effects(q: str = "", ctx: Ctx = Depends(require_ctx)):
    from app.services import store as store_svc
    from app.web.deps import render
    _need_dev(ctx)
    q = q.strip()
    found, effects = None, None
    if q:
        found = ctx.db.scalar(select(User).where(func.lower(User.username) == q.lower()))
        if found is None:
            from app.db.models import ChatAlias
            hit = ctx.db.scalar(select(ChatAlias).where(ChatAlias.alias_key == store_svc.alias_key(q)))
            found = hit.user if hit else None
        if found is not None:
            effects = store_svc.effects_of(ctx.db, found)
    return render(ctx, "dev_store.html", active="store", q=q, target=found, fx=effects)


@router.post("/store/revoke")
async def store_revoke(ctx: Ctx = Depends(require_ctx)):
    from app.services import store as store_svc
    admin = _need_dev(ctx)
    data = await form_data(ctx.request)
    target = ctx.db.get(User, int(data["user_id"])) if (data.get("user_id") or "").isdigit() else None
    back = f"/dev/store?q={target.username}" if target else "/dev/store"
    if target is None:
        return flash_redirect(back, "Pick an account first.", "warning")
    note = store_svc.revoke_effect(ctx.db, admin, target, data.get("what") or "", reason=data.get("reason") or "",
                                   refund=data.get("refund") == "1")
    return flash_redirect(back, f"{target.username}: {note}.", "success")
