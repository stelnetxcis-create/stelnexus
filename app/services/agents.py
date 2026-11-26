"""Agent network: registration, hierarchy, credit transfer, suspension.

Fixes vs. the original:
* role of the created account is validated server-side against the creator's
  role (the old form merely offered a dropdown): Admin -> Master Agent -> Agent -> Sub Agent;
* passwords are Argon2id-hashed and never echoed back;
* credit transfer is a balanced ledger transaction under row locks, scoped to
  the downline, idempotent, and cannot overdraw the sender;
* referral codes come from a CSPRNG, not PHP's rand().
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import assert_user_in_scope, can_create_role, downline_ids, has_global_scope, is_top, scope_ids
import re

from app.core.identity import check_username, normalize_ph_mobile
from app.core.security import hash_password, password_problems, pii, random_code
from app.core.settings_registry import SettingsStore
from app.db.models import ADMIN_TIER, UPLINE_ROLES, Role, Status, User
from app.services import accounts, ledger


def _unique_username(db: Session, username: str, settings=None, *, public_signup: bool = False) -> str:
    from app.services import identity
    username = check_username(username, min_length=identity.min_username_length(settings), public_signup=public_signup)
    identity.assert_username_free(db, username)           # users AND players, ignoring upper/lower case
    return username


def _unique_ref(db: Session) -> str:
    for _ in range(12):
        code = random_code(10)
        if not db.scalar(select(User.id).where(User.ref_code == code)):
            return code
    raise Conflict("Could not allocate a referral code, please retry.")


def _names(roles) -> str:
    labels = [r.label for r in roles]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]


def pick_upline(db: Session, role: str, parent_id: int | None) -> User:
    """The active account a new `role` ('agent', 'sub_agent' or 'player')
    reports to, for creators that are not part of the tree (Admin, Pawn). An explicit `parent_id`
    may be any role allowed above `role` (see UPLINE_ROLES). With none given, the usual upline is
    used if exactly one such active account exists."""
    allowed = UPLINE_ROLES[role]
    if parent_id:
        parent = db.get(User, parent_id)
    else:
        found = db.scalars(select(User).where(User.role == allowed[0], User.status == Status.ACTIVE)
                           .order_by(User.id).limit(2)).all()
        parent = found[0] if len(found) == 1 else None
    if parent is None or parent.role not in allowed or parent.status != Status.ACTIVE:
        label = "Player" if role == "player" else Role(role).label
        raise ValidationError(f"Choose the active {_names(allowed)} this {label} reports to.")
    return parent


def _resolve_parent(db: Session, actor: User, role: Role, parent_id: int | None) -> User | None:
    """Who the new account reports to.

    An Agent sits under a Master Agent and a Sub Agent under an Agent. Anyone in
    the tree creating their own downline is the upline. The Admin (and a Pawn allowed to create
    accounts) are not part of the tree, so they must name the upline; if exactly one usual
    candidate exists it is used."""
    if role == Role.ADMIN:
        return None
    if role in (Role.AGENT, Role.SUB_AGENT) and actor.role in (Role.ADMIN, Role.BISHOP, Role.PAWN):
        return pick_upline(db, role.value, parent_id)
    if actor.role in (Role.ADMIN, Role.BISHOP) and role == Role.MASTER:
        return db.get(User, parent_id) if parent_id else actor
    return actor


def create_agent(
    db: Session,
    actor: User,
    *,
    username: str,
    password: str,
    role: Role,
    full_name: str,
    contact: str | None = None,
    parent_id: int | None = None,
    settings: SettingsStore | None = None,
    gcash_number: str | None = None,
    email: str | None = None,
    public_signup: bool = False,
) -> User:
    username = (username or "").strip()
    full_name = full_name.strip()
    if not full_name or len(full_name) > 120:
        raise ValidationError("Full name is required (max 120 characters).")
    if role == Role.ADMIN:
        raise Forbidden("There is only one Admin account; it cannot be duplicated.")
    if not can_create_role(actor, role):
        raise Forbidden(f"A {actor.role_label} cannot create a {role.label}.")
    if role == Role.BISHOP and actor.role != Role.ADMIN:
        raise Forbidden("Only the King appoints a Bishop.")
    username = _unique_username(db, username, settings, public_signup=public_signup)
    if (contact or "").strip():
        contact = normalize_ph_mobile(contact, what="Contact number")
    gcash = (gcash_number or "").strip()
    if role == Role.TELLER:
        gcash = normalize_ph_mobile(gcash, what="A Teller's GCash number")
    email_enc = email_key_ = None
    if (email or "").strip():
        from app.services import identity
        email_enc, email_key_ = identity.claim_gmail(db, email)

    problems = password_problems(
        password, username, min_length=int(settings.get("security.password_min_length")) if settings else None)
    if problems:
        raise ValidationError(" ".join(dict.fromkeys(problems)))

    parent = _resolve_parent(db, actor, role, parent_id)
    if parent is not None and parent.status != Status.ACTIVE:
        raise ValidationError("The upline account is not active.")

    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        full_name=full_name,
        contact=(contact or "").strip()[:32] or None,
        email_enc=email_enc, email_key=email_key_,
        gcash_enc=pii.encrypt(gcash) if role == Role.TELLER else None,
        parent_id=parent.id if parent else None,
        ref_code=_unique_ref(db),
        status=Status.ACTIVE,
        must_change_password=True,
        created_by_id=actor.id,
    )
    db.add(user)
    db.flush()
    if user.has_wallet:                     # declarators and pawns never get a balance
        accounts.wallet_for_user(db, user)
        accounts.commission_for_user(db, user)
    if role == Role.TELLER:                 # their first number joins the GCash pool
        from app.services import gcash
        gcash.seed_from_teller(db, user, gcash_number or "")
    audit.record(db, actor, "agent.create", entity_type="user", entity_id=user.id,
                 after={"username": user.username, "role": role.value, "parent_id": user.parent_id,
                        "full_name": full_name, "contact": contact})
    return user


def update_agent(
    db: Session, actor: User, target_id: int, *, full_name: str | None = None,
    contact: str | None = None, status: Status | None = None, reason: str | None = None,
) -> User:
    target = assert_user_in_scope(db, actor, target_id)
    if target.id == actor.id and status is not None and status != Status.ACTIVE:
        raise ValidationError("You cannot suspend your own account.")
    if target.role == Role.ADMIN and actor.role != Role.ADMIN:
        raise Forbidden("Only an admin may modify an admin.")
    before = {"full_name": target.full_name, "contact": target.contact, "status": target.status}
    if full_name is not None:
        full_name = full_name.strip()
        if not full_name or len(full_name) > 120:
            raise ValidationError("Full name is required (max 120 characters).")
        target.full_name = full_name
    if contact is not None:
        target.contact = contact.strip()[:32] or None
    if status is not None:
        if status == Status.SUSPENDED and not reason:
            raise ValidationError("A reason is required to suspend an account.")
        target.status = status
        if status != Status.ACTIVE:
            revoke_sessions(db, target)
    db.flush()
    audit.record(db, actor, "agent.update", entity_type="user", entity_id=target.id,
                 before=before, after={"full_name": target.full_name, "contact": target.contact,
                                       "status": target.status}, reason=reason)
    return target


def reset_password(db: Session, actor: User, target_id: int, new_password: str,
                   settings: SettingsStore | None = None) -> User:
    target = assert_user_in_scope(db, actor, target_id)
    problems = password_problems(
        new_password, target.username,
        min_length=int(settings.get("security.password_min_length")) if settings else None)
    if problems:
        raise ValidationError(" ".join(dict.fromkeys(problems)))
    target.password_hash = hash_password(new_password)
    target.must_change_password = True
    target.password_changed_at = dt.datetime.now(dt.timezone.utc)
    target.failed_attempts = 0
    target.locked_until = None
    revoke_sessions(db, target)
    db.flush()
    audit.record(db, actor, "agent.password_reset", entity_type="user", entity_id=target.id)
    return target


def set_pawn_grants(db: Session, actor: User, pawn_id: int, grants) -> User:
    """The King or a Bishop decides what a Pawn may see and change in the Admin Panel. Audited.

    A Bishop can only switch on what it holds itself, and never a settings group the King sealed."""
    from app.core import rbac
    from app.core.rbac import GRANT_BY_KEY, Perm, has_perm, normalize_grants
    if not has_perm(actor, Perm.PAWNS_MANAGE):
        raise Forbidden("Only an admin can change what a Pawn may see.")
    pawn = db.get(User, pawn_id)
    if pawn is None or pawn.role != Role.PAWN:
        raise NotFound("Pawn not found.")
    keys = normalize_grants(grants)
    if actor.role != Role.ADMIN:
        mine, sealed_groups = rbac.permissions_of(actor), rbac.sealed_groups()
        for key in set(keys) - set(pawn.grants or []):
            grant = GRANT_BY_KEY.get(key)
            if grant is None:
                continue
            if any(p not in mine for p in grant.perms) or (key.startswith("vars:") and key.split(":")[1] in sealed_groups):
                raise Forbidden(f"You cannot switch on \u201c{grant.label}\u201d.")
    before = list(pawn.grants or [])
    pawn.grants = keys
    db.flush()
    audit.record(db, actor, "pawn.grants", entity_type="user", entity_id=pawn.id,
                 before={"grants": before, "username": pawn.username},
                 after={"grants": pawn.grants, "username": pawn.username})
    return pawn


EDITABLE_DEFAULT_ROLES = ("bishop", "master", "agent", "sub_agent", "teller", "declarator", "pawn", "player")


def _require_access_manager(actor: User, what: str = "change access rights") -> None:
    from app.core.rbac import Perm, has_perm
    if not has_perm(actor, Perm.PAWNS_MANAGE):
        raise Forbidden(f"Only an admin can {what}.")


def _after_commit_invalidate(db: Session) -> None:
    from sqlalchemy import event
    from app.core import rbac
    rbac.invalidate_role_overrides()
    event.listen(db, "after_commit", lambda _s: rbac.invalidate_role_overrides(), once=True)


def can_manage_role(actor: User, role_key: str) -> bool:
    """Whose rights may this admin edit? The King: every role but its own. A Bishop: only roles below the Bishops."""
    if actor.role == Role.ADMIN:
        return role_key != Role.ADMIN.value
    if actor.role == Role.BISHOP:
        return role_key not in (Role.ADMIN.value, Role.BISHOP.value)
    return False


def _assert_manage_role(actor: User, role_key: str) -> None:
    if role_key not in EDITABLE_DEFAULT_ROLES or not can_manage_role(actor, role_key):
        raise Forbidden("That account type's rights cannot be changed.")


def _assert_manage_user(actor: User, target: User) -> None:
    if target.role == Role.ADMIN or (target.role == Role.BISHOP and actor.role != Role.ADMIN):
        raise Forbidden("That account's rights cannot be changed.")


def _locked_perm_values() -> set[str]:
    """Rights no editor may switch ON for anyone: the King's own, and Bola while it is dev-only. (The Access manager
    keys are never grantable either, but a revoke of them is allowed: the King can take them from a Bishop.)"""
    from app.core import rbac
    return {p.value for p in rbac.KING_ONLY} | ({p.value for p in rbac.BOLA_PERMS} if rbac.bola_dev_only() else set())


def _sorted_keys(keys) -> list[str]:
    from app.core import rbac
    return sorted({str(k) for k in (keys or [])}, key=lambda k: rbac._PERM_ORDER.get(k, 10_000))


def _prepare_rules(rules) -> dict[str, dict]:
    """Rules as posted -> clean rules. A malformed rule is refused with the right's name, never silently dropped."""
    from app.core import rbac
    from app.core import rules as rules_mod
    out: dict[str, dict] = {}
    for key, raw in (rules or {}).items():
        try:
            perm = rbac.Perm(str(key))
        except ValueError:
            continue
        if perm in rbac.NON_DELEGABLE or perm in rbac.KING_ONLY:
            continue
        rule = rules_mod.clean_rule(raw)
        label = rbac.PERM_LABELS.get(perm.value, perm.value)
        if rule is None:
            raise ValidationError(f"Check the settings for \u201c{label}\u201d (a time, hours or number is missing or wrong).")
        if rule["mode"] == "ask" and perm in rbac.VIEW_PERMS:
            raise ValidationError(f"\u201cAsk first\u201d does not apply to a look-only right: {label}.")
        out[perm.value] = rule
    return out


