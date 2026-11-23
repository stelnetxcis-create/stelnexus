from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.core import ratelimit
from app.core.config import get_settings
from app.core.errors import AuthError, ValidationError
from app.core.middleware import CSRF_COOKIE, cookie_header, make_csrf_cookie
from app.core.rbac import Perm
from app.db.models import User
from app.services import auth, agents
from app.web.deps import (
    Ctx, attach_session_cookie, clear_session_cookie, form_data, flash_redirect, home_for,
    optional_ctx, render, require_ctx,
)

router = APIRouter()
_cfg = get_settings()


@router.get("/")
async def index(ctx: Ctx = Depends(optional_ctx)):
    return RedirectResponse(home_for(ctx.user), status_code=303)


@router.get("/login")
async def login_page(request: Request, ctx: Ctx = Depends(optional_ctx)):
    if ctx.user is not None:
        return RedirectResponse(home_for(ctx.user), status_code=303)
    # Bind to whatever session cookie the browser already holds (it may be stale), because
    # the CSRF check on POST /login validates against that same cookie.
    token, cookie_value = make_csrf_cookie(request.cookies.get(_cfg.SESSION_COOKIE, ""))
    ctx.csrf_token = token
    response = render(ctx, "login.html", csrf_token=token, active="login")
    response.headers.append("Set-Cookie", cookie_header(
        CSRF_COOKIE, cookie_value, max_age=1800, http_only=False,
        secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
    return response


@router.post("/login")
async def login_submit(request: Request, ctx: Ctx = Depends(optional_ctx)):
    data = await form_data(request)
    username = data.get("username", "")
    try:
        ratelimit.check(f"login-post:{ctx.ip}", limit=30, window_seconds=300)
        user = auth.authenticate(ctx.db, username=username, password=data.get("password", ""),
                                 ip=ctx.ip, settings=ctx.store)
    except AuthError as exc:
        token, cookie_value = make_csrf_cookie(request.cookies.get(_cfg.SESSION_COOKIE, ""))
        ctx.csrf_token = token
        response = render(ctx, "login.html", status_code=401, error=exc.message,
                          username=username[:32], csrf_token=token, active="login")
        response.headers.append("Set-Cookie", cookie_header(
            CSRF_COOKIE, cookie_value, max_age=1800, http_only=False,
            secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
        return response

    raw, _row = auth.create_session(ctx.db, user, ip=ctx.ip,
                                    user_agent=request.headers.get("user-agent"), settings=ctx.store)
    target = "/profile?next=1" if user.must_change_password else home_for(user)
    response = flash_redirect(target, f"Welcome back, {user.full_name.split(' ')[0]}.")
    attach_session_cookie(response, raw)
    return response


@router.post("/logout")
async def logout(request: Request, ctx: Ctx = Depends(optional_ctx)):
    if ctx.session_row is not None:
        auth.destroy_session(ctx.db, ctx.session_row, ctx.user)
    response = flash_redirect("/login", "You have been signed out.", "info")
    clear_session_cookie(response)
    return response


@router.get("/preferences")
async def settings_page(ctx: Ctx = Depends(require_ctx)):
    """Per-device preferences (live sounds, animation level). Nothing here touches the account."""
    return render(ctx, "preferences.html", active="preferences")


@router.get("/profile")
async def profile(ctx: Ctx = Depends(require_ctx)):
    return render(ctx, "profile.html", active="profile",
                  tokens=auth.list_api_tokens(ctx.db, ctx.user),
                  wallet=agents.wallet_balance(ctx.db, ctx.user),
                  commission=agents.commission_balance(ctx.db, ctx.user))


@router.post("/profile/password")
async def change_password(request: Request, ctx: Ctx = Depends(require_ctx)):
    data = await form_data(request)
    auth.change_password(ctx.db, ctx.user, current=data.get("current", ""),
                         new=data.get("new", ""), settings=ctx.store)
    raw, _ = auth.create_session(ctx.db, ctx.user, ip=ctx.ip,
                                 user_agent=request.headers.get("user-agent"), settings=ctx.store)
    response = flash_redirect(home_for(ctx.user), "Password updated. Other sessions were signed out.")
    attach_session_cookie(response, raw)
    return response


@router.post("/profile/tokens")
async def create_token(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.TOKENS_MANAGE)
    data = await form_data(request)
    raw, row = auth.create_api_token(ctx.db, ctx.user, name=data.get("name", "mobile"),
                                     days=int(data.get("days", "30") or 30))
    return render(ctx, "profile.html", active="profile", new_token=raw,
                  tokens=auth.list_api_tokens(ctx.db, ctx.user),
                  wallet=agents.wallet_balance(ctx.db, ctx.user),
                  commission=agents.commission_balance(ctx.db, ctx.user))


@router.post("/profile/tokens/{token_id}/revoke")
async def revoke_token(token_id: int, ctx: Ctx = Depends(require_ctx)):
    auth.revoke_api_token(ctx.db, ctx.user, token_id)
    return flash_redirect("/profile", "API token revoked.")
