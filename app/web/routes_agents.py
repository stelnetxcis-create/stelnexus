from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.core.errors import ValidationError
from app.core.rbac import Perm, creatable_roles
from app.db.models import UPLINE_ROLES, Role, Status, User
from app.services import agents
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


def _int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValidationError("Choose a valid upline.") from exc


def _uplines(ctx: Ctx, creatable: list[Role]) -> list[User]:
    """Accounts the Admin / a Pawn may attach a new Agent or Sub Agent to."""
    if ctx.user.role not in (Role.ADMIN, Role.BISHOP, Role.PAWN):
        return []
    wanted = {up for r in creatable if r.value in UPLINE_ROLES for up in UPLINE_ROLES[r.value]}
    if not wanted:
        return []
    return list(ctx.db.scalars(select(User).where(User.role.in_(wanted), User.status == Status.ACTIVE)
                               .order_by(User.role, User.username)).all())


@router.post("/agents/create")
async def agents_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENTS_CREATE)
    data = await form_data(request)
    try:
        role = Role(data.get("role", ""))
    except ValueError as exc:
        raise ValidationError("Choose a valid user type.") from exc
    user = agents.create_agent(
        ctx.db, ctx.user, username=data.get("username", ""), password=data.get("password", ""),
        role=role, full_name=data.get("full_name", ""), contact=data.get("contact"),
        parent_id=_int(data.get("parent_id")), settings=ctx.store, gcash_number=data.get("gcash"),
    )
    return flash_redirect("/staff/people?tab=team", f"{user.role_label} '{user.username}' created. Ref code {user.ref_code}.")


@router.post("/agents/{agent_id}/update")
async def agents_update(agent_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENTS_EDIT)
    data = await form_data(request)
    status = data.get("status") or None
    agents.update_agent(
        ctx.db, ctx.user, agent_id, full_name=data.get("full_name") or None,
        contact=data.get("contact"), status=Status(status) if status else None,
        reason=data.get("reason"),
    )
    return flash_redirect("/staff/people?tab=team", "Agent updated.")


@router.post("/agents/{agent_id}/password")
async def agents_password(agent_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENTS_EDIT)
    data = await form_data(request)
    agents.reset_password(ctx.db, ctx.user, agent_id, data.get("password", ""), settings=ctx.store)
    return flash_redirect("/staff/people?tab=team", "Password reset. The agent must change it at next sign-in.")