def _compute_layer(base_state: dict, wanted, rules: dict | None, old: tuple[list, list, dict], *, keep=None):
    """Store what an editor posted as a small difference from what the level starts with.

    `wanted` is the set of rights that should be plainly ON; `rules` maps a right to its conditional rule (None = leave
    existing rules untouched, as the all-roles matrix does). Anything not wanted and without a rule is OFF."""
    from app.core import rbac
    old_g, old_r, old_rules = old
    wanted = {str(k) for k in (wanted or [])}
    locked = _locked_perm_values()
    keep = {str(k) for k in (keep or ())}
    new_g, new_r, new_rules = set(), set(), {}
    for perm in rbac.Perm:
        key = perm.value
        if key in locked or key in keep:                    # not editable here / not posted: keep whatever was stored
            if key in old_g:
                new_g.add(key)
            if key in old_r:
                new_r.add(key)
            if key in old_rules:
                new_rules[key] = old_rules[key]
            continue
        if rules is None and key in old_rules:
            new_rules[key] = old_rules[key]
            continue
        if rules is not None and key in rules:
            new_rules[key] = rules[key]
            continue
        base_kind = base_state.get(perm, ("deny", None))[0]
        if key in wanted:
            if base_kind != "allow" and perm not in rbac.NON_DELEGABLE:
                new_g.add(key)
        elif base_kind != "deny":
            new_r.add(key)
    return _sorted_keys(new_g), _sorted_keys(new_r), new_rules


