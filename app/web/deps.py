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
from app.core.middleware import CSRF_COOKIE, cookie_header, csrf_cookie_valid, make_csrf_cookie
from app.core.money import fmt, pesos
from app.core import assets
from app.core.rbac import Perm, bola_dev_only, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import Notification, Role, Status, User
from app.db.models import Session as SessionRow
from app.db.session import get_db
from app.services import auth
from app.web.conditional import fragment_response, wants_fragment

_cfg = get_settings()
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.autoescape = True
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True
_flash_serializer = URLSafeTimedSerializer(_cfg.SECRET_KEY, salt="stelnexus.flash")
_viewas_serializer = URLSafeTimedSerializer(_cfg.SECRET_KEY, salt="stelnexus.viewas")

# Dev "view as": the hidden Admin can look at the console through another account's eyes, read-only.
VIEWAS_COOKIE = "snx_viewas"
VIEWAS_MAX_AGE = 8 * 3600
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

templates.env.globals.update(
    fmt=fmt, pesos=pesos, Perm=Perm, bola_dev_only=bola_dev_only,
    datefmt=lambda value, pattern="%Y-%m-%d %H:%M": value.strftime(pattern) if value else "—",
    dayfmt=lambda value: value.strftime("%Y-%m-%d") if value else "—",
)
templates.env.globals.update(css=assets.css, js=assets.js, asset_urls=assets.asset_urls, preload_fonts=assets.preload_fonts)
templates.env.filters["money"] = fmt
templates.env.filters["datetime"] = lambda v, p="%Y-%m-%d %H:%M": v.strftime(p) if v else "—"
templates.env.filters["date"] = lambda v: v.strftime("%Y-%m-%d") if v else "—"
def _ledger_label(kind: str) -> str:
    from app.services.wallet import label_for      # lazy: services import web helpers elsewhere
    return label_for(kind or "")


templates.env.filters["ledger_label"] = _ledger_label


@dataclass
class Ctx:
    request: Request
    db: DbSession
    store: SettingsStore
    user: User | None = None
    real_user: User | None = None      # the signed-in Admin while 'viewing as' someone else
    session_row: SessionRow | None = None
    csrf_token: str = ""
    flashes: list[dict] = field(default_factory=list)

    @property
    def perms(self) -> set[str]:
        return {p.value for p in permissions_of(self.user)} if self.user else set()

    @property
    def viewing_as(self) -> User | None:
        """The account being simulated, or None when the Admin sees the console as themselves."""
        return self.user if self.real_user is not None else None

    def can(self, perm: Perm | str) -> bool:
        value = perm.value if isinstance(perm, Perm) else perm
        return value in self.perms

    def need(self, perm: Perm) -> None:
        if not self.can(perm):
            raise Forbidden("Your role is not allowed to perform this action.")

    def who(self, other: User | None, empty: str = "—") -> str:
        """Username to display for `other`. The hidden Admin shows as 'System' to everyone else."""
        if other is None:
            return empty
        if other.is_hidden and not (self.user and self.user.role == other.role):
            return "System"
        return other.username

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
        _apply_view_as(ctx)
    return ctx


def make_viewas_cookie_value(admin_id: int, target_id: int) -> str:
    return _viewas_serializer.dumps({"a": admin_id, "t": target_id})


def _apply_view_as(ctx: Ctx) -> None:
    """Swap in the simulated account when the signed-in hidden Admin asked for it.

    The cookie is signed, expires, and is bound to this Admin's id; anyone else's cookie is ignored.
    While simulating, every non-GET request is refused so a preview can never move money or change
    data. /dev/* and /logout always act as the real Admin so the person can always get out."""
    user = ctx.user
    raw = ctx.request.cookies.get(VIEWAS_COOKIE)
    if user is None or user.role != Role.ADMIN or not raw:
        return
    path = ctx.request.url.path
    if path.startswith("/dev/") or path == "/logout":
        return
    try:
        data = _viewas_serializer.loads(raw, max_age=VIEWAS_MAX_AGE)
        if int(data.get("a", 0)) != user.id:
            return
        target = ctx.db.get(User, int(data.get("t", 0)))
    except (BadSignature, SignatureExpired, TypeError, ValueError, AttributeError):
        return
    if target is None or target.role == Role.ADMIN or target.status != Status.ACTIVE:
        return
    if ctx.request.method not in _SAFE_METHODS:
        raise Forbidden(f"Read-only while viewing as {target.username}. Exit the preview from the DEV menu to make changes.")
    ctx.real_user, ctx.user = user, target


def require_ctx(request: Request, db: DbSession = Depends(get_db)) -> Ctx:
    ctx = optional_ctx(request, db)
    if ctx.user is None:
        if auth.was_displaced(db, request.cookies.get(_cfg.SESSION_COOKIE)):
            raise AuthError(auth.DISPLACED_MSG)
        raise AuthError("Please sign in to continue.")
    token, _ = make_csrf_cookie(request.cookies.get(_cfg.SESSION_COOKIE, ""))
    ctx.csrf_token = token
    return ctx


def _fragment_name(ctx: Ctx) -> str | None:
    """The region the browser asked for (``X-Fragment``), if this request can be answered with one.

    The CSRF token is random per page render and its cookie is re-issued each time, so a fragment must reuse
    the token the page already holds (and must not touch the cookie), or the forms OUTSIDE the refreshed box
    would stop working. No valid token cookie: no fragment, the caller gets the whole page and falls back.
    """
    name = wants_fragment(ctx.request) if ctx.request.method == "GET" else None
    if not name or ctx.user is None:
        return None
    cookie = ctx.request.cookies.get(CSRF_COOKIE, "")
    token = cookie.rsplit(".", 1)[0] if "." in cookie else ""
    if not csrf_cookie_valid(cookie, token, ctx.request.cookies.get(_cfg.SESSION_COOKIE, "")):
        return None
    ctx.csrf_token = token
    return name


