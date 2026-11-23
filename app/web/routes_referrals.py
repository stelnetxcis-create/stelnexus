"""Referral-code sign-up (public) and code management (signed-in uploaders)."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from fastapi.responses import RedirectResponse

from app.core import ratelimit
from app.core.config import get_settings
from app.core.errors import StelError, ValidationError
from app.core.middleware import CSRF_COOKIE, cookie_header, make_csrf_cookie
from app.core.rbac import Perm
from app.db.models import NETWORK_ROLES, UPLINE_ROLES, Role, Status, User
from app.services import referrals
from app.web.deps import Ctx, flash_redirect, form_data, optional_ctx, render, require_ctx
from app.web.paging import paginate

router = APIRouter()
_cfg = get_settings()
_UNITS = {"minutes": 1, "hours": 60, "days": 1440}


def _public(ctx: Ctx, template: str, status_code: int = 200, **extra):
    """Render a signed-out page with a fresh CSRF cookie (same as the login page)."""
    token, cookie_value = make_csrf_cookie("")
    ctx.csrf_token = token
    response = render(ctx, template, status_code=status_code, csrf_token=token, active="login", **extra)
    response.headers.append("Set-Cookie", cookie_header(
        CSRF_COOKIE, cookie_value, max_age=1800, http_only=False,
        secure=_cfg.COOKIE_SECURE, same_site=_cfg.COOKIE_SAMESITE))
    return response


# --- public sign-up --------------------------------------------------------

@router.get("/register")
async def register_page(request: Request, ctx: Ctx = Depends(optional_ctx)):
    if ctx.user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return _public(ctx, "register.html", step="code", code=request.query_params.get("code", "")[:16])


@router.post("/register/code")
async def register_check_code(request: Request, ctx: Ctx = Depends(optional_ctx)):
    data = await form_data(request)
    code = data.get("code", "")
    try:
        ratelimit.check(f"refcode:{ctx.ip}", limit=15, window_seconds=600)
        row = referrals.lookup_valid(ctx.db, code)
    except StelError as exc:
        return _public(ctx, "register.html", status_code=400, step="code", code=code[:16], error=exc.message)
    return _public(ctx, "register.html", step="details", code=row.code,
                   role=row.role, role_label=referrals.role_label(row.role), issuer=row.issuer.full_name)


@router.post("/register")
async def register_submit(request: Request, ctx: Ctx = Depends(optional_ctx)):
    data = await form_data(request)
    code = data.get("code", "")
    try:
        ratelimit.check(f"register:{ctx.ip}", limit=10, window_seconds=600)
        if data.get("password", "") != data.get("password2", ""):
            raise ValidationError("The two passwords do not match.")
        birth = None
        if data.get("birth_date"):
            try:
                birth = dt.date.fromisoformat(data["birth_date"])
            except ValueError as exc:
                raise ValidationError("Enter the date of birth as YYYY-MM-DD.") from exc
        user = referrals.redeem(
            ctx.db, code, username=data.get("username", ""), password=data.get("password", ""),
            full_name=data.get("full_name", ""), contact=data.get("contact"), settings=ctx.store,
            birth_date=birth, gcash_number=data.get("gcash_number"))
    except StelError as exc:
        try:
            row = referrals.lookup_valid(ctx.db, code)
        except StelError:
            return _public(ctx, "register.html", status_code=400, step="code", code=code[:16], error=exc.message)
        return _public(ctx, "register.html", status_code=400, step="details", code=row.code,
                       role=row.role, role_label=referrals.role_label(row.role),
                       issuer=row.issuer.full_name, error=exc.message,
                       username=data.get("username", "")[:32], full_name=data.get("full_name", "")[:120],
                       contact=data.get("contact", "")[:32])
    return flash_redirect("/login", f"Account '{user.username}' created. Sign in below.", "success")


# --- manage codes ----------------------------------------------------------

def _admin_uplines(ctx: Ctx) -> list[User]:
    """Every active Master Agent, Agent and Sub Agent the Admin may attach a new account to."""
    if not ctx.user.is_admin_tier:
        return []
    return list(ctx.db.scalars(
        select(User).where(User.role.in_(NETWORK_ROLES), User.status == Status.ACTIVE)
        .order_by(User.role, User.username)).all())


@router.get("/referrals")
async def referrals_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.REFERRALS_MANAGE)
    pager, rows = paginate(request, 100, lambda limit, offset: referrals.list_codes(
        ctx.db, ctx.user, limit=limit, offset=offset))
    return render(ctx, "referrals.html", active="referrals", pager=pager, rows=rows,
                  invite_roles=[(r, referrals.role_label(r)) for r in referrals.invite_roles(ctx.user)],
                  uplines=_admin_uplines(ctx),
                  role_names={r.value: r.label for r in Role},
                  default_ttl=int(ctx.store.get("referral.default_ttl_minutes")))


@router.post("/referrals/create")
async def referrals_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.REFERRALS_MANAGE)
    data = await form_data(request)
    try:
        amount = int(data.get("ttl_amount", "") or 0)
        unit = _UNITS[data.get("ttl_unit", "minutes")]
    except (ValueError, KeyError) as exc:
        raise ValidationError("Choose a valid timer.") from exc
    try:
        parent_id = int(data.get("parent_id") or 0) or None
    except ValueError as exc:
        raise ValidationError("Choose a valid upline.") from exc
    row = referrals.create_code(ctx.db, ctx.user, ttl_minutes=amount * unit, note=data.get("note"),
                                role=data.get("role") or None, parent_id=parent_id)
    return flash_redirect("/referrals", f"Code {row.code} created. It makes a {referrals.role_label(row.role)}. One use only.")


@router.post("/referrals/{code_id}/revoke")
async def referrals_revoke(code_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.REFERRALS_MANAGE)
    referrals.revoke(ctx.db, ctx.user, code_id)
    return flash_redirect("/referrals", "Code revoked.")
