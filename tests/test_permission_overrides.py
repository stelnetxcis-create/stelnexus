"""Admin Panel -> Pawns & Access: per-account permission overrides and the rights chart."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.core import rbac
from app.core.errors import Forbidden
from app.core.rbac import Perm, base_permissions_of, permissions_for, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import AuditLog, Role, User
from app.db.session import session_scope
from app.services import agents
from tests.conftest import PASSWORD
from tests.test_web import login


def _tag():
    return f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]


def _admin(db):
    return db.scalar(select(User).where(User.username == "admin"))


def _mk(db, admin, store, role, name, parent=None):
    user = agents.create_agent(db, parent or admin, username=name, password=PASSWORD, role=role,
                               full_name=name.upper(), settings=store)
    user.must_change_password = False
    return user


def _agent(db, admin, store, name):
    master = _mk(db, admin, store, Role.MASTER, f"m{name}")
    return _mk(db, admin, store, Role.AGENT, name, parent=master)


def test_catalog_covers_every_permission_once():
    listed = [p for _, items in rbac.PERM_CATALOG for p, _ in items]
    assert sorted(p.value for p in listed) == sorted(p.value for p in Perm)


def test_override_allows_and_denies_and_deny_wins():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        agent = _agent(db, admin, store, f"pa{tag}")
        assert Perm.CASHFLOW_VIEW not in permissions_of(agent) and Perm.BETS_PLACE in permissions_of(agent)
        agents.set_permission_overrides(db, admin, agent.id, [Perm.CASHFLOW_VIEW.value, Perm.BETS_PLACE.value],
                                        [Perm.BETS_PLACE.value])
        perms = permissions_of(agent)
        assert Perm.CASHFLOW_VIEW in perms
        assert Perm.BETS_PLACE not in perms                     # deny beats allow
        assert Perm.BETS_PLACE in base_permissions_of(agent)    # the role default is untouched
        agents.set_permission_overrides(db, admin, agent.id, [], [])
        assert permissions_of(agent) == permissions_for(Role.AGENT)


def test_pawn_manager_keys_cannot_be_delegated_or_removed():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        master = _mk(db, admin, store, Role.MASTER, f"pm{tag}")
        agents.set_permission_overrides(db, admin, master.id, [Perm.PAWNS_MANAGE.value], [])
        assert Perm.PAWNS_MANAGE not in permissions_of(master)
        # even a tampered row is ignored
        master.perm_grants = [Perm.PAWNS_MANAGE.value]
        assert Perm.PAWNS_MANAGE not in permissions_of(master)
        # and the Admin cannot be locked out
        admin.perm_revokes = [Perm.PAWNS_MANAGE.value, Perm.PANEL_VIEW.value]
        assert Perm.PAWNS_MANAGE in permissions_of(admin) and Perm.PANEL_VIEW in permissions_of(admin)
        admin.perm_revokes = None


def test_only_the_admin_can_set_overrides_and_never_on_the_admin():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        master = _mk(db, admin, store, Role.MASTER, f"po{tag}")
        agent = _mk(db, admin, store, Role.AGENT, f"pb{tag}", parent=master)
        with pytest.raises(Forbidden):
            agents.set_permission_overrides(db, master, agent.id, [Perm.CASHFLOW_VIEW.value], [])
        with pytest.raises(Forbidden):
            agents.set_permission_overrides(db, admin, admin.id, [], [Perm.PANEL_VIEW.value])
        pawn = _mk(db, admin, store, Role.PAWN, f"pw{tag}")
        agents.set_pawn_grants(db, admin, pawn.id, ["accounts:edit"])
        with pytest.raises(Forbidden):                          # a Pawn is not a manager of this
            agents.set_permission_overrides(db, pawn, agent.id, [Perm.CASHFLOW_VIEW.value], [])


def test_override_change_is_audited():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        agent = _agent(db, admin, store, f"pd{tag}")
        agents.set_permission_overrides(db, admin, agent.id, [Perm.AUDIT_VIEW.value], [Perm.BETS_PLACE.value])
        row = db.scalars(select(AuditLog).where(AuditLog.action == "account.perms",
                                                AuditLog.entity_id == str(agent.id))).first() \
            or db.scalars(select(AuditLog).where(AuditLog.action == "account.perms")).all()[-1]
        assert row.after["allow"] == [Perm.AUDIT_VIEW.value] and row.after["deny"] == [Perm.BETS_PLACE.value]


def test_pages_work_over_http_and_are_admin_only():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        agent = _agent(db, admin, store, f"ph{tag}")
        agent_id = agent.id
        _mk(db, admin, store, Role.MASTER, f"pj{tag}")
    c, r = login("admin")
    assert r.status_code == 303
    assert c.get("/admin/pawns/access").status_code == 200
    page = c.get(f"/admin/pawns/access/{agent_id}")
    assert page.status_code == 200 and f"ph{tag}" in page.text and "Admin only" in page.text
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post(f"/admin/pawns/access/{agent_id}",
               data={"csrf_token": token, f"p:{Perm.CASHFLOW_VIEW.value}": "allow", f"p:{Perm.BETS_PLACE.value}": "deny"})
    assert r.status_code == 303
    with session_scope() as db:
        a = db.get(User, agent_id)
        assert Perm.CASHFLOW_VIEW in permissions_of(a) and Perm.BETS_PLACE not in permissions_of(a)
    r = c.post(f"/admin/pawns/access/{agent_id}/reset", data={"csrf_token": token})
    assert r.status_code == 303
    with session_scope() as db:
        assert permissions_of(db.get(User, agent_id)) == permissions_for(Role.AGENT)
    assert c.get("/admin/pawns").status_code == 200            # Pawns page still renders with its new tabs
    c2, r = login(f"pj{tag}")                                    # Master: forbidden
    assert r.status_code == 303
    assert c2.get("/admin/pawns/access").status_code in (403, 404)


# ------------------------------------------------------------------ role defaults (the Admin edits the defaults)

def _reset_all_role_defaults():
    with session_scope() as db:
        admin = _admin(db)
        for key in agents.EDITABLE_DEFAULT_ROLES:
            builtin = (rbac.builtin_player_perms() if key == "player"
                       else rbac.builtin_permissions_for(key, gated=False))
            agents.set_role_defaults(db, admin, key, [p.value for p in builtin])
    rbac.invalidate_role_overrides()


@pytest.fixture()
def clean_roles():
    _reset_all_role_defaults()
    yield
    _reset_all_role_defaults()


def test_changing_a_role_default_reaches_every_account_in_that_role(clean_roles):
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        _mk(db, admin, store, Role.DECLARATOR, f"rd{tag}")
        _mk(db, admin, store, Role.DECLARATOR, f"re{tag}")
        builtin = {p.value for p in rbac.builtin_permissions_for(Role.DECLARATOR, gated=False)}
        assert agents.set_role_defaults(db, admin, "declarator", builtin | {Perm.BETS_VIEW.value})
        assert not agents.set_role_defaults(db, admin, "declarator", builtin | {Perm.BETS_VIEW.value})   # no-op
    with session_scope() as db:
        for name in (f"rd{tag}", f"re{tag}"):
            user = db.scalar(select(User).where(User.username == name))
            assert Perm.BETS_VIEW in permissions_of(user)
        # a per-account Deny still beats the new default
        one = db.scalar(select(User).where(User.username == f"rd{tag}"))
        agents.set_permission_overrides(db, _admin(db), one.id, [], [Perm.BETS_VIEW.value])
        assert Perm.BETS_VIEW not in permissions_of(one)
    assert Perm.BETS_VIEW not in permissions_for(Role.TELLER)          # other roles untouched
    # and a right can be taken away from a role too
    with session_scope() as db:
        admin = _admin(db)
        agent_defaults = {p.value for p in rbac.builtin_permissions_for(Role.AGENT, gated=False)}
        agents.set_role_defaults(db, admin, "agent", agent_defaults - {Perm.BETS_PLACE.value})
    assert Perm.BETS_PLACE not in permissions_for(Role.AGENT) and Perm.BETS_PLACE in permissions_for(Role.SUB_AGENT)


def test_player_defaults_can_change_but_the_manager_keys_cannot_move(clean_roles):
    with session_scope() as db:
        admin = _admin(db)
        everything = {p.value for p in Perm}
        agents.set_role_defaults(db, admin, "master", everything)             # tries to include PAWNS_MANAGE
        agents.set_role_defaults(db, admin, "player", {p.value for p in rbac.builtin_player_perms()} - {Perm.BETS_PLACE.value})
    assert Perm.PAWNS_MANAGE not in permissions_for(Role.MASTER)
    assert Perm.CASHFLOW_VIEW in permissions_for(Role.MASTER)                 # the rest of "everything" did apply
    assert Perm.BETS_PLACE not in rbac.player_perms()


def test_role_defaults_are_admin_only_and_the_admin_role_is_fixed(clean_roles):
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        master = _mk(db, admin, store, Role.MASTER, f"rf{tag}")
        with pytest.raises(Forbidden):
            agents.set_role_defaults(db, master, "agent", [])
        from app.core.errors import ValidationError
        with pytest.raises(ValidationError):
            agents.set_role_defaults(db, admin, "admin", [])
    assert permissions_for(Role.ADMIN) == set(Perm)


def test_role_default_pages_work_over_http(clean_roles):
    c, r = login("admin")
    assert r.status_code == 303
    page = c.get("/admin/pawns/access/roles")
    assert page.status_code == 200 and "Role defaults" in page.text
    token = c.cookies.get("snx_csrf").split(".")[0]
    data = {"csrf_token": token}
    for role_key in agents.EDITABLE_DEFAULT_ROLES:                              # post the current state, plus one change
        held = rbac.player_perms() if role_key == "player" else permissions_for(role_key)
        data.update({f"r:{role_key}:{p.value}": "on" for p in held})
    data[f"r:teller:{Perm.CASHFLOW_VIEW.value}"] = "on"
    assert c.post("/admin/pawns/access/roles", data=data).status_code == 303
    rbac.invalidate_role_overrides()
    assert Perm.CASHFLOW_VIEW in permissions_for(Role.TELLER)
    assert c.post("/admin/pawns/access/roles/teller/reset", data={"csrf_token": token}).status_code == 303
    rbac.invalidate_role_overrides()
    assert Perm.CASHFLOW_VIEW not in permissions_for(Role.TELLER)
    assert c.get("/admin/pawns/access").status_code == 200
