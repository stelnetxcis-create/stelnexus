"""Shared web dependencies: authenticated context, templating, flashes, cookies."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session as DbSession

from app.core import audit
from app.core.config import get_settings
from app.core.errors import AuthError, Forbidden
from app.core.middleware import CSRF_COOKIE, cookie_header, make_csrf_cookie
from app.core.money import fmt, pesos
from app.core.rbac import Perm, permissions_for
from app.core.settings_registry import SettingsStore
from app.db.models import Notification, User
from app.db.models import Session as SessionRow
from app.db.session import get_db
from app.services import auth

_cfg = get_settings()
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.autoescape = True
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True
_flash_serializer = URLSafeTimedSerializer(_cfg.SECRET_KEY, salt="stelnexus.flash")

templates.env.globals.update(
    fmt=fmt, pesos=pesos, Perm=Perm,
    datefmt=lambda value, pattern="%Y-%m-%d %H:%M": value.strftime(pattern) if value else "—",
    dayfmt=lambda value: value.strftime("%Y-%m-%d") if value else "—",
)
templates.env.filters["money"] = fmt
templates.env.filters["datetime"] = lambda v, p="%Y-%m-%d %H:%M": v.strftime(p) if v else "—"
templates.env.filters["date"] = lambda v: v.strftime("%Y-%m-%d") if v else "—"


@dataclass
class Ctx:
    request: Request
    db: DbSession
    store: SettingsStore
    user: User | None = None
    session_row: SessionRow | None = None
    csrf_token: str = ""
    flashes: list[dict] = field(default_factory=list)

    @property
    def perms(self) -> set[str]:
        return {p.value for p in permissions_for(self.user.role)} if self.user else set()

    def can(self, perm: Perm | str) -> bool:
        value = perm.value if isinstance(perm, Perm) else perm
        return value in self.perms

    def need(self, perm: Perm) -> None:
        if not self.can(perm):
            raise Forbidden("Your role is not allowed to perform this action.")

    @property
    def ip(self) -> str | None:
        return self.request.scope.get("state", {}).get("client_ip")


def _read_flashes(request: Request) -> list[dict]:
    raw = request.cookies.get("snx_flash")
    if not raw:
        return []
    try:
        return list(_flash_serializer.loads(raw, max_age=60))
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return []


def base_ctx(request: Request, db: DbSession) -> Ctx:
    store = SettingsStore(db)
    return Ctx(request=request, db=db, store=store, flashes=_read_flashes(request))


def optional_ctx(request: Request, db: DbSession = Depends(get_db)) -> Ctx:
    ctx = base_ctx(request, db)
    resolved = auth.resolve_session(db, request.cookies.get(_cfg.SESSION_COOKIE), ctx.store)
    if resolved:
        ctx.session_row, ctx.user = resolved
    return ctx


def require_ctx(request: Request, db: DbSession = Depends(get_db)) -> Ctx:
    ctx = optional_ctx(request, db)
    if ctx.user is None:
        raise AuthError("Please sign in to continue.")
    token, _ = make_csrf_cookie(request.cookies.get(_cfg.SESSION_COOKIE, ""))
    ctx.csrf_token = token
    return ctx


def render(ctx: Ctx, template: str, status_code: int = 200, **extra: Any):
    store = ctx.store
    settings = store.all()
    notifications: list[Notification] = []
    if ctx.user is not None:
        from sqlalchemy import select
        notifications = list(ctx.db.scalars(
            select(Notification).where(Notification.user_id == ctx.user.id, Notification.is_read.is_(False))
            .order_by(Notification.id.desc()).limit(8)).all())
    context = {
        "request": ctx.request, "ctx": ctx, "user": ctx.user, "perms": ctx.perms,
        "settings": settings, "brand": settings.get("brand.name", "StelNexus"),
        "tagline": settings.get("brand.tagline", ""), "csrf_token": ctx.csrf_token,
        "flashes": ctx.flashes, "notifications": notifications,
        "active": extra.pop("active", ""), "now": dt.datetime.now(dt.timezone.utc),
        **extra,
    }
    response = templates.TemplateResponse(ctx.request, template, context, status_code=status_code)
    if ctx.flashes:
        response.delete_cookie("snx_flash", path="/")
    if ctx.csrf_token:
        _, cookie_value = make_csrf_cookie_from(ctx.csrf_token, ctx.request)
        response.headers.append("Set-Cookie", cookie_header(
            CSRF_COOKIE, cookie_value, max_age=_cfg.session_absolute_seconds,
            http_only=False, secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
    return response


def make_csrf_cookie_from(token: str, request: Request) -> tuple[str, str]:
    from app.core.middleware import sign_csrf
    session_cookie = request.cookies.get(_cfg.SESSION_COOKIE, "")
    return token, f"{token}.{sign_csrf(token, session_cookie)}"


def home_for(user: User | None) -> str:
    """Landing page after sign-in. A declarator has no dashboard."""
    if user is None:
        return "/login"
    return "/games" if user.role.value == "declarator" else "/dashboard"


def flash_redirect(url: str, message: str | None = None, level: str = "success",
                   status_code: int = 303):
    from starlette.responses import RedirectResponse
    response = RedirectResponse(url, status_code=status_code)
    if message:
        payload = _flash_serializer.dumps([{"level": level, "message": message}])
        response.headers.append("Set-Cookie", cookie_header(
            "snx_flash", payload, max_age=60, http_only=True,
            secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
    return response


def attach_session_cookie(response, raw_token: str) -> None:
    response.headers.append("Set-Cookie", cookie_header(
        _cfg.SESSION_COOKIE, raw_token, max_age=_cfg.session_absolute_seconds,
        http_only=True, secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
    token, value = make_csrf_cookie(raw_token)
    response.headers.append("Set-Cookie", cookie_header(
        CSRF_COOKIE, value, max_age=_cfg.session_absolute_seconds,
        http_only=False, secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))


def clear_session_cookie(response) -> None:
    for name in (_cfg.SESSION_COOKIE, CSRF_COOKIE):
        response.headers.append("Set-Cookie", cookie_header(
            name, "", max_age=0, http_only=name == _cfg.SESSION_COOKIE,
            secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))


async def form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: (value if isinstance(value, str) else "") for key, value in form.items()}


def audit_action(ctx: Ctx, action: str, **kwargs) -> None:
    audit.record(ctx.db, ctx.user, action, **kwargs)