def _assert_ceiling(actor: User, new_g, new_rules, old_g, old_rules) -> None:
    """Nobody hands out what they do not hold themselves, and nobody but the King touches what the King sealed."""
    from app.core import rbac
    if actor.role == Role.ADMIN:
        return
    mine, sealed = rbac.permissions_of(actor), rbac.sealed_perms()
    added = (set(new_g) - set(old_g)) | {k for k, v in new_rules.items() if old_rules.get(k) != v}
    for key in added:
        perm = rbac.Perm(key)
        if perm not in mine or perm in sealed:
            raise Forbidden(f"You cannot hand out \u201c{rbac.PERM_LABELS.get(key, key)}\u201d.")


def _layer_of(row) -> tuple[list, list, dict]:
    if row is None:
        return [], [], {}
    return _sorted_keys(row.perm_grants), _sorted_keys(row.perm_revokes), dict(row.perm_rules or {})


def set_role_defaults(db: Session, actor: User, role_key: str, wanted, rules: dict | None = None, keep=None) -> bool:
    """Change what every account of a role may do by default. Audited. Returns True if anything changed.

    `wanted` = the rights that are plainly ON, `rules` = the conditional ones (ask / temporary / hours / limit). The
    result is stored as a small difference from the built-in defaults written in code."""
    from app.core import rbac
    from app.db.models import RolePermOverride
    _require_access_manager(actor, "change role defaults")
    _assert_manage_role(actor, role_key)
    clean = _prepare_rules(rules) if rules is not None else None
    state = {p: ("allow", None) for p in (rbac.builtin_player_perms() if role_key == rbac.PLAYER_ROLE_KEY
                                           else rbac.builtin_permissions_for(role_key, gated=False))}
    row = db.get(RolePermOverride, role_key)
    old = _layer_of(row)
    new_g, new_r, new_rules = _compute_layer(state, wanted, clean, old, keep=keep)
    if (new_g, new_r, new_rules) == old:
        return False
    _assert_ceiling(actor, new_g, new_rules, old[0], old[2])
    if not new_g and not new_r and not new_rules:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(RolePermOverride(role=role_key, perm_grants=new_g, perm_revokes=new_r, perm_rules=new_rules,
                                updated_by_id=actor.id))
    else:
        row.perm_grants, row.perm_revokes, row.perm_rules, row.updated_by_id = new_g, new_r, new_rules, actor.id
    db.flush()
    audit.record(db, actor, "role.perms", entity_type="role", entity_id=role_key,
                 before={"allow": old[0], "deny": old[1], "rules": old[2]},
                 after={"allow": new_g, "deny": new_r, "rules": new_rules})
    _after_commit_invalidate(db)
    return True


