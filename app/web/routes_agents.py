from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import CHILD_ROLE, Perm, scope_ids
from app.db.models import Role, Status, User
from app.services import agents
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


def _creatable_roles(user: User) -> list[Role]:
    if user.role == Role.ADMIN:
        return [Role.MASTER, Role.ADMIN, Role.TELLER, Role.DECLARATOR]
    child = CHILD_ROLE.get(Role(user.role))
    return [child] if child else []


@router.get("/agents")
async def agents_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENTS_VIEW)
    search = request.query_params.get("q", "")[:60]
    rows = agents.list_agents(ctx.db, ctx.user, search=search or None)
    wallets = {row.id: agents.wallet_balance(ctx.db, row) for row in rows}
    commissions = {row.id: agents.commission_balance(ctx.db, row) for row in rows}
    return render(ctx, "agents.html", active="agents", rows=rows, wallets=wallets,
                  commissions=commissions, search=search, creatable=_creatable_roles(ctx.user),
                  my_wallet=agents.wallet_balance(ctx.db, ctx.user),
                  downline=sorted(scope_ids(ctx.db, ctx.user) - {ctx.user.id}))


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
        settings=ctx.store,
    )
    return flash_redirect("/agents", f"{user.role_label} '{user.username}' created. Ref code {user.ref_code}.")


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
    return flash_redirect("/agents", "Agent updated.")


@router.post("/agents/{agent_id}/password")
async def agents_password(agent_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENTS_EDIT)
    data = await form_data(request)
    agents.reset_password(ctx.db, ctx.user, agent_id, data.get("password", ""), settings=ctx.store)
    return flash_redirect("/agents", "Password reset. The agent must change it at next sign-in.")


@router.post("/agents/transfer")
async def agents_transfer(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENTS_TRANSFER)
    data = await form_data(request)
    try:
        target_id = int(data.get("target_id", "0"))
    except ValueError as exc:
        raise ValidationError("Choose a target agent.") from exc
    amount = to_cents(data.get("amount", "0"))
    agents.transfer_credit(ctx.db, ctx.user, target_id=target_id, amount_cents=amount,
                           settings=ctx.store, note=data.get("note"),
                           idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect("/agents", "Load credit transferred.")