def render(ctx: Ctx, template: str, status_code: int = 200, **extra: Any):
    store = ctx.store
    settings = store.all()
    fragment = _fragment_name(ctx)
    notifications: list[Notification] = []
    if ctx.user is not None and fragment is None:          # the bell lives in the page header, not in a region
        from sqlalchemy import select
        notifications = list(ctx.db.scalars(
            select(Notification).where(Notification.user_id == ctx.user.id, Notification.is_read.is_(False))
            .order_by(Notification.id.desc()).limit(8)).all())
    real = ctx.real_user or ctx.user
    is_dev = bool(real is not None and real.role == Role.ADMIN)
    my_balance = None
    wallet_pending = 0
    if ctx.user is not None and ctx.user.has_wallet:
        from sqlalchemy import select as _select
        from app.db.models import Account as _Account
        _row = ctx.db.scalar(_select(_Account.balance_cents).where(_Account.code == f"wallet:user:{ctx.user.id}"))
        my_balance = int(_row or 0)
        if Perm.REQUESTS_REVIEW.value in ctx.perms:
            from app.services import cashdesk as _cashdesk
            wallet_pending = _cashdesk.pending_count(ctx.db, ctx.user)
    elif ctx.user is not None and Perm.REQUESTS_REVIEW.value in ctx.perms:   # a Teller holds no balance
        from app.services import cashdesk as _cashdesk
        wallet_pending = _cashdesk.pending_count(ctx.db, ctx.user)
    context = {
        "my_balance": my_balance, "wallet_pending": wallet_pending, "request": ctx.request, "ctx": ctx, "user": ctx.user, "perms": ctx.perms,
        "settings": settings, "brand": settings.get("brand.name", "StelNexus"),
        "tagline": settings.get("brand.tagline", ""), "csrf_token": ctx.csrf_token,
        "flashes": ctx.flashes, "notifications": notifications,
        "active": extra.pop("active", ""), "now": dt.datetime.now(dt.timezone.utc),
        "who": ctx.who, "is_dev": is_dev, "viewing_as": ctx.viewing_as,
        "dev": _dev_context(ctx) if is_dev else None,
        **extra,
    }
    response = templates.TemplateResponse(ctx.request, template, context, status_code=status_code)
    if fragment is not None:                               # no cookies: the page's own flash / CSRF state stays as is
        return fragment_response(ctx.request, response, fragment)
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


_DEV_ROLE_ORDER = (Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.TELLER, Role.DECLARATOR, Role.PAWN)


def _dev_context(ctx: Ctx) -> dict:
    """Everything the DEV menu shows: accounts to preview, what the previewed account may do, env."""
    from sqlalchemy import func as _func, select as _select
    from app.db.models import Player as _Player
    rows = ctx.db.scalars(_select(User).where(User.role.in_(_DEV_ROLE_ORDER), User.status == Status.ACTIVE)
                          .order_by(User.username)).all()
    groups = [{"role": role.value, "label": role.label,
               "users": [u for u in rows if u.role == role]} for role in _DEV_ROLE_ORDER]
    # Players are real accounts but live in their own table (not User), so they cannot be swapped into
    # the 'view as' session. The menu lists them and opens their history page instead.
    players = ctx.db.scalars(_select(_Player).where(_Player.status == Status.ACTIVE)
                             .order_by(_Player.username, _Player.full_name).limit(200)).all()
    players_total = int(ctx.db.scalar(_select(_func.count(_Player.id)).where(_Player.status == Status.ACTIVE)) or 0)
    return {
        "groups": groups,
        "players": players,
        "players_total": players_total,
        "perms": sorted(ctx.perms),
        "env": _cfg.ENV,
        "db": ctx.db.get_bind().dialect.name,
    }


def home_for(user: User | None) -> str:
    """Landing page after sign-in. Declarators and Pawns have no dashboard: a Pawn with Admin Panel
    access lands there, everyone else of that kind lands on the fights."""
    if user is None:
        return "/login"
    if user.role.value == "declarator":
        return "/change_name"
    if user.role.value == "teller":
        return "/wallet?t=approvals"
    if user.role.value == "pawn":
        return "/admin" if Perm.PANEL_VIEW in permissions_of(user) else "/change_name"
    return "/dashboard"


def flash_redirect(url: str, message: str | None = None, level: str = "success",
                   status_code: int = 303, undo: dict | None = None):
    """Redirect with a one-shot toast. `undo` = {"action": "/fights/3/lock", "fields": {"locked": "0"}}
    adds an Undo button to the toast that re-posts that (inverse) action with the page's CSRF token.
    Only offer it for actions that are safely reversible (lock / mode / status switches)."""
    from starlette.responses import RedirectResponse
    response = RedirectResponse(url, status_code=status_code)
    if message:
        note = {"level": level, "message": message}
        if undo and undo.get("action", "").startswith("/") and not undo["action"].startswith("//"):
            note["undo"] = {"action": undo["action"],
                            "fields": {str(k): str(v) for k, v in (undo.get("fields") or {}).items()}}
        payload = _flash_serializer.dumps([note])
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
    for name in (_cfg.SESSION_COOKIE, CSRF_COOKIE, VIEWAS_COOKIE):
        response.headers.append("Set-Cookie", cookie_header(
            name, "", max_age=0, http_only=name != CSRF_COOKIE,
            secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))


async def form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: (value if isinstance(value, str) else "") for key, value in form.items()}


def audit_action(ctx: Ctx, action: str, **kwargs) -> None:
    audit.record(ctx.db, ctx.user, action, **kwargs)
