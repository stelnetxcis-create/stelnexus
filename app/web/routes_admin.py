"""The Admin Panel: one place for everything the Admin controls.

    /admin              overview
    /admin/variables    every business variable (was Settings)
    /admin/locations    the venue registry
    /admin/cuts         Volume % / SOP % (was the Cuts page)
    /admin/access       Access: the Dev / Staff / Users trees, role defaults, account types (Admin only; see routes_access)
    /admin/audit        audit trail and ledger integrity

The Admin sees all of it. A Pawn sees only the sections the Admin switched on for that account
(``User.grants``), and inside Variables only the groups granted. Master has no access here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.core import audit
from app.core.errors import NotFound, ValidationError
from app.core.rbac import (GRANTS, Perm, normalize_grants, offered_grants, variable_groups_editable,
                           variable_groups_visible)
from app.core.settings_registry import BY_KEY, DEFS, GROUPS
from app.db.models import AuditLog, Event, EventStatus, Location, Role, Status, User
from app.services import account_admin, agents, bola, ipbans, locations, presence, reports, teams
from app.web.paging import paginate
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()

SECTIONS = (
    # key, url, label, perm
    ("overview", "/admin", "Overview", Perm.PANEL_VIEW),
    ("variables", "/admin/variables", "Variables", Perm.SETTINGS_VIEW),
    ("locations", "/admin/locations", "Locations", Perm.LOCATIONS_VIEW),
    ("teams", "/admin/teams", "Teams", Perm.TEAMS_VIEW),
    ("bola", "/admin/bola/schedules", "Bola draw times", Perm.BOLA_MANAGE),
    ("cuts", "/admin/cuts", "Cuts", Perm.CUTS_VIEW),
    ("accounts", "/admin/accounts", "Accounts", Perm.ACCOUNTS_VIEW),
    ("streams", "/admin/streams", "Streams", Perm.STREAMS_VIEW),
    ("ipbans", "/admin/ipbans", "IP bans", Perm.IPBANS_MANAGE),
    ("access", "/admin/access", "Access", Perm.PAWNS_MANAGE),
    ("audit", "/admin/audit", "Audit trail", Perm.AUDIT_VIEW),
)


def panel(ctx: Ctx, section: str, template: str, **extra):
    """Render an Admin Panel page with its section tabs (only the ones this account may open)."""
    tabs = [(key, url, label) for key, url, label, perm in SECTIONS if ctx.can(perm)]
    if any(key == "access" for key, _u, _l in tabs):               # a live count of requests waiting for this admin
        from app.services import access as _access
        waiting = _access.pending_count(ctx.db, ctx.user)
        if waiting:
            tabs = [(k, u, f"{l} · {waiting}" if k == "access" else l) for k, u, l in tabs]
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
    cards.append(("Open events", open_events, "taking bets", "/games/sabong/schedule"))
    if ctx.can(Perm.STAFF_TELLER):
        from app.services import gcash as _gcash
        live = sum(1 for a in _gcash.list_accounts(db) if a.is_active)
        cards.append(("GCash pool", live, "numbers in rotation", "/staff/teller"))
    if ctx.can(Perm.PAWNS_MANAGE):
        n = int(db.scalar(select(func.count(User.id)).where(User.role == Role.PAWN)) or 0)
        cards.append(("Pawns", n, "spectator accounts", "/admin/access/dev/pawn"))
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
        bola_limit_per_line=form.get("bola_limit_per_line"),
        bola_max_liability_per_outcome=form.get("bola_max_liability_per_outcome"),
        bola_max_liability_per_draw=form.get("bola_max_liability_per_draw"),
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


# --------------------------------------------------------------------------- teams
# The sports team catalog (NBA first). Registry only: no game or bet uses a team yet.

@router.get("/admin/teams")
async def teams_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.TEAMS_VIEW)
    rows = teams.list_teams(ctx.db, "nba")
    return panel(ctx, "teams", "admin_teams.html", team_groups=teams.grouped(rows), total=len(rows),
                 on_count=sum(1 for t in rows if t.is_active), can_manage=ctx.can(Perm.TEAMS_MANAGE))


@router.post("/admin/teams/{team_id}/{action}")
async def teams_toggle(team_id: int, action: str, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.TEAMS_MANAGE)
    if action not in ("activate", "deactivate"):
        raise NotFound("Unknown action.")
    row = teams.set_active(ctx.db, ctx.user, team_id, action == "activate")
    return flash_redirect("/admin/teams", f"{row.full_name} is {'on' if row.is_active else 'off'}.")


# --------------------------------------------------------------------------- bola draw times
# The old tbl_area.ball_1/2/3: up to six draw slots per area, in Manila local time.
# Master and Admin hold BOLA_MANAGE, so both can edit schedules here.

@router.get("/admin/bola/schedules")
async def bola_schedules_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    rows = bola.schedules(ctx.db)
    by_area: dict[int, list] = {}
    for row in rows:
        by_area.setdefault(row.location_id, []).append(row)
    return panel(ctx, "bola", "admin_bola_schedules.html",
                 areas=locations.list_locations(ctx.db), by_area=by_area,
                 enabled=bool(ctx.store.get("bola.enabled")),
                 cutoff_minutes=int(ctx.store.get("bola.cutoff_minutes")))


@router.post("/admin/bola/schedules/save")
async def bola_schedules_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    data = await form_data(request)
    try:
        location_id, slot = int(data.get("location_id") or 0), int(data.get("slot") or 0)
    except (TypeError, ValueError) as exc:
        from app.core.errors import ValidationError
        raise ValidationError("Choose an area and a slot number.") from exc
    row = bola.upsert_schedule(
        ctx.db, ctx.user, location_id=location_id, slot=slot,
        draw_time=data.get("draw_time", ""), is_active=data.get("is_active") == "1")
    return flash_redirect("/admin/bola/schedules",
                          f"Slot {row.slot} saved for {row.location.name}.")


@router.post("/admin/bola/schedules/{schedule_id}/delete")
async def bola_schedules_delete(schedule_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.BOLA_MANAGE)
    bola.delete_schedule(ctx.db, ctx.user, schedule_id)
    return flash_redirect("/admin/bola/schedules", "Draw time removed. Existing draws are kept.")


# --------------------------------------------------------------------------- pawns (Admin only)

@router.post("/admin/pawns/create")
async def pawns_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    pawn = agents.create_agent(
        ctx.db, ctx.user, username=form.get("username", ""), password=form.get("password", ""),
        role=Role.PAWN, full_name=form.get("full_name", ""), contact=form.get("contact"),
        settings=ctx.store)
    agents.set_pawn_grants(ctx.db, ctx.user, pawn.id, _multi(form, "grant"))
    return flash_redirect("/admin/access/dev/pawn", f"Pawn '{pawn.username}' created. They must change the password at first sign-in.")


@router.post("/admin/pawns/{pawn_id}/grants")
async def pawns_grants(pawn_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    pawn = agents.set_pawn_grants(ctx.db, ctx.user, pawn_id, _multi(form, "grant"))
    return flash_redirect("/admin/access/dev/pawn", f"Access updated for {pawn.username}. It applies on their next click.")


@router.post("/admin/pawns/{pawn_id}/status")
async def pawns_status(pawn_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    data = await form_data(request)
    pawn = ctx.db.get(User, pawn_id)
    if pawn is None or pawn.role != Role.PAWN:
        raise NotFound("Pawn not found.")
    agents.update_agent(ctx.db, ctx.user, pawn_id, status=Status(data.get("status", "active")),
                        reason=data.get("reason"))
    return flash_redirect("/admin/access/dev/pawn", f"{pawn.username} is now {data.get('status', 'active')}.")


@router.post("/admin/pawns/{pawn_id}/password")
async def pawns_password(pawn_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    data = await form_data(request)
    pawn = ctx.db.get(User, pawn_id)
    if pawn is None or pawn.role != Role.PAWN:
        raise NotFound("Pawn not found.")
    agents.reset_password(ctx.db, ctx.user, pawn_id, data.get("password", ""), settings=ctx.store)
    return flash_redirect("/admin/access/dev/pawn", f"Password reset for {pawn.username}.")


# --------------------------------------------------------------------------- accounts

@router.get("/admin/accounts")
async def accounts_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ACCOUNTS_VIEW)
    q = request.query_params
    kind = q.get("kind") if q.get("kind") in ("all", "users", "players") else "all"
    from app.web.paging import make_pager, page_number
    per = 100
    upage, ppage = page_number(request, "upage"), page_number(request, "ppage")
    found = account_admin.list_accounts(ctx.db, search=q.get("q", "")[:60] or None, kind=kind,
                                        include_removed=q.get("removed") == "1", limit=per + 1,
                                        user_offset=upage * per, player_offset=ppage * per)
    viewer_is_admin = ctx.user.is_admin_tier
    # Pawns are the Admin's to manage; a Pawn never even sees the controls for another Pawn.
    users_pager = make_pager(request, per, upage, min(len(found["users"]), per),
                             len(found["users"]) > per, "upage")
    players_pager = make_pager(request, per, ppage, min(len(found["players"]), per),
                               len(found["players"]) > per, "ppage")
    users = [u for u in found["users"][:per] if viewer_is_admin or u.role != Role.PAWN]
    return panel(ctx, "accounts", "admin_accounts.html", users=users, players=found["players"][:per],
                 users_pager=users_pager, players_pager=players_pager,
                 search=q.get("q", "")[:60], kind=kind, removed=q.get("removed") == "1",
                 can_edit=ctx.can(Perm.ACCOUNTS_EDIT), can_remove=ctx.can(Perm.ACCOUNTS_REMOVE),
                 me=ctx.user.id)


@router.post("/admin/accounts/user/{user_id}/edit")
async def accounts_user_edit(user_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ACCOUNTS_EDIT)
    data = await form_data(request)
    user = account_admin.edit_user(ctx.db, ctx.user, user_id, username=data.get("username"),
                                   full_name=data.get("full_name"), contact=data.get("contact", ""),
                                   reason=data.get("reason"))
    return flash_redirect("/admin/accounts", f"Saved {user.username}.")


@router.post("/admin/accounts/user/{user_id}/remove")
async def accounts_user_remove(user_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ACCOUNTS_REMOVE)
    data = await form_data(request)
    user = account_admin.remove_user(ctx.db, ctx.user, user_id, reason=data.get("reason", ""))
    return flash_redirect("/admin/accounts", f"{user.username} was removed and signed out.")


@router.post("/admin/accounts/player/{player_id}/edit")
async def accounts_player_edit(player_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ACCOUNTS_EDIT)
    data = await form_data(request)
    player = account_admin.edit_player(
        ctx.db, ctx.user, player_id, username=data.get("username"), full_name=data.get("full_name"),
        gcash_number=data.get("gcash") or None, address=data.get("address") or None,
        notes=data.get("notes", ""), reason=data.get("reason"))
    return flash_redirect("/admin/accounts?kind=players", f"Saved {player.full_name}.")


@router.post("/admin/accounts/player/{player_id}/remove")
async def accounts_player_remove(player_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ACCOUNTS_REMOVE)
    data = await form_data(request)
    player = account_admin.remove_player(ctx.db, ctx.user, player_id, reason=data.get("reason", ""))
    return flash_redirect("/admin/accounts?kind=players", f"{player.full_name} was removed.")


# --------------------------------------------------------------------------- IP bans

@router.get("/admin/ipbans")
async def ipbans_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.IPBANS_MANAGE)
    pager, bans = paginate(request, 100, lambda limit, offset: ipbans.list_bans(
        ctx.db, limit=limit, offset=offset))
    return panel(ctx, "ipbans", "admin_ipbans.html", bans=bans, pager=pager,
                 my_ip=ctx.request.scope.get("state", {}).get("client_ip"))


@router.post("/admin/ipbans/create")
async def ipbans_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.IPBANS_MANAGE)
    data = await form_data(request)
    hours = data.get("hours")
    try:
        hours = int(hours) if hours else None
    except ValueError as exc:
        raise ValidationError("Duration must be a whole number of hours.") from exc
    row = ipbans.ban(ctx.db, ctx.user, network=data.get("network", ""), reason=data.get("reason", ""),
                     hours=hours, actor_ip=ctx.request.scope.get("state", {}).get("client_ip"))
    return flash_redirect("/admin/ipbans", f"{row.network} is now blocked.")


@router.post("/admin/ipbans/{ban_id}/delete")
async def ipbans_delete(ban_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.IPBANS_MANAGE)
    row = ipbans.unban(ctx.db, ctx.user, ban_id)
    return flash_redirect("/admin/ipbans", f"{row.network} is unblocked.")


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
    pager, rows = paginate(request, 300, lambda limit, offset: ctx.db.scalars(
        stmt.order_by(AuditLog.id.desc()).limit(limit).offset(offset)).all())
    return panel(ctx, "audit", "audit.html", rows=rows, pager=pager,
                 filters={"action": action, "entity": entity})


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
