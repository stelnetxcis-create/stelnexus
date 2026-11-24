"""DEV tools: only the hidden Admin. Everything here acts as the real Admin (see deps._apply_view_as).

View-as lets the Admin look at the console exactly as another account sees it -- same menu, same
dashboard, same data scope -- without knowing their password. It is read-only: while it is active
every non-GET request is refused, so a preview can never change anything."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.errors import Forbidden
from app.db.models import Player, Role, Status, User
from app.web.deps import (
    VIEWAS_COOKIE, VIEWAS_MAX_AGE, Ctx, audit_action, flash_redirect, form_data, home_for,
    make_viewas_cookie_value, player_preview_user, require_ctx,
)
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
    # A player is not a User row: preview them through an in-memory stand-in (see deps.player_preview_user).
    player: Player | None = None
    if data.get("player_id", "").isdigit():
        player = ctx.db.get(Player, int(data["player_id"]))
    elif data.get("role") == "player":
        player = ctx.db.scalar(select(Player).where(Player.status == Status.ACTIVE).order_by(Player.id).limit(1))
        if player is None:
            return flash_redirect(_back(ctx), "There is no active Player account to preview yet.", "warning")
    if data.get("player_id", "").isdigit() or data.get("role") == "player":
        if player is None or player.status != Status.ACTIVE:
            return flash_redirect(_back(ctx), "Pick an active account to preview.", "warning")
        stand_in = player_preview_user(player)
        audit_action(ctx, "dev.view_as", entity_type="player", entity_id=player.id,
                     after={"username": stand_in.username, "role": "player"})
        response = flash_redirect(home_for(stand_in), f"Viewing as {stand_in.username} (Player) · read-only.", "info")
        response.headers.append("Set-Cookie", cookie_header(
            VIEWAS_COOKIE, make_viewas_cookie_value(admin.id, player_id=player.id), max_age=VIEWAS_MAX_AGE,
            http_only=True, secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
        return response
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
    return flash_redirect("/games/sabong/live?pick=1", f"Event “{title}” deleted.", "success")


@router.post("/purge/fight/{fight_id}")
async def purge_fight(fight_id: int, ctx: Ctx = Depends(require_ctx)):
    from app.services import purge
    event_id = purge.purge_fight(ctx.db, _need_dev(ctx), fight_id)
    return flash_redirect(f"/events/{event_id}" if event_id else "/games/sabong/live?pick=1", "Fight deleted.", "success")


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