# ------------------------------------------------------------------ custom account types

def _clean_type_name(name: str) -> str:
    from app.core.access_tree import KIND_LABEL
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not 2 <= len(name) <= 40:
        raise ValidationError("Give the account type a name of 2 to 40 characters.")
    if name.lower() in {v.lower() for v in KIND_LABEL.values()} | {"admin", "player", "players", "king", "bishop"}:
        raise ValidationError("That name is already used by a built-in account type.")
    return name


def create_account_type(db: Session, actor: User, *, name: str, base_role: str, description: str = "") -> "AccountProfile":
    from app.core.access_tree import TYPE_BASES
    from app.db.models import AccountProfile
    _require_access_manager(actor, "create account types")
    if base_role not in TYPE_BASES or not can_manage_role(actor, base_role):
        raise ValidationError("Choose which built-in account type this one is based on.")
    name = _clean_type_name(name)
    if db.scalar(select(AccountProfile.id).where(func.lower(AccountProfile.name) == name.lower())):
        raise Conflict("An account type with that name already exists.")
    row = AccountProfile(name=name, base_role=base_role, description=(description or "").strip()[:200],
                         perm_grants=[], perm_revokes=[], perm_rules={}, created_by_id=actor.id)
    db.add(row)
    db.flush()
    audit.record(db, actor, "accounttype.create", entity_type="account_type", entity_id=row.id,
                 after={"name": name, "base_role": base_role})
    return row


