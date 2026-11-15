from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.core import audit
from app.core.errors import ValidationError
from app.core.rbac import Perm
from app.core.security import hash_password, random_code
from app.core.settings_registry import BY_KEY, DEFS, GROUPS
from app.db.models import AuditLog, Role, Station, Status, User
from app.services import accounts, reports
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


@router.get("/settings")
async def settings_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.SETTINGS_VIEW)
    values = ctx.store.all()
    grouped = {group: [d for d in DEFS if d.group == group] for group in GROUPS}
    return render(ctx, "settings.html", active="settings", grouped=grouped, values=values)


@router.post("/settings")
async def settings_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.SETTINGS_MANAGE)
    data = await form_data(request)
    changed = []
    for key, definition in BY_KEY.items():
        if key not in data:
            if definition.kind == "bool":
                raw = "false"          # unchecked checkbox
            else:
                continue
        else:
            raw = data[key]
        old, new = ctx.store.set(key, raw, ctx.user.id)
        if old != new:
            changed.append(key)
            audit.record(ctx.db, ctx.user, "settings.update", entity_type="setting", entity_id=key,
                         before={"value": old}, after={"value": new})
    return flash_redirect("/settings", f"{len(changed)} setting(s) updated." if changed else "No changes.")


@router.get("/audit")
async def audit_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AUDIT_VIEW)
    params = request.query_params
    stmt = select(AuditLog)
    action = params.get("action", "")[:48]
    entity = params.get("entity", "")[:32]
    if action:
        stmt = stmt.where(AuditLog.action.ilike(f"%{action}%"))
    if entity:
        stmt = stmt.where(AuditLog.entity_type == entity)
    rows = list(ctx.db.scalars(stmt.order_by(AuditLog.id.desc()).limit(300)).all())
    return render(ctx, "audit.html", active="audit", rows=rows,
                  filters={"action": action, "entity": entity})


@router.get("/stations")
async def stations_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STATIONS_VIEW)
    return render(ctx, "stations.html", active="stations", rows=reports.stations(ctx.db))


@router.post("/stations/create")
async def stations_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STATIONS_MANAGE)
    data = await form_data(request)
    code = (data.get("code") or "").strip().upper()[:24]
    name = (data.get("name") or "").strip()[:80]
    if not code or not name:
        raise ValidationError("Station code and name are required.")
    if ctx.db.scalar(select(Station.id).where(Station.code == code)):
        raise ValidationError("That station code already exists.")

    username = (data.get("username") or "").strip()
    teller = None
    if username:
        if ctx.db.scalar(select(User.id).where(User.username == username)):
            raise ValidationError("That teller username already exists.")
        password = data.get("password") or ""
        from app.core.security import password_problems
        problems = password_problems(password, username)
        if problems:
            raise ValidationError(" ".join(problems))
        teller = User(username=username, password_hash=hash_password(password), role=Role.TELLER,
                      full_name=name, ref_code=random_code(10), status=Status.ACTIVE,
                      must_change_password=True, created_by_id=ctx.user.id)
        ctx.db.add(teller)
        ctx.db.flush()
        accounts.wallet_for_user(ctx.db, teller)

    station = Station(code=code, name=name, location=(data.get("location") or "")[:120] or None,
                      user_id=teller.id if teller else None, is_active=True)
    ctx.db.add(station)
    ctx.db.flush()
    accounts.cash_for_station(ctx.db, station)
    audit.record(ctx.db, ctx.user, "station.create", entity_type="station", entity_id=station.id,
                 after={"code": code, "name": name, "teller": username or None})
    return flash_redirect("/stations", f"Station {code} created.")


@router.get("/integrity")
async def integrity_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AUDIT_VIEW)
    return render(ctx, "integrity.html", active="audit", report=reports.integrity(ctx.db))
