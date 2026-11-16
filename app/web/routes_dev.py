"""DEV tools: only the hidden Admin. Everything here acts as the real Admin (see deps._apply_view_as).

View-as lets the Admin look at the console exactly as another account sees it -- same menu, same
dashboard, same data scope -- without knowing their password. It is read-only: while it is active
every non-GET request is refused, so a preview can never change anything."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.errors import Forbidden
from app.db.models import Role, Status, User
from app.web.deps import (
    VIEWAS_COOKIE, VIEWAS_MAX_AGE, Ctx, audit_action, flash_redirect, form_data, home_for,
    make_viewas_cookie_value, require_ctx,
)
from app.core.config import get_settings
from app.core.middleware import cookie_header

router = APIRouter(prefix="/dev")
_cfg = get_settings()
_PREVIEWABLE = (Role.MASTER, Role.CAVO, Role.KUBRADOR, Role.DECLARATOR, Role.PAWN)


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