def _own_type(db: Session, actor: User, type_id: int):
    from app.db.models import AccountProfile
    row = db.get(AccountProfile, type_id)
    if row is None or not can_manage_role(actor, row.base_role):
        raise NotFound("Account type not found.")
    return row


def update_account_type(db: Session, actor: User, type_id: int, *, name: str, description: str = "") -> "AccountProfile":
    from app.db.models import AccountProfile
    _require_access_manager(actor, "edit account types")
    row = _own_type(db, actor, type_id)
    name = _clean_type_name(name)
    clash = db.scalar(select(AccountProfile.id).where(func.lower(AccountProfile.name) == name.lower(), AccountProfile.id != type_id))
    if clash:
        raise Conflict("An account type with that name already exists.")
    before = {"name": row.name, "description": row.description}
    row.name, row.description = name, (description or "").strip()[:200]
    db.flush()
    audit.record(db, actor, "accounttype.update", entity_type="account_type", entity_id=row.id,
                 before=before, after={"name": row.name, "description": row.description})
    return row


def set_type_defaults(db: Session, actor: User, type_id: int, wanted, rules: dict | None = None, keep=None) -> bool:
    """Set what a custom account type may do: stored as a difference from its base role's current defaults."""
    from app.core import rbac
    _require_access_manager(actor, "change account type rights")
    row = _own_type(db, actor, type_id)
    clean = _prepare_rules(rules) if rules is not None else None
    state = rbac.role_state(row.base_role)
    old = _layer_of(row)
    new_g, new_r, new_rules = _compute_layer(state, wanted, clean, old, keep=keep)
    if (new_g, new_r, new_rules) == old:
        return False
    _assert_ceiling(actor, new_g, new_rules, old[0], old[2])
    row.perm_grants, row.perm_revokes, row.perm_rules = new_g, new_r, new_rules
    db.flush()
    audit.record(db, actor, "accounttype.perms", entity_type="account_type", entity_id=row.id,
                 before={"allow": old[0], "deny": old[1], "rules": old[2], "name": row.name},
                 after={"allow": new_g, "deny": new_r, "rules": new_rules, "name": row.name})
    _after_commit_invalidate(db)
    return True


