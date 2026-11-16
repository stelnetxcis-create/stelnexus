"""The Admin Panel: one place for everything the Admin controls.

    /admin              overview
    /admin/variables    every business variable (was Settings)
    /admin/locations    the venue registry
    /admin/cuts         Volume % / SOP % (was the Cuts page)
    /admin/pawns        create Pawns and choose what each may see and change (Admin only)
    /admin/audit        audit trail and ledger integrity

The Admin sees all of it. A Pawn sees only the sections the Admin switched on for that account
(``User.grants``), and inside Variables only the groups granted. Master has no access here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.core import audit
from app.core.errors import NotFound
from app.core.rbac import (GRANTS, Perm, normalize_grants, variable_groups_editable,
                           variable_groups_visible)
from app.core.settings_registry import BY_KEY, DEFS, GROUPS
from app.db.models import AuditLog, Event, EventStatus, Location, Role, Status, User
from app.services import agents, locations, presence, reports
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()

SECTIONS = (
    # key, url, label, perm
    ("overview", "/admin", "Overview", Perm.PANEL_VIEW),
    ("variables", "/admin/variables", "Variables", Perm.SETTINGS_VIEW),
    ("locations", "/admin/locations", "Locations", Perm.LOCATIONS_VIEW),
    ("cuts", "/admin/cuts", "Cuts", Perm.CUTS_VIEW),
    ("pawns", "/admin/pawns", "Pawns", Perm.PAWNS_MANAGE),
    ("audit", "/admin/audit", "Audit trail", Perm.AUDIT_VIEW),
)


def panel(ctx: Ctx, section: str, template: str, **extra):
    """Render an Admin Panel page with its section tabs (only the ones this account may open)."""
    tabs = [(key, url, label) for key, url, label, perm in SECTIONS if ctx.can(perm)]
    return render(ctx, template, active="admin", panel_tabs=tabs, panel_active=section, **extra)


def _multi(request_form, name: str) -> list[str]:
    return [str(v) for v in request_form.getlist(name)]


# --------------------------------------------------------------------------- overview

@router.get("/admin")
async def admin_home(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PANEL_VIEW)
    db = ctx.db
    cards = []
    if ctx.can(Perm.ONLINE_VIEW):
        online = presence.online_accounts(db, ctx.user, ctx.store)
        cards.append(("Online now", len(online), "accounts signed in", "/clp/online"))
    if ctx.can(Perm.LOCATIONS_VIEW):
        n = int(db.scalar(select(func.count(Location.id)).where(Location.is_active.is_(True))) or 0)
        cards.append(("Locations", n, "active venues", "/admin/locations"))
    open_events = int(db.scalar(select(func.count(Event.id)).where(Event.status == EventStatus.OPEN)) or 0)
    cards.append(("Open events", open_events, "taking bets", "/events"))
    if ctx.can(Perm.PAWNS_MANAGE):
        n = int(db.scalar(select(func.count(User.id)).where(User.role == Role.PAWN)) or 0)
        cards.append(("Pawns", n, "spectator accounts", "/admin/pawns"))
    recent = []
    if ctx.can(Perm.AUDIT_VIEW):
        recent = list(db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(8)).all())
    return panel(ctx, "overview", "admin_home.html", cards=cards, recent=recent,
                 withdrawals_on=bool(ctx.store.get("wallet.withdrawals_enabled")),
                 is_pawn=ctx.user.role == Role.PAWN)


# --------------------------------------------------------------------------- variables

@router.get("/admin/variables")
async def variables_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.SETTINGS_VIEW)
    visible = variable_groups_visible(ctx.user)
    editable = variable_groups_editable(ctx.user)
    grouped = {g: [d for d in DEFS if d.group == g] for g in GROUPS if g in visible}
    return panel(ctx, "variables", "admin_variables.html", grouped=grouped, values=ctx.store.all(),
                 editable=editable)


@router.post("/admin/variables")
async def variables_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.SETTINGS_MANAGE)
    editable = variable_groups_editable(ctx.user)
    data = await form_data(request)
    changed = []
    for key, definition in BY_KEY.items():
        if definition.group not in editable:
            continue                       # a Pawn can only touch the groups it was given
        if key not in data:
            if definition.kind == "bool":
                raw = "false"              # unchecked checkbox
            else:
                continue
        else:
            raw = data[key]
        old, new = ctx.store.set(key, raw, ctx.user.id)
        if old != new:
            changed.append(key)
            audit.record(ctx.db, ctx.user, "settings.update", entity_type="setting", entity_id=key,
                         before={"value": old}, after={"value": new})
    return flash_redirect("/admin/variables",
                          f"{len(changed)} variable(s) updated." if changed else "No changes.")


# --------------------------------------------------------------------------- locations

def _location_form(form) -> dict:
    return dict(
        name=form.get("name", ""), address=form.get("address"), city=form.get("city"),
        notes=form.get("notes"), image=form.get("image", ""),
        custom_fields=locations.parse_custom_fields(_multi(form, "cf_label"), _multi(form, "cf_value")),
        default_fight_count=form.get("default_fight_count"),
        default_payout_model=form.get("default_payout_model"),
        default_house_cut_bps=form.get("default_house_cut_bps"),
    )


@router.get("/admin/locations")
async def locations_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.LOCATIONS_VIEW)
    return panel(ctx, "locations", "admin_locations.html",
                 rows=locations.list_locations(ctx.db), counts=locations.event_counts(ctx.db),
                 can_manage=ctx.can(Perm.LOCATIONS_MANAGE),
                 max_fields=locations.MAX_CUSTOM_FIELDS, venue_images=locations.VENUE_IMAGES)


@router.post("/admin/locations/create")
async def locations_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.LOCATIONS_MANAGE)
    form = await request.form()
    row = locations.create_location(ctx.db, ctx.user, **_location_form(form))
    return flash_redirect("/admin/locations", f"Location '{row.name}' registered.")


@router.post("/admin/locations/{location_id}/update")
async def locations_update(location_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.LOCATIONS_MANAGE)
    form = await request.form()
    row = locations.update_location(ctx.db, ctx.user, location_id, **_location_form(form))
    return flash_redirect("/admin/locations", f"Location '{row.name}' saved.")


@router.post("/admin/locations/{location_id}/archive")
async def locations_archive(location_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.LOCATIONS_MANAGE)
    row = locations.set_active(ctx.db, ctx.user, location_id, False)
    return flash_redirect("/admin/locations", f"'{row.name}' archived. It no longer shows in the event form.")


@router.post("/admin/locations/{location_id}/restore")
async def locations_restore(location_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.LOCATIONS_MANAGE)
    row = locations.set_active(ctx.db, ctx.user, location_id, True)
    return flash_redirect("/admin/locations", f"'{row.name}' is active again.")


@router.post("/admin/locations/{location_id}/delete")
async def locations_delete(location_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.LOCATIONS_MANAGE)
    locations.delete_location(ctx.db, ctx.user, location_id)
    return flash_redirect("/admin/locations", "Location deleted.")


# --------------------------------------------------------------------------- pawns (Admin only)

@router.get("/admin/pawns")
async def pawns_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    sections: dict[str, list] = {}
    for grant in GRANTS:
        sections.setdefault(grant.section, []).append(grant)
    return panel(ctx, "pawns", "admin_pawns.html", pawns=agents.list_pawns(ctx.db), sections=sections,
                 min_len=int(ctx.store.get("security.password_min_length")))


@router.post("/admin/pawns/create")
async def pawns_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    pawn = agents.create_agent(
        ctx.db, ctx.user, username=form.get("username", ""), password=form.get("password", ""),
        role=Role.PAWN, full_name=form.get("full_name", ""), contact=form.get("contact"),
        settings=ctx.store)
    agents.set_pawn_grants(ctx.db, ctx.user, pawn.id, _multi(form, "grant"))
    return flash_redirect("/admin/pawns", f"Pawn '{pawn.username}' created. They must change the password at first sign-in.")


@router.post("/admin/pawns/{pawn_id}/grants")
async def pawns_grants(pawn_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    pawn = agents.set_pawn_grants(ctx.db, ctx.user, pawn_id, _multi(form, "grant"))
    return flash_redirect("/admin/pawns", f"Access updated for {pawn.username}. It applies on their next click.")


@router.post("/admin/pawns/{pawn_id}/status")
async def pawns_status(pawn_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    data = await form_data(request)
    pawn = ctx.db.get(User, pawn_id)
    if pawn is None or pawn.role != Role.PAWN:
        raise NotFound("Pawn not found.")
    agents.update_agent(ctx.db, ctx.user, pawn_id, status=Status(data.get("status", "active")),
                        reason=data.get("reason"))
    return flash_redirect("/admin/pawns", f"{pawn.username} is now {data.get('status', 'active')}.")


@router.post("/admin/pawns/{pawn_id}/password")
async def pawns_password(pawn_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    data = await form_data(request)
    pawn = ctx.db.get(User, pawn_id)
    if pawn is None or pawn.role != Role.PAWN:
        raise NotFound("Pawn not found.")
    agents.reset_password(ctx.db, ctx.user, pawn_id, data.get("password", ""), settings=ctx.store)
    return flash_redirect("/admin/pawns", f"Password reset for {pawn.username}.")


# --------------------------------------------------------------------------- audit & integrity

@router.get("/admin/audit")
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
    return panel(ctx, "audit", "audit.html", rows=rows, filters={"action": action, "entity": entity})


@router.get("/admin/integrity")
async def integrity_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AUDIT_VIEW)
    return panel(ctx, "audit", "integrity.html", report=reports.integrity(ctx.db))


# --------------------------------------------------------------------------- old addresses

@router.get("/settings", include_in_schema=False)
async def legacy_settings(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.SETTINGS_VIEW)
    return flash_redirect("/admin/variables")


@router.get("/audit", include_in_schema=False)
async def legacy_audit(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AUDIT_VIEW)
    return flash_redirect("/admin/audit")


@router.get("/integrity", include_in_schema=False)
async def legacy_integrity(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AUDIT_VIEW)
    return flash_redirect("/admin/integrity")
