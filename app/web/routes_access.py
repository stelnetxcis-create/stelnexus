"""Admin Panel -> Access: every account type and what it may do, in three trees.

    /admin/access                          overview of the Dev, Staff and Users trees
    /admin/access/{tree}/{kind}            one account type: its default rights and its accounts
                                           (kind = a built-in role such as ``agent`` or ``player``, or ``t12`` for a custom type)
    /admin/access/account/{id}             one account: its type and per-account Allow / Deny
    /admin/access/matrix                   every role side by side, editable
    /admin/access/types/...                create, rename and delete custom account types

Admin only (``Perm.PAWNS_MANAGE`` is never delegable). Rights resolve as: role defaults -> custom account type ->
a Pawn's grants -> the account's own Allow / Deny (Deny always wins). See app.core.rbac.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.core import rbac
from app.core.access_tree import KIND_BLURB, KIND_LABEL, TREE_BY_KEY, TREES, TYPE_BASES, Tree, tree_of
from app.core.errors import NotFound
from app.core.rbac import BOLA_PERMS, NON_DELEGABLE, PERM_CATALOG, Perm, offered_grants, permissions_of
from app.db.models import AccountProfile, Player, Role, User
from app.services import agents
from app.web.deps import Ctx, flash_redirect, require_ctx
from app.web.routes_admin import panel

router = APIRouter()
SECTION = "access"
_ROLE_COLUMNS = ("master", "agent", "sub_agent", "teller", "declarator", "pawn", "player")
MEMBER_LIMIT = 200


# --------------------------------------------------------------------------- helpers

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
    def href(self) -> str:
        return f"/admin/access/{self.tree.key}/{self.slug}"


def _builtin_kind(key: str) -> Kind:
    return Kind(key, KIND_LABEL[key], KIND_BLURB[key], tree_of(key), key)


def _custom_kind(row: AccountProfile) -> Kind:
    base_label = KIND_LABEL.get(row.base_role, row.base_role)
    return Kind(f"t{row.id}", row.name, row.description or f"A custom {base_label} type.", tree_of(row.base_role),
                row.base_role, row)


def _find_kind(db, tree_key: str, slug: str) -> Kind:
    tree = TREE_BY_KEY.get(tree_key)
    if tree is None:
        raise NotFound("That group does not exist.")
    if slug in tree.kinds:
        return _builtin_kind(slug)
    if slug.startswith("t") and slug[1:].isdigit():
        row = db.get(AccountProfile, int(slug[1:]))
        if row is not None and tree_of(row.base_role).key == tree.key:
            return _custom_kind(row)
    raise NotFound("That account type does not exist.")


def _kind_rights(kind: Kind, *, gated: bool = False) -> set[Perm]:
    """What every account of this kind may do by default (before Pawn grants and per-account settings)."""
    if kind.base == "admin":
        return set(Perm)
    if kind.type is not None:
        return rbac.type_permissions(kind.type.id, kind.base, gated=gated)
    return rbac.default_permissions_for_key(kind.base, gated=gated)


def _kind_baseline(kind: Kind) -> set[Perm]:
    """What a kind starts from: the code's built-in defaults, or (custom types) the base role's current defaults."""
    if kind.type is not None:
        return rbac.permissions_for(kind.base, gated=False)
    if kind.base == rbac.PLAYER_ROLE_KEY:
        return rbac.builtin_player_perms()
    if kind.base == "admin":
        return set(Perm)
    return rbac.builtin_permissions_for(kind.base, gated=False)


def _counts(db) -> tuple[dict[str, int], dict[int, int]]:
    by_role = {role.value: int(n) for role, n in db.execute(select(User.role, func.count(User.id)).group_by(User.role)).all()}
    by_role["player"] = int(db.scalar(select(func.count(Player.id))) or 0)
    by_type = {int(tid): int(n) for tid, n in db.execute(
        select(User.account_profile_id, func.count(User.id)).where(User.account_profile_id.is_not(None))
        .group_by(User.account_profile_id)).all()}
    return by_role, by_type


def _kinds_of_tree(db, tree: Tree) -> list[Kind]:
    kinds = [_builtin_kind(k) for k in tree.kinds]
    rows = db.scalars(select(AccountProfile).order_by(AccountProfile.name)).all()
    kinds += [_custom_kind(r) for r in rows if tree_of(r.base_role).key == tree.key]
    return kinds


def _card(kind: Kind, by_role: dict, by_type: dict) -> dict:
    members = by_type.get(kind.type.id, 0) if kind.type else by_role.get(kind.base, 0)
    customised = (kind.type is not None) or (kind.base in rbac.role_overrides())
    return {"kind": kind, "members": members, "rights": len(_kind_rights(kind)), "customised": customised}


def _page(ctx: Ctx, template: str, **extra):
    return panel(ctx, SECTION, template, trees=TREES, kind_labels=KIND_LABEL,
                 type_bases=[(k, KIND_LABEL[k], tree_of(k).label) for k in TYPE_BASES], **extra)


# --------------------------------------------------------------------------- overview

@router.get("/admin/access")
async def access_home(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    db = ctx.db
    by_role, by_type = _counts(db)
    tree_groups = [{"tree": t, "cards": [_card(k, by_role, by_type) for k in _kinds_of_tree(db, t)]} for t in TREES]
    individual = sum(1 for g, r in db.execute(select(User.perm_grants, User.perm_revokes)).all() if g or r)
    custom_types = int(db.scalar(select(func.count(AccountProfile.id))) or 0)
    return _page(ctx, "access_home.html", acc_tree="overview", tree_groups=tree_groups, individual=individual,
                 custom_types=custom_types, roles_customised=len(rbac.role_overrides()),
                 total_accounts=sum(by_role.values()))


# --------------------------------------------------------------------------- matrix (all roles side by side)

def _matrix_columns():
    cols = []
    for key in _ROLE_COLUMNS:
        cols.append((key, KIND_LABEL[key], {p.value for p in rbac.default_permissions_for_key(key, gated=False)},
                     {p.value for p in _kind_baseline(_builtin_kind(key))}))
    return cols


@router.get("/admin/access/matrix")
async def access_matrix(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    locked = {p.value for p in NON_DELEGABLE} | ({p.value for p in BOLA_PERMS} if rbac.bola_dev_only() else set())
    return _page(ctx, "access_matrix.html", acc_tree="matrix", catalog=PERM_CATALOG, roles=_matrix_columns(), locked=locked,
                 non_delegable={p.value for p in NON_DELEGABLE}, bola_locked=rbac.bola_dev_only(),
                 role_tree={k: tree_of(k).key for k in _ROLE_COLUMNS})


@router.post("/admin/access/matrix")
async def access_matrix_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    changed = []
    for key in agents.EDITABLE_DEFAULT_ROLES:
        wanted = [p.value for p in Perm if str(form.get(f"r:{key}:{p.value}", "")) == "on"]
        if agents.set_role_defaults(ctx.db, ctx.user, key, wanted):
            changed.append(KIND_LABEL[key])
    msg = ("Defaults updated for " + ", ".join(changed) + ". Applies to everyone of those types within seconds."
           if changed else "No changes.")
    return flash_redirect("/admin/access/matrix", msg)


# --------------------------------------------------------------------------- one account

def _parse_overrides(form) -> tuple[list[str], list[str]]:
    grants, revokes = [], []
    for perm in Perm:
        choice = str(form.get(f"p:{perm.value}", ""))
        if choice == "allow":
            grants.append(perm.value)
        elif choice == "deny":
            revokes.append(perm.value)
    return grants, revokes


def _account_kind(db, target: User) -> Kind:
    if target.account_profile_id is not None:
        row = db.get(AccountProfile, target.account_profile_id)
        if row is not None:
            return _custom_kind(row)
    return _builtin_kind(target.role.value)


@router.get("/admin/access/account/{user_id}")
async def access_account(user_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    target = ctx.db.get(User, user_id)
    if target is None or target.role == Role.ADMIN:
        raise NotFound("Account not found.")
    kind = _builtin_kind(target.role.value)
    own_kind = _account_kind(ctx.db, target)
    types = [_custom_kind(r) for r in ctx.db.scalars(select(AccountProfile).where(AccountProfile.base_role == target.role.value)
                                                      .order_by(AccountProfile.name)).all()]
    return _page(ctx, "access_account.html", acc_tree=kind.tree.key, kind=kind, own_kind=own_kind, target=target,
                 catalog=PERM_CATALOG, base={p.value for p in rbac.base_permissions_of(target)},
                 allow=set(target.perm_grants or []), deny=set(target.perm_revokes or []),
                 effective={p.value for p in permissions_of(target)}, types=types,
                 non_delegable={p.value for p in NON_DELEGABLE}, bola_locked=rbac.bola_dev_only(),
                 bola_perms={p.value for p in BOLA_PERMS}, kinds_nav=_kinds_nav(ctx.db, kind.tree, own_kind.slug))


@router.post("/admin/access/account/{user_id}")
async def access_account_save(user_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    form = await request.form()
    raw_type = str(form.get("account_type", "")).strip()
    if "account_type" in form:
        agents.assign_account_type(ctx.db, ctx.user, user_id, int(raw_type) if raw_type.isdigit() else None)
    grants, revokes = _parse_overrides(form)
    target = agents.set_permission_overrides(ctx.db, ctx.user, user_id, grants, revokes)
    return flash_redirect(f"/admin/access/account/{user_id}",
                          f"Saved for {target.username}. It applies on their next click.")


@router.post("/admin/access/account/{user_id}/reset")
async def access_account_reset(user_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    agents.assign_account_type(ctx.db, ctx.user, user_id, None)
    target = agents.set_permission_overrides(ctx.db, ctx.user, user_id, [], [])
    return flash_redirect(f"/admin/access/account/{user_id}",
                          f"{target.username} is back to the plain defaults for their role.")


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
    tree, name = tree_of(row.base_role), row.name
    moved = agents.delete_account_type(ctx.db, ctx.user, type_id)
    note = f" {moved} account{'s' if moved != 1 else ''} went back to the plain {KIND_LABEL[row.base_role]} defaults." if moved else ""
    return flash_redirect(f"/admin/access/{tree.key}", f"'{name}' deleted.{note}")


# --------------------------------------------------------------------------- one account type (a tree's page)

def _kinds_nav(db, tree: Tree, active_slug: str) -> list[dict]:
    by_role, by_type = _counts(db)
    return [{"kind": k, "members": (by_type.get(k.type.id, 0) if k.type else by_role.get(k.base, 0)),
             "active": k.slug == active_slug} for k in _kinds_of_tree(db, tree)]


@router.get("/admin/access/{tree_key}")
async def tree_page(tree_key: str, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    tree = TREE_BY_KEY.get(tree_key)
    if tree is None:
        raise NotFound("That group does not exist.")
    return RedirectResponse(f"/admin/access/{tree.key}/{tree.kinds[0]}", status_code=303)


@router.get("/admin/access/{tree_key}/{slug}")
async def kind_page(tree_key: str, slug: str, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    db = ctx.db
    kind = _find_kind(db, tree_key, slug)
    held = {p.value for p in _kind_rights(kind)}
    baseline = {p.value for p in _kind_baseline(kind)}
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
            members.append({"user": u, "rights": len(permissions_of(u)),
                            "individual": bool(u.perm_grants or u.perm_revokes), "type": u.account_profile_id})
    type_names = {t.id: t.name for t in db.scalars(select(AccountProfile)).all()}
    extra = {}
    if kind.base == "pawn" and kind.type is None:
        sections: dict[str, list] = {}
        for grant in offered_grants():
            sections.setdefault(grant.section, []).append(grant)
        extra = {"pawns": [m["user"] for m in members], "sections": sections,
                 "min_len": int(ctx.store.get("security.password_min_length"))}
    return _page(ctx, "access_kind.html", acc_tree=kind.tree.key, kind=kind, held=held, baseline=baseline,
                 catalog=PERM_CATALOG, members=members, member_total=member_total, member_limit=MEMBER_LIMIT,
                 players=players, type_names=type_names, kinds_nav=_kinds_nav(db, kind.tree, kind.slug),
                 locked={p.value for p in NON_DELEGABLE} | ({p.value for p in BOLA_PERMS} if rbac.bola_dev_only() else set()),
                 non_delegable={p.value for p in NON_DELEGABLE}, bola_locked=rbac.bola_dev_only(),
                 customised=(kind.base in rbac.role_overrides()) if kind.type is None else bool(kind.type.perm_grants or kind.type.perm_revokes),
                 **extra)


@router.post("/admin/access/{tree_key}/{slug}/defaults")
async def kind_save(tree_key: str, slug: str, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    kind = _find_kind(ctx.db, tree_key, slug)
    if kind.base == "admin":
        raise NotFound("The Dev account always has every right.")
    form = await request.form()
    wanted = [p.value for p in Perm if str(form.get(f"p:{p.value}", "")) == "on"]
    if kind.type is not None:
        changed = agents.set_type_defaults(ctx.db, ctx.user, kind.type.id, wanted)
    else:
        changed = agents.set_role_defaults(ctx.db, ctx.user, kind.base, wanted)
    return flash_redirect(kind.href, f"Default rights for {kind.label} saved. They apply to everyone of this type within seconds."
                          if changed else "No changes.")


@router.post("/admin/access/{tree_key}/{slug}/reset")
async def kind_reset(tree_key: str, slug: str, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PAWNS_MANAGE)
    kind = _find_kind(ctx.db, tree_key, slug)
    if kind.base == "admin":
        raise NotFound("The Dev account always has every right.")
    baseline = [p.value for p in _kind_baseline(kind)]
    if kind.type is not None:
        agents.set_type_defaults(ctx.db, ctx.user, kind.type.id, baseline)
    else:
        agents.set_role_defaults(ctx.db, ctx.user, kind.base, baseline)
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