def delete_account_type(db: Session, actor: User, type_id: int) -> int:
    """Remove a custom type. Its accounts keep their role and fall back to the role defaults. Returns how many."""
    from sqlalchemy import update
    _require_access_manager(actor, "delete account types")
    row = _own_type(db, actor, type_id)
    moved = db.execute(update(User).where(User.account_profile_id == type_id).values(account_profile_id=None)).rowcount or 0
    audit.record(db, actor, "accounttype.delete", entity_type="account_type", entity_id=row.id,
                 before={"name": row.name, "base_role": row.base_role, "allow": list(row.perm_grants or []),
                         "deny": list(row.perm_revokes or [])}, after={"accounts_moved": moved})
    db.delete(row)
    db.flush()
    _after_commit_invalidate(db)
    return int(moved)


def assign_account_type(db: Session, actor: User, user_id: int, type_id: int | None) -> User:
    """Put an account in a custom type (or back to none). The type must be built on the account's own role."""
    from app.db.models import AccountProfile
    _require_access_manager(actor, "assign account types")
    target = db.get(User, user_id)
    if target is None:
        raise NotFound("Account not found.")
    _assert_manage_user(actor, target)
    if type_id is not None:
        row = db.get(AccountProfile, type_id)
        if row is None:
            raise NotFound("Account type not found.")
        if row.base_role != target.role.value:
            raise ValidationError(f"'{row.name}' is built on {row.base_role.replace('_', ' ')}, not {target.role.label}.")
    if target.account_profile_id == type_id:
        return target
    before = target.account_profile_id
    target.account_profile_id = type_id
    db.flush()
    audit.record(db, actor, "account.type", entity_type="user", entity_id=target.id,
                 before={"account_profile_id": before, "username": target.username},
                 after={"account_profile_id": type_id, "username": target.username})
    _after_commit_invalidate(db)
    return target


def set_permission_overrides(db: Session, actor: User, user_id: int, grants, revokes, rules: dict | None = None, keep=None) -> User:
    """Allow, deny or put a rule on single rights for ONE account. Audited.

    `grants` = Allow, `revokes` = Deny, `rules` = conditional (None keeps the account's existing rules, except for
    rights that appear in grants / revokes, which replace them). `keep` = rights not to touch at all. Deny beats Allow."""
    from app.core import rbac
    _require_access_manager(actor, "change account permissions")
    target = db.get(User, user_id)
    if target is None:
        raise NotFound("Account not found.")
    _assert_manage_user(actor, target)
    allow, deny = rbac.normalize_overrides(grants, revokes)
    old_g, old_r, old_rules = old = _layer_of(target)
    keep = {str(k) for k in (keep or ())}
    posted_rules = old_rules if rules is None else _prepare_rules(rules)
    new_rules = {k: v for k, v in posted_rules.items() if k not in allow and k not in deny}
    allow = [k for k in allow if k not in _locked_perm_values()]
    for key in keep:                                      # rows that were not posted stay exactly as they were
        for bucket, stored in ((allow, old_g), (deny, old_r)):
            if key in stored and key not in bucket:
                bucket.append(key)
        if key in old_rules:
            new_rules[key] = old_rules[key]
        else:
            new_rules.pop(key, None)
    allow, deny = _sorted_keys(allow), _sorted_keys(deny)
    if (allow, deny, new_rules) == old:
        return target
    _assert_ceiling(actor, allow, new_rules, old_g, old_rules)
    target.perm_grants, target.perm_revokes, target.perm_rules = allow, deny, new_rules
    db.flush()
    audit.record(db, actor, "account.perms", entity_type="user", entity_id=target.id,
                 before={"allow": old_g, "deny": old_r, "rules": old_rules, "username": target.username, "role": target.role.value},
                 after={"allow": allow, "deny": deny, "rules": new_rules, "username": target.username, "role": target.role.value})
    _after_commit_invalidate(db)
    return target


def list_permission_accounts(db: Session, viewer: User | None = None) -> list[User]:
    """Every account whose rights the viewer can edit. The King: everyone but itself. A Bishop: nobody at or above a Bishop."""
    hidden = [Role.ADMIN] if viewer is None or viewer.role == Role.ADMIN else [Role.ADMIN, Role.BISHOP]
    return list(db.scalars(select(User).where(User.role.notin_(hidden)).order_by(User.role, User.username)).all())


