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
from app.core.rbac import Perm, bola_dev_only, permissions_of, rights_of
from app.core.settings_registry import SettingsStore
from app.db.models import Notification, Player, Role, Status, User
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
def _visible_games(ctx):
    from app import games
    return games.visible_games(ctx) if ctx is not None and ctx.user is not None else []


templates.env.globals["visible_games"] = _visible_games
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
    _rights_cache: tuple | None = field(default=None, repr=False)

    def rights(self):
        """What the signed-in account may do right now (computed once per request)."""
        cached = self._rights_cache
        if cached is None or cached[0] is not self.user:
            cached = (self.user, rights_of(self.user) if self.user else None)
            self._rights_cache = cached
        return cached[1]

    @property
    def perms(self) -> set[str]:
        """For templates: what to SHOW. Includes rights that must be asked for first, so their buttons appear
        (pressing one sends the request). Never use this to authorize anything: use can() / need()."""
        rights = self.rights()
        if rights is None:
            return set()
        return {p.value for p in rights.allowed} | {p.value for p in rights.ask}

    @property
    def viewing_as(self) -> User | None:
        """The account being simulated, or None when the Admin sees the console as themselves."""
        return self.user if self.real_user is not None else None

    def can(self, perm: Perm | str) -> bool:
        """May do it right now. A right that must be asked for first does NOT count: fail closed."""
        value = perm.value if isinstance(perm, Perm) else perm
        rights = self.rights()
        return rights is not None and any(p.value == value for p in rights.allowed)

    def need(self, perm: Perm) -> None:
        """Enforce a right. Returns, or raises Forbidden, or raises ApprovalRequired (the action is parked and the
        King / Bishops are asked; it does not run). See app.services.access."""
        from app.services import access
        access.gate(self, perm)

    def who(self, other: User | None, empty: str = "—") -> str:
        """Username to display for `other`. The hidden Admin shows as 'System' to everyone else."""
        if other is None:
            return empty
        if other.is_hidden and not (self.user and (self.user.role == Role.ADMIN or self.user.id == other.id)):
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


def make_viewas_cookie_value(admin_id: int, target_id: int | None = None, *, player_id: int | None = None) -> str:
    if player_id is not None:
        return _viewas_serializer.dumps({"a": admin_id, "p": player_id})
    return _viewas_serializer.dumps({"a": admin_id, "t": target_id})


def player_preview_user(player: Player) -> User:
    """In-memory stand-in for the DEV 'view as a player' (never added to the session, never saved).

    A player is not a User row, so the shared console cannot be swapped to one directly. This object has a
    negative id (it matches no real account, so no network scope can reach another person's data) and carries
    the player's id: permissions_of() gives it the Player rights, the ticket lists read only the player's own
    tickets, and the top bar shows the player's own wallet."""
    stand_in = User(id=-player.id, username=player.username or player.full_name, full_name=player.full_name,
                    role=Role.AGENT, status=Status.ACTIVE, password_hash="", ref_code="",
                    must_change_password=False)
    stand_in._preview_player_id = player.id
    return stand_in


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
        if data.get("p") is not None:
            player = ctx.db.get(Player, int(data["p"]))
            if player is None or player.status != Status.ACTIVE:
                return
            target = player_preview_user(player)
        else:
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


from app.services.license_info import license_info  # noqa: E402


def wallet_snapshot(ctx: Ctx) -> tuple[int | None, int]:
    """(my credit in cents or None when this account holds none, requests waiting for me to review).
    The page header renders it and ``/_live/wallet`` repeats it, so both always agree."""
    from sqlalchemy import select as _select
    from app.db.models import Account as _Account
    my_balance, wallet_pending = None, 0
    if ctx.user is None:
        return None, 0
    preview_pid = ctx.user.preview_player_id
    if preview_pid is not None:                            # DEV preview of a player: their own credit
        my_balance = int(ctx.db.scalar(_select(_Account.balance_cents)
                                       .where(_Account.code == f"wallet:player:{preview_pid}")) or 0)
    elif ctx.user.has_wallet:
        _row = ctx.db.scalar(_select(_Account.balance_cents).where(_Account.code == f"wallet:user:{ctx.user.id}"))
        my_balance = int(_row or 0)
        if Perm.REQUESTS_REVIEW.value in ctx.perms:
            from app.services import cashdesk as _cashdesk
            wallet_pending = _cashdesk.pending_count(ctx.db, ctx.user)
    elif Perm.REQUESTS_REVIEW.value in ctx.perms:          # a Teller holds no balance
        from app.services import cashdesk as _cashdesk
        wallet_pending = _cashdesk.pending_count(ctx.db, ctx.user)
    return my_balance, wallet_pending


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
    my_balance, wallet_pending = wallet_snapshot(ctx)
    context = {
        "license": license_info(store), "my_balance": my_balance, "wallet_pending": wallet_pending, "request": ctx.request, "ctx": ctx, "user": ctx.user, "perms": ctx.perms,
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
    notes = None
    previewed = ctx.user.preview_player_id if (ctx.user is not None and ctx.real_user is not None) else None
    if previewed is not None:
        from app.core.errors import Forbidden as _Forbidden
        from app.services import players as _players
        person = ctx.db.get(_Player, previewed)
        if person is not None:
            try:
                _players.assert_can_bet(ctx.db, person, 0, ctx.store)
                block = None
            except _Forbidden as exc:
                block = exc.message
            limit = person.daily_bet_limit_cents
            if limit is None:
                limit = int(ctx.store.get("player.daily_bet_limit_cents")) or None
            notes = {"upline": ctx.who(person.agent), "status": person.status.value,
                     "kyc": bool(person.kyc_verified), "can_bet": block is None, "block": block,
                     "staked": _players.staked_today(ctx.db, person), "limit": limit}
    return {
        "groups": groups,
        "players": players,
        "players_total": players_total,
        "player_notes": notes,
        "perms": sorted(ctx.perms),
        "env": _cfg.ENV,
        "db": ctx.db.get_bind().dialect.name,
    }


def home_for(user: User | None) -> str:
    """Landing page after sign-in. Declarators and Pawns have no dashboard: a Pawn with Admin Panel
    access lands there, everyone else of that kind lands on the fights."""
    if user is None:
        return "/login"
    if user.preview_player_id is not None:
        return "/games/sabong/live?pick=1"
    if user.role.value == "declarator":
        return "/games/sabong/live"
    if user.role.value == "teller":
        return "/wallet?t=approvals"
    if user.role.value == "pawn":
        return "/admin" if Perm.PANEL_VIEW in permissions_of(user) else "/games/sabong/live"
    return "/dashboard"


def event_url(event_id: int, fight_id: int | None = None) -> str:
    """The one page of a Sabong event (watch, bet, declare and run it), optionally on a given fight."""
    return f"/events/{int(event_id)}" + (f"?fight_id={int(fight_id)}" if fight_id else "")


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
