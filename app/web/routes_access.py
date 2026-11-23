"""Admin Panel -> Access: every account type and what it may do, in three trees, plus asking first.

    /admin/access                          overview of the Dev, Staff and Users trees
    /admin/access/{tree}/{kind}            one account type: its default rights and its accounts
                                           (kind = a role such as ``agent`` / ``player``, or ``t12`` for a custom type)
    /admin/access/account/{id}             one account: its type and its own rights
    /admin/access/matrix                   every role side by side
    /admin/access/types/...                create, rename and delete custom account types
    /admin/access/requests                 rights people asked for, waiting for the King or a Bishop
    /admin/access/king                     the King's desk: emergency switches, sealed rights, approval policy (King only)
    /admin/access/bishops/...              appoint and manage Bishops (King only)
    /access/ask, /access/mine              anyone: ask for access, see and run what was approved

Every right can be Yes, No, Ask first, Temporary, Hours or Limit (app.core.rules). Rights resolve as: role defaults ->
Pawn switches -> account type -> the account's own settings -> a running pass -> the King's ceilings (sealed, frozen).

What only the King may see (the King account, Bishops, the King desk) answers "not found" to everyone else, so a Bishop
can never tell a King exists.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.core import rbac
from app.core import rules as rules_mod
from app.core.access_tree import (KIND_BLURB, KIND_ICON, KIND_LABEL, TREE_BY_KEY, TREES, TYPE_BASES, Tree,
                                  kinds_visible_to, tree_of)
from app.core.errors import NotFound, ValidationError
from app.core.rbac import BOLA_PERMS, KING_ONLY, NON_DELEGABLE, PERM_CATALOG, PERM_LABELS, Perm, VIEW_PERMS, offered_grants
from app.core.settings_registry import GROUPS
from app.db.models import AccessRequest, AccountProfile, Player, Role, Status, User
from app.services import access, agents
from app.web.deps import Ctx, flash_redirect, render, require_ctx
from app.web.routes_admin import panel

router = APIRouter()
SECTION = "access"
_ROLE_COLUMNS = ("bishop", "master", "agent", "sub_agent", "teller", "declarator", "pawn", "player")
MEMBER_LIMIT = 200
_MODE_RADIOS = ("deny", "allow", "ask", "temp", "window", "limit")


# --------------------------------------------------------------------------- who sees what

def _king(ctx: Ctx) -> bool:
    return ctx.user.role == Role.ADMIN


def _king_only(ctx: Ctx) -> None:
    """King-only pages answer exactly like a missing page for everyone else."""
    if not _king(ctx):
        raise NotFound("That page does not exist.")


def _catalog_for(ctx: Ctx):
    """The rights list as this admin may see it: a Bishop never sees what the King sealed or kept for itself."""
    if _king(ctx):
        return PERM_CATALOG
    hidden = rbac.sealed_perms() | set(KING_ONLY)
    return tuple((cat, tuple((p, label) for p, label in items if p not in hidden)) for cat, items in PERM_CATALOG)


def _visible_trees(ctx: Ctx) -> list[dict]:
    out = []
    for tree in TREES:
        kinds = kinds_visible_to(tree, ctx.user)
        if kinds:
            out.append({"tree": tree, "kinds": kinds,
                        "tagline": tree.tagline if _king(ctx) else (tree.tagline_other or tree.tagline)})
    return out


@dataclass
class Kind:
    slug: str                  # URL part: a role value / "player" / "admin", or "t<id>" for a custom type
    label: str
    blurb: str
    tree: Tree
    base: str                  # the role value (or "player" / "admin") this kind is built on
    type: AccountProfile | None = None

    @property
    def is_custom(self) -> bool:
        return self.type is not None

    @property
    def icon(self) -> str:
        return KIND_ICON.get(self.base, "") if self.type is None else ""

    @property
    def href(self) -> str:
        return f"/admin/access/{self.tree.key}/{self.slug}"


def _builtin_kind(key: str) -> Kind:
    return Kind(key, KIND_LABEL[key], KIND_BLURB[key], tree_of(key), key)


def _custom_kind(row: AccountProfile) -> Kind:
    base_label = KIND_LABEL.get(row.base_role, row.base_role)
    return Kind(f"t{row.id}", row.name, row.description or f"A custom {base_label} type.", tree_of(row.base_role),
                row.base_role, row)


def _find_kind(ctx: Ctx, tree_key: str, slug: str) -> Kind:
    tree = TREE_BY_KEY.get(tree_key)
    if tree is None:
        raise NotFound("That group does not exist.")
    if slug in tree.kinds:
        if slug not in kinds_visible_to(tree, ctx.user):
            raise NotFound("That account type does not exist.")
        return _builtin_kind(slug)
    if slug.startswith("t") and slug[1:].isdigit():
        row = ctx.db.get(AccountProfile, int(slug[1:]))
        if row is not None and tree_of(row.base_role).key == tree.key and agents.can_manage_role(ctx.user, row.base_role):
            return _custom_kind(row)
    raise NotFound("That account type does not exist.")


def _kind_rights(kind: Kind, *, gated: bool = False) -> set[Perm]:
    """What every account of this kind may do by default (before Pawn switches and per-account settings)."""
    if kind.base == "admin":
        return set(Perm)
    if kind.type is not None:
        return rbac.type_permissions(kind.type.id, kind.base, gated=gated)
    return rbac.default_permissions_for_key(kind.base, gated=gated)


def _kind_state(kind: Kind) -> dict:
    if kind.base == "admin":
        return {p: ("allow", None) for p in Perm}
    if kind.type is not None:
        return rbac.type_state(kind.type.id, kind.base)
    return rbac.role_state(kind.base)


def _kind_baseline_state(kind: Kind) -> dict:
    """What a kind starts from: the code's built-in defaults, or (custom types) the base role's current state."""
    if kind.type is not None:
        return rbac.role_state(kind.base)
    if kind.base == "admin":
        return {p: ("allow", None) for p in Perm}
    base = rbac.builtin_player_perms() if kind.base == rbac.PLAYER_ROLE_KEY else rbac.builtin_permissions_for(kind.base, gated=False)
    return {p: ("allow", None) for p in base}


def _counts(db) -> tuple[dict[str, int], dict[int, int]]:
    by_role = {role.value: int(n) for role, n in db.execute(select(User.role, func.count(User.id)).group_by(User.role)).all()}
    by_role["player"] = int(db.scalar(select(func.count(Player.id))) or 0)
    by_type = {int(tid): int(n) for tid, n in db.execute(
        select(User.account_profile_id, func.count(User.id)).where(User.account_profile_id.is_not(None))
        .group_by(User.account_profile_id)).all()}
    return by_role, by_type


def _kinds_of_tree(ctx: Ctx, tree: Tree) -> list[Kind]:
    kinds = [_builtin_kind(k) for k in kinds_visible_to(tree, ctx.user)]
    rows = ctx.db.scalars(select(AccountProfile).order_by(AccountProfile.name)).all()
    kinds += [_custom_kind(r) for r in rows if tree_of(r.base_role).key == tree.key and agents.can_manage_role(ctx.user, r.base_role)]
    return kinds


def _members(by_role, by_type, kind: Kind) -> int:
    return by_type.get(kind.type.id, 0) if kind.type else by_role.get(kind.base, 0)


def _kinds_nav(ctx: Ctx, tree: Tree, active_slug: str) -> list[dict]:
    by_role, by_type = _counts(ctx.db)
    return [{"kind": k, "members": _members(by_role, by_type, k), "active": k.slug == active_slug}
            for k in _kinds_of_tree(ctx, tree)]


def _page(ctx: Ctx, template: str, **extra):
    waiting = access.pending_count(ctx.db, ctx.user)
    return panel(ctx, SECTION, template, trees=[t["tree"] for t in _visible_trees(ctx)], is_king=_king(ctx), waiting=waiting,
                 can_decide=ctx.can(Perm.APPROVALS_DECIDE), kind_labels=KIND_LABEL,
                 type_bases=[(k, KIND_LABEL[k], tree_of(k).label) for k in TYPE_BASES if agents.can_manage_role(ctx.user, k)],
                 **extra)


# --------------------------------------------------------------------------- the rights editor (forms <-> rules)

def rule_view(rule: dict | None) -> dict:
    """A rule as the form fields that edit it."""
    view = {"mode": (rule or {}).get("mode", ""), "text": rules_mod.describe(rule) if rule else ""}
    if not rule:
        return view
    tz = rules_mod.local_tz()

    def local(value):
        moment = rules_mod.parse_moment(value)
        return moment.astimezone(tz).strftime("%Y-%m-%dT%H:%M") if moment else ""
    view.update(tu=local(rule.get("until")), tf=local(rule.get("from")), ta=rule.get("after", "deny"),
                wd=[str(d) for d in rule.get("days", [])], ws=rule.get("start", "08:00"), we=rule.get("end", "17:00"),
                wo=rule.get("outside", "deny"), lm=rule.get("max", ""), lp=rule.get("per", "day"), lo=rule.get("over", "deny"))
    return view


def _local_moment(value: str, label: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=rules_mod.local_tz())
    except ValueError:
        raise ValidationError(f"Check the date and time for \u201c{label}\u201d.")


def parse_rights(form) -> dict:
    """Read the per-right controls: m:<perm> is the mode, the other fields hold its settings.

    Returns allow / deny lists, a rules dict, the rights set to Default, and `posted` (the rights that were in the
    form at all: anything else is left untouched)."""
    now = rules_mod.utcnow()
    out = {"allow": [], "deny": [], "rules": {}, "default": set(), "posted": set()}
    for perm in Perm:
        key = perm.value
        if f"m:{key}" not in form:
            continue
        out["posted"].add(key)
        mode = str(form.get(f"m:{key}", ""))
        label = PERM_LABELS.get(key, key)
        if mode == "allow":
            out["allow"].append(key)
        elif mode == "deny":
            out["deny"].append(key)
        elif mode == "default":
            out["default"].add(key)
        elif mode == "ask":
            out["rules"][key] = {"mode": "ask"}
        elif mode == "temp":
            quick, until, start = (str(form.get(f"{p}:{key}", "")).strip() for p in ("td", "tu", "tf"))
            if quick in rules_mod.DURATIONS:
                end = now + dt.timedelta(seconds=rules_mod.DURATIONS[quick])
            elif until:
                end = _local_moment(until, label)
            else:
                raise ValidationError(f"Pick how long \u201c{label}\u201d stays on (a quick choice or an end time).")
            rule = {"mode": "temp", "until": rules_mod.iso(end), "after": str(form.get(f"ta:{key}", "deny"))}
            if start:
                rule["from"] = rules_mod.iso(_local_moment(start, label))
            out["rules"][key] = rule
        elif mode == "window":
            out["rules"][key] = {"mode": "window", "days": [d for d in form.getlist(f"wd:{key}")],
                                 "start": str(form.get(f"ws:{key}", "")), "end": str(form.get(f"we:{key}", "")),
                                 "outside": str(form.get(f"wo:{key}", "deny"))}
        elif mode == "limit":
            out["rules"][key] = {"mode": "limit", "max": str(form.get(f"lm:{key}", "")), "per": str(form.get(f"lp:{key}", "day")),
                                 "over": str(form.get(f"lo:{key}", "deny"))}
    return out


def _mode_of_state(entry) -> str:
    kind, rule = entry
    return "allow" if kind == "allow" else ("deny" if kind == "deny" else rule["mode"])


def _editor_rows(ctx: Ctx, state: dict, baseline: dict, *, simple_nd: bool = False, layer: rbac.Layer | None = None,
                 result: rbac.Rights | None = None, inherited: set | None = None) -> dict:
    """Everything the template needs per right: the mode to show, its settings, whether it differs from the start,
    and why a row is locked."""
    mine = None if _king(ctx) else rbac.permissions_of(ctx.user)
    gated = rbac.bola_dev_only()
    rows = {}
    for perm in Perm:
        key = perm.value
        if layer is not None:
            if perm in layer.revokes:
                mode, rule = "deny", None
            elif perm in layer.rules:
                mode, rule = layer.rules[perm]["mode"], layer.rules[perm]
            elif perm in layer.grants:
                mode, rule = "allow", None
            else:
                mode, rule = "default", None
        else:
            entry = state.get(perm, ("deny", None))
            mode, rule = _mode_of_state(entry), entry[1]
        base_mode = _mode_of_state(baseline.get(perm, ("deny", None)))
        locked = None
        if perm in KING_ONLY:
            locked = "King only"
        elif gated and perm in BOLA_PERMS:
            locked = "Locked: Bola is dev-only"
        elif perm in NON_DELEGABLE and not simple_nd:
            locked = "Admin only"
        elif mine is not None and perm not in mine:
            locked = "Not yours to give"
        rows[key] = {"mode": mode, "rule": rule_view(rule), "diff": (layer is None and mode != base_mode), "locked": locked,
                     "simple": perm in NON_DELEGABLE, "askable": perm not in VIEW_PERMS,
                     "result": result.mode_of(perm) if result is not None else None,
                     "inherited": (perm in inherited) if inherited is not None else None}
    return rows


# --------------------------------------------------------------------------- overview

@router.get("/admin/access")
async def access_home(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    db = ctx.db
    by_role, by_type = _counts(db)
    groups = []
    for entry in _visible_trees(ctx):
        cards = []
        for kind in _kinds_of_tree(ctx, entry["tree"]):
            customised = (kind.type is not None) or (kind.base in rbac.role_overrides())
            cards.append({"kind": kind, "members": _members(by_role, by_type, kind), "rights": len(_kind_rights(kind)),
                          "customised": customised})
        groups.append({"tree": entry["tree"], "tagline": entry["tagline"], "cards": cards})
    visible_roles = [r.value for r in Role if _king(ctx) or r not in (Role.ADMIN, Role.BISHOP)]
    individual = sum(1 for g, r, ru in db.execute(select(User.perm_grants, User.perm_revokes, User.perm_rules)).all() if g or r or ru)
    return _page(ctx, "access_home.html", acc_tree="overview", tree_groups=groups, individual=individual,
                 custom_types=int(db.scalar(select(func.count(AccountProfile.id))) or 0),
                 roles_customised=len([k for k in rbac.role_overrides() if _king(ctx) or k != "bishop"]),
                 total_accounts=sum(n for k, n in by_role.items() if k in visible_roles or k == "player"),
                 frozen=[n for n, on in (("Pawns", rbac.flag("freeze_pawns")), ("Bishops", rbac.flag("freeze_bishops"))) if on and (_king(ctx) or n == "Pawns")],
                 paused=bool(rbac.flag("approvals_paused")))


# --------------------------------------------------------------------------- matrix (all roles side by side)

def _matrix_columns(ctx: Ctx):
    cols = []
    for key in _ROLE_COLUMNS:
        if not agents.can_manage_role(ctx.user, key):
            continue
        state = rbac.role_state(key)
        held = {p.value for p in rbac.default_permissions_for_key(key, gated=False)}
        builtin = {p.value for p in _kind_baseline_state(_builtin_kind(key))}
        ruled = {p.value: rules_mod.MODE_GLYPH.get(r["mode"], "?") for p, (k, r) in state.items() if k == "rule"}
        cols.append((key, KIND_LABEL[key], held, builtin, ruled))
    return cols


@router.get("/admin/access/matrix")
async def access_matrix(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    locked = {p.value for p in NON_DELEGABLE} | {p.value for p in KING_ONLY} | ({p.value for p in BOLA_PERMS} if rbac.bola_dev_only() else set())
    return _page(ctx, "access_matrix.html", acc_tree="matrix", catalog=_catalog_for(ctx), roles=_matrix_columns(ctx), locked=locked,
                 non_delegable={p.value for p in NON_DELEGABLE}, bola_locked=rbac.bola_dev_only(),
                 role_tree={k: tree_of(k).key for k in _ROLE_COLUMNS})


@router.post("/admin/access/matrix")
async def access_matrix_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    changed = []
    for key in agents.EDITABLE_DEFAULT_ROLES:
        if not agents.can_manage_role(ctx.user, key):
            continue
        # An unchecked box is not sent, so a cell only counts as "off" when the page said it was there (rr:...).
        # Cells with rules, locked cells and rights a Bishop does not hold are never in `shown`: they stay as they are.
        shown = {p.value for p in Perm if f"rr:{key}:{p.value}" in form}
        wanted = [p.value for p in Perm if str(form.get(f"r:{key}:{p.value}", "")) == "on"]
        if agents.set_role_defaults(ctx.db, ctx.user, key, wanted, None, keep={p.value for p in Perm} - shown):
            changed.append(KIND_LABEL[key])
    msg = ("Defaults updated for " + ", ".join(changed) + ". Applies to everyone of those types within seconds."
           if changed else "No changes.")
    return flash_redirect("/admin/access/matrix", msg)


# --------------------------------------------------------------------------- one account

def _account_kind(ctx: Ctx, target: User) -> Kind:
    if target.account_profile_id is not None:
        row = ctx.db.get(AccountProfile, target.account_profile_id)
        if row is not None:
            return _custom_kind(row)
    return _builtin_kind(target.role.value)


def _own_account(ctx: Ctx, user_id: int) -> User:
    target = ctx.db.get(User, user_id)
    if target is None or target.role == Role.ADMIN or (target.role == Role.BISHOP and not _king(ctx)):
        raise NotFound("Account not found.")
    return target


@router.get("/admin/access/account/{user_id}")
async def access_account(user_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    target = _own_account(ctx, user_id)
    kind = _builtin_kind(target.role.value)
    own_kind = _account_kind(ctx, target)
    types = [_custom_kind(r) for r in ctx.db.scalars(select(AccountProfile).where(AccountProfile.base_role == target.role.value)
                                                      .order_by(AccountProfile.name)).all()]
    base_state = rbac._account_state(target, with_account=False)
    rights = rbac.rights_of(target)
    rows = _editor_rows(ctx, base_state, base_state, simple_nd=(target.role == Role.BISHOP and _king(ctx)),
                        layer=rbac.user_layer(target), result=rights, inherited=rbac.base_permissions_of(target))
    return _page(ctx, "access_account.html", acc_tree=kind.tree.key, kind=kind, own_kind=own_kind, target=target,
                 catalog=_catalog_for(ctx), rows=rows, types=types, effective_count=len(rights.allowed),
                 rule_modes=rules_mod.MODE_LABEL, kinds_nav=_kinds_nav(ctx, kind.tree, own_kind.slug),
                 piece=_piece(ctx, target), frozen=rights.frozen)


def _piece(ctx: Ctx, user: User) -> str:
    from app.core.chess import role_name
    return role_name(user.role, ctx.user)


@router.post("/admin/access/account/{user_id}")
async def access_account_save(user_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    raw_type = str(form.get("account_type", "")).strip()
    if "account_type" in form:
        agents.assign_account_type(ctx.db, ctx.user, user_id, int(raw_type) if raw_type.isdigit() else None)
    parsed = parse_rights(form)
    keep = {p.value for p in Perm} - parsed["posted"]
    target = agents.set_permission_overrides(ctx.db, ctx.user, user_id, parsed["allow"], parsed["deny"], parsed["rules"], keep=keep)
    return flash_redirect(f"/admin/access/account/{user_id}", f"Saved for {target.username}. It applies on their next click.")


@router.post("/admin/access/account/{user_id}/reset")
async def access_account_reset(user_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    agents.assign_account_type(ctx.db, ctx.user, user_id, None)
    target = agents.set_permission_overrides(ctx.db, ctx.user, user_id, [], [], {})
    return flash_redirect(f"/admin/access/account/{user_id}", f"{target.username} is back to the plain defaults for their role.")


# --------------------------------------------------------------------------- custom account types

@router.post("/admin/access/types/create")
async def type_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    row = agents.create_account_type(ctx.db, ctx.user, name=str(form.get("name", "")),
                                     base_role=str(form.get("base_role", "")), description=str(form.get("description", "")))
    kind = _custom_kind(row)
    return flash_redirect(kind.href, f"'{row.name}' created. Choose its rights below, then move accounts into it.")


@router.post("/admin/access/types/{type_id}/update")
async def type_update(type_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    row = agents.update_account_type(ctx.db, ctx.user, type_id, name=str(form.get("name", "")),
                                     description=str(form.get("description", "")))
    return flash_redirect(_custom_kind(row).href, "Account type updated.")


@router.post("/admin/access/types/{type_id}/delete")
async def type_delete(type_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    row = ctx.db.get(AccountProfile, type_id)
    if row is None:
        raise NotFound("Account type not found.")
    tree, name, base = tree_of(row.base_role), row.name, row.base_role
    moved = agents.delete_account_type(ctx.db, ctx.user, type_id)
    note = f" {moved} account{'s' if moved != 1 else ''} went back to the plain {KIND_LABEL[base]} defaults." if moved else ""
    return flash_redirect(f"/admin/access/{tree.key}", f"'{name}' deleted.{note}")


# --------------------------------------------------------------------------- requests: the inbox for King and Bishops

def _request_rows(ctx: Ctx, rows: list[AccessRequest]) -> list[dict]:
    users = {u.id: u for u in ctx.db.scalars(select(User).where(User.id.in_({r.requester_id for r in rows} | {r.decided_by_id for r in rows if r.decided_by_id} or {0}))).all()}
    out = []
    for r in rows:
        requester = users.get(r.requester_id)
        decider = users.get(r.decided_by_id) if r.decided_by_id else None
        out.append({"r": r, "requester": requester, "piece": _piece(ctx, requester) if requester else "",
                    "decider": ("you" if decider and decider.id == ctx.user.id else ("an admin" if decider else "")),
                    "label": PERM_LABELS.get(r.perm, r.perm), "fields": (r.payload or {}).get("fields") or [],
                    "can_decide": access.can_decide(ctx.db, ctx.user, r, requester)})
    return out


@router.get("/admin/access/requests")
async def requests_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.APPROVALS_DECIDE)
    pending = _request_rows(ctx, access.visible_requests(ctx.db, ctx.user, status="pending"))
    history = [x for x in _request_rows(ctx, access.visible_requests(ctx.db, ctx.user, limit=60)) if x["r"].status != "pending"][:40]
    return _page(ctx, "access_requests.html", acc_tree="requests", pending=pending, history=history,
                 timed=list(access.TIMED_CHOICES), max_hours=int(rbac.flag("max_pass_hours")))


@router.post("/admin/access/requests/{request_id}/decide")
async def request_decide(request_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.APPROVALS_DECIDE)
    form = await request.form()
    choice = str(form.get("choice", ""))
    note = str(form.get("note", ""))
    if choice == "deny":
        row = access.decide(ctx.db, ctx.user, request_id, decision="deny", note=note)
        return flash_redirect("/admin/access/requests", "Denied. They have been told.", "info")
    grant = "uses:" + str(form.get("uses", "3")) if choice == "uses" else choice
    row = access.decide(ctx.db, ctx.user, request_id, decision="approve", grant=grant, note=note)
    return flash_redirect("/admin/access/requests", "Approved. They have been told and can run it now.")


@router.post("/admin/access/requests/{request_id}/revoke")
async def request_revoke(request_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.APPROVALS_DECIDE)
    access.revoke_pass(ctx.db, ctx.user, request_id)
    return flash_redirect("/admin/access/requests", "That pass is cancelled.", "info")


# --------------------------------------------------------------------------- requests: anyone

@router.get("/access/ask")
async def ask_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    perm = request.query_params.get("perm", "")
    try:
        label = PERM_LABELS[Perm(perm).value]
    except (ValueError, KeyError):
        raise NotFound("That page does not exist.")
    back = request.query_params.get("back", "/dashboard")
    back = back if back.startswith("/") and not back.startswith("//") else "/dashboard"
    return render(ctx, "access_ask.html", perm=perm, label=label, back=back)


@router.post("/access/ask")
async def ask_submit(request: Request, ctx: Ctx = Depends(require_ctx)):
    form = await request.form()
    back = str(form.get("back", "/dashboard"))
    back = back if back.startswith("/") and not back.startswith("//") else "/dashboard"
    access.ask_for_access(ctx.db, ctx.user, str(form.get("perm", "")), path=back, reason=str(form.get("reason", "")))
    return flash_redirect("/access/mine", "Asked. You will be notified when it is answered.", "info")


@router.get("/access/mine")
async def my_requests(ctx: Ctx = Depends(require_ctx)):
    rows = access.mine(ctx.db, ctx.user)
    items = [{"r": r, "label": PERM_LABELS.get(r.perm, r.perm), "fields": (r.payload or {}).get("fields") or [],
              "runnable": r.status == "approved" and r.kind == "action" and r.method in ("POST", "PUT", "PATCH", "DELETE")}
             for r in rows]
    return render(ctx, "access_mine.html", items=items, now=dt.datetime.now(dt.timezone.utc))


@router.post("/access/mine/{request_id}/cancel")
async def my_request_cancel(request_id: int, ctx: Ctx = Depends(require_ctx)):
    access.cancel(ctx.db, ctx.user, request_id)
    return flash_redirect("/access/mine", "Cancelled.", "info")


# --------------------------------------------------------------------------- King only: the desk and the Bishops

@router.get("/admin/access/king")
async def king_desk(ctx: Ctx = Depends(require_ctx)):
    _king_only(ctx)
    ctx.need(Perm.KING_DESK)
    db = ctx.db
    sealed = {p.value for p in rbac.sealed_perms()} - {p.value for p in KING_ONLY}
    active_passes = int(db.scalar(select(func.count(AccessRequest.id)).where(AccessRequest.status == "approved")) or 0)
    recent = db.execute(select(AccessRequest).order_by(AccessRequest.id.desc()).limit(8)).scalars().all()
    flags = {k: rbac.flag(k) for k in rbac.FLAG_DEFAULTS}
    return _page(ctx, "access_king.html", acc_tree="king", flags=flags, sealed=sealed, catalog=PERM_CATALOG,
                 king_only={p.value for p in KING_ONLY} | {p.value for p in NON_DELEGABLE}, var_groups=list(GROUPS),
                 sealed_groups=rbac.sealed_groups(), active_passes=active_passes)


@router.post("/admin/access/king/flags")
async def king_flags(request: Request, ctx: Ctx = Depends(require_ctx)):
    _king_only(ctx)
    ctx.need(Perm.KING_DESK)
    form = await request.form()
    changes = {k: (str(form.get(k, "")) == "on") for k in ("freeze_pawns", "freeze_bishops", "approvals_paused", "bishops_may_approve")}
    changes["request_ttl_hours"] = form.get("request_ttl_hours", rbac.flag("request_ttl_hours"))
    changes["max_pass_hours"] = form.get("max_pass_hours", rbac.flag("max_pass_hours"))
    changes["sealed_perms"] = [str(v) for v in form.getlist("sealed_perm")]
    changes["sealed_groups"] = [str(v) for v in form.getlist("sealed_group")]
    done = access.set_flags(ctx.db, ctx.user, changes)
    return flash_redirect("/admin/access/king", "Saved. Applies within seconds." if done else "No changes.", "success" if done else "info")


@router.post("/admin/access/king/revoke-passes")
async def king_revoke_passes(ctx: Ctx = Depends(require_ctx)):
    _king_only(ctx)
    ctx.need(Perm.KING_DESK)
    count = access.revoke_all_passes(ctx.db, ctx.user)
    return flash_redirect("/admin/access/king", f"{count} pass{'es' if count != 1 else ''} ended.", "info")


@router.post("/admin/access/bishops/create")
async def bishop_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    _king_only(ctx)
    ctx.need(Perm.BISHOPS_MANAGE)
    form = await request.form()
    bishop = agents.create_agent(ctx.db, ctx.user, username=str(form.get("username", "")), password=str(form.get("password", "")),
                                 role=Role.BISHOP, full_name=str(form.get("full_name", "")), contact=form.get("contact"),
                                 settings=ctx.store)
    return flash_redirect("/admin/access/dev/bishop", f"Bishop '{bishop.username}' appointed. They must change the password at first sign-in.")


def _bishop(ctx: Ctx, bishop_id: int) -> User:
    _king_only(ctx)
    ctx.need(Perm.BISHOPS_MANAGE)
    row = ctx.db.get(User, bishop_id)
    if row is None or row.role != Role.BISHOP:
        raise NotFound("Bishop not found.")
    return row


@router.post("/admin/access/bishops/{bishop_id}/status")
async def bishop_status(bishop_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    row = _bishop(ctx, bishop_id)
    form = await request.form()
    agents.update_agent(ctx.db, ctx.user, bishop_id, status=Status(str(form.get("status", "active"))), reason=str(form.get("reason", "")) or None)
    return flash_redirect("/admin/access/dev/bishop", f"{row.username} is now {form.get('status', 'active')}.")


@router.post("/admin/access/bishops/{bishop_id}/password")
async def bishop_password(bishop_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    row = _bishop(ctx, bishop_id)
    form = await request.form()
    agents.reset_password(ctx.db, ctx.user, bishop_id, str(form.get("password", "")), settings=ctx.store)
    return flash_redirect("/admin/access/dev/bishop", f"Password reset for {row.username}.")


# --------------------------------------------------------------------------- one account type (a tree's page)

@router.get("/admin/access/{tree_key}")
async def tree_page(tree_key: str, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    tree = TREE_BY_KEY.get(tree_key)
    if tree is None:
        raise NotFound("That group does not exist.")
    return RedirectResponse(f"/admin/access/{tree.key}/{kinds_visible_to(tree, ctx.user)[0]}", status_code=303)


@router.get("/admin/access/{tree_key}/{slug}")
async def kind_page(tree_key: str, slug: str, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    db = ctx.db
    kind = _find_kind(ctx, tree_key, slug)
    held = {p.value for p in _kind_rights(kind)}
    state, baseline = _kind_state(kind), _kind_baseline_state(kind)
    rows = _editor_rows(ctx, state, baseline, simple_nd=(kind.base == "bishop" and _king(ctx)))
    members, member_total, players = [], 0, []
    if kind.base == "player":
        member_total = int(db.scalar(select(func.count(Player.id))) or 0)
        players = list(db.scalars(select(Player).order_by(Player.id.desc()).limit(25)).all())
    else:
        q = select(User).where(User.role == Role(kind.base))
        if kind.type is not None:
            q = q.where(User.account_profile_id == kind.type.id)
        member_total = int(db.scalar(select(func.count()).select_from(q.subquery())) or 0)
        for u in db.scalars(q.order_by(User.username).limit(MEMBER_LIMIT)).all():
            members.append({"user": u, "rights": len(rbac.permissions_of(u)),
                            "individual": bool(u.perm_grants or u.perm_revokes or u.perm_rules), "type": u.account_profile_id})
    type_names = {t.id: t.name for t in db.scalars(select(AccountProfile)).all()}
    extra = {}
    if kind.base == "pawn" and kind.type is None:
        sections: dict[str, list] = {}
        for grant in offered_grants():
            if not _king(ctx) and (any(p not in rbac.permissions_of(ctx.user) for p in grant.perms)
                                   or (grant.key.startswith("vars:") and grant.key.split(":")[1] in rbac.sealed_groups())):
                continue
            sections.setdefault(grant.section, []).append(grant)
        extra = {"pawns": [m["user"] for m in members], "sections": sections,
                 "min_len": int(ctx.store.get("security.password_min_length"))}
    elif kind.base == "bishop" and kind.type is None:
        extra = {"bishops": [m["user"] for m in members], "min_len": int(ctx.store.get("security.password_min_length"))}
    customised = (kind.base in rbac.role_overrides()) if kind.type is None else bool(not rbac.type_overrides().get(kind.type.id, rbac.EMPTY_LAYER).is_empty())
    return _page(ctx, "access_kind.html", acc_tree=kind.tree.key, kind=kind, held=held, rows=rows, catalog=_catalog_for(ctx),
                 members=members, member_total=member_total, member_limit=MEMBER_LIMIT, players=players, type_names=type_names,
                 kinds_nav=_kinds_nav(ctx, kind.tree, kind.slug), bola_locked=rbac.bola_dev_only(),
                 customised=customised, rule_modes=rules_mod.MODE_LABEL, **extra)


def _save_kind(ctx: Ctx, kind: Kind, form) -> bool:
    parsed = parse_rights(form)
    keep = {p.value for p in Perm} - parsed["posted"]
    if kind.type is not None:
        return agents.set_type_defaults(ctx.db, ctx.user, kind.type.id, parsed["allow"], parsed["rules"], keep=keep)
    return agents.set_role_defaults(ctx.db, ctx.user, kind.base, parsed["allow"], parsed["rules"], keep=keep)


@router.post("/admin/access/{tree_key}/{slug}/defaults")
async def kind_save(tree_key: str, slug: str, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    kind = _find_kind(ctx, tree_key, slug)
    if kind.base == "admin":
        raise NotFound("That page does not exist.")
    changed = _save_kind(ctx, kind, await request.form())
    return flash_redirect(kind.href, f"Default rights for {kind.label} saved. They apply to everyone of this type within seconds."
                          if changed else "No changes.")


@router.post("/admin/access/{tree_key}/{slug}/reset")
async def kind_reset(tree_key: str, slug: str, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    kind = _find_kind(ctx, tree_key, slug)
    if kind.base == "admin":
        raise NotFound("That page does not exist.")
    baseline = _kind_baseline_state(kind)
    wanted = [p.value for p, (k, _r) in baseline.items() if k == "allow"]
    if kind.type is not None:
        agents.set_type_defaults(ctx.db, ctx.user, kind.type.id, wanted, {})
    else:
        agents.set_role_defaults(ctx.db, ctx.user, kind.base, wanted, {})
    return flash_redirect(kind.href, f"{kind.label} is back to its starting defaults.")


# --------------------------------------------------------------------------- old addresses (bookmarks and old links)

@router.get("/admin/pawns", include_in_schema=False)
async def legacy_pawns(ctx: Ctx = Depends(require_ctx)):
    """The old Pawns page is now Access -> Dev -> Pawn; this address shows it directly."""
    return await kind_page("dev", "pawn", ctx)


@router.get("/admin/pawns/access", include_in_schema=False)
@router.get("/admin/pawns/access/roles", include_in_schema=False)
async def legacy_access(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    return RedirectResponse("/admin/access", status_code=303)


@router.get("/admin/pawns/access/{user_id}", include_in_schema=False)
async def legacy_access_account(user_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    return RedirectResponse(f"/admin/access/account/{user_id}", status_code=303)