def list_pawns(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.role == Role.PAWN).order_by(User.username)).all())


def revoke_sessions(db: Session, user: User) -> None:
    from app.db.models import Session as DbSession
    now = dt.datetime.now(dt.timezone.utc)
    for row in db.scalars(select(DbSession).where(DbSession.user_id == user.id, DbSession.revoked_at.is_(None))):
        row.revoked_at = now
    db.flush()


def transfer_credit(
    db: Session, actor: User, *, target_id: int, amount_cents: int,
    settings: SettingsStore, note: str | None = None, idempotency_key: str | None = None,
) -> ledger.LedgerTxn:
    """Move wallet credit to another account. Kept as the stable entry point; the rules (who may
    receive credit, limits, no-balance roles) live in services.wallet."""
    from app.services import wallet as wallet_service
    return wallet_service.transfer(db, actor, target_id=target_id, amount_cents=amount_cents,
                                   settings=settings, note=note, idempotency_key=idempotency_key)


def list_agents(db: Session, actor: User, *, search: str | None = None,
                role: Role | None = None, limit: int = 200, offset: int = 0) -> list[User]:
    stmt = select(User)
    if has_global_scope(actor):
        stmt = stmt.where(User.id != actor.id)
        if not actor.is_admin_tier:
            stmt = stmt.where(or_(User.role != Role.TELLER, User.gcash_enc.is_not(None)))
        if actor.role != Role.ADMIN:
            stmt = stmt.where(User.role.notin_(ADMIN_TIER))      # the hidden accounts are never listed
    else:
        stmt = stmt.where(User.id.in_(scope_ids(db, actor) - {actor.id}))
    if role:
        stmt = stmt.where(User.role == role)
    if search:
        term = f"%{search.strip()[:60]}%"
        stmt = stmt.where(or_(User.username.ilike(term), User.full_name.ilike(term), User.ref_code.ilike(term)))
    stmt = stmt.order_by(User.id.desc()).limit(clamp_limit(limit, 500)).offset(clamp_offset(offset))
    return list(db.scalars(stmt).all())


def get_agent(db: Session, actor: User, agent_id: int) -> User:
    user = db.get(User, agent_id)
    if user is None:
        raise NotFound("Agent not found.")
    assert_user_in_scope(db, actor, agent_id)
    return user


def wallet_balance(db: Session, user: User) -> int:
    if not user.has_wallet:
        return 0
    return accounts.wallet_for_user(db, user).balance_cents


def commission_balance(db: Session, user: User) -> int:
    if not user.has_wallet:
        return 0
    return accounts.commission_for_user(db, user).balance_cents


def tree(db: Session, actor: User) -> list[dict]:
    """Nested downline for the dashboard tree widget."""
    ids = scope_ids(db, actor)
    users = list(db.scalars(select(User).where(User.id.in_(ids)).order_by(User.id)))
    if actor.role != Role.ADMIN:              # filter in memory: no db.get() per id
        users = [u for u in users if u.id == actor.id or u.role not in ADMIN_TIER]
    balances = accounts.user_balances(db, users)
    by_parent: dict[int | None, list[User]] = {}
    for user in users:
        by_parent.setdefault(user.parent_id, []).append(user)

    def build(parent_id: int | None, depth: int = 0) -> list[dict]:
        if depth > 6:
            return []
        return [
            {"user": u, "wallet": balances[u.id][0] or 0, "children": build(u.id, depth + 1)}
            for u in by_parent.get(parent_id, [])
        ]

    if actor.role == Role.ADMIN:
        return build(None)
    if actor.role == Role.BISHOP:        # the King and other Bishops are hidden, so the visible roots are the orphans
        known = {u.id for u in users}
        return [{"user": u, "wallet": balances[u.id][0] or 0, "children": build(u.id, 1)}
                for u in users if u.id != actor.id and u.parent_id not in known]
    return build(actor.id)
