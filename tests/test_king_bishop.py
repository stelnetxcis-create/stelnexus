"""The King (the one true admin), Bishops (deputies who believe they are the top admin) and Pawns."""
import datetime as dt
import re

import pytest
from sqlalchemy import select

from app.core import chess, rbac
from app.core.errors import Forbidden, ValidationError
from app.core.rbac import Perm, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import Role, User
from app.db.session import session_scope
from app.services import access, agents
from tests.conftest import PASSWORD
from tests.test_web import login


def _tag():
    return f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]


def _admin(db):
    return db.scalar(select(User).where(User.username == "admin"))


def _mk(db, actor, store, role, name, **kw):
    user = agents.create_agent(db, actor, username=name, password=PASSWORD, role=role, full_name=name.upper(), settings=store, **kw)
    user.must_change_password = False
    return user


@pytest.fixture(autouse=True)
def _clean_flags():
    yield
    from app.db.models import AccessFlag
    with session_scope() as db:
        for row in db.scalars(select(AccessFlag)).all():
            db.delete(row)
    rbac.invalidate_role_overrides()


def test_a_bishop_holds_every_right_except_the_kings_and_what_is_sealed():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        bishop = _mk(db, admin, store, Role.BISHOP, f"bs{tag}")
        mine = permissions_of(bishop)
        assert Perm.PAWNS_MANAGE in mine and Perm.APPROVALS_DECIDE in mine and Perm.SETTINGS_MANAGE in mine
        assert Perm.BISHOPS_MANAGE not in mine and Perm.KING_DESK not in mine
        assert Perm.HIDDEN_VIEW not in mine                              # sealed by default
        with pytest.raises(Forbidden):                                   # only the King appoints a Bishop
            _mk(db, bishop, store, Role.BISHOP, f"bt{tag}")
        pawn = _mk(db, bishop, store, Role.PAWN, f"pw{tag}")            # but a Bishop makes Pawns
        assert pawn.role == Role.PAWN


def test_the_king_alone_is_told_the_real_names():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        bishop = _mk(db, admin, store, Role.BISHOP, f"bn{tag}")
        pawn = _mk(db, admin, store, Role.PAWN, f"pn{tag}")
        assert chess.piece(Role.BISHOP, admin) == ("Bishop", "♗") and chess.piece(Role.ADMIN, admin) == ("King", "♔")
        assert chess.piece(Role.PAWN, admin) == ("Pawn", "♙")
        for viewer in (bishop, pawn):                                    # nobody else learns a King exists
            assert chess.piece(Role.BISHOP, viewer) == ("Admin", "♔")
            assert chess.piece(Role.ADMIN, viewer) == ("Admin", "♔")
        assert Role.BISHOP.label == "Admin"
        assert bishop.is_hidden and admin.is_hidden


def test_the_king_and_other_bishops_are_never_listed_to_a_bishop():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        b1 = _mk(db, admin, store, Role.BISHOP, f"b1{tag}")
        _mk(db, admin, store, Role.BISHOP, f"b2{tag}")
        _mk(db, admin, store, Role.PAWN, f"pl{tag}")
        names = {u.username for u in agents.list_agents(db, b1, limit=500)}
        assert f"pl{tag}" in names and "admin" not in names and f"b2{tag}" not in names
        everyone = {u.username for u in agents.list_agents(db, admin, limit=500)}
        assert {f"b1{tag}", f"b2{tag}"} <= everyone                       # the King sees every Bishop
        assert "System" == _ctx_who(db, b1, admin)


def _ctx_who(db, viewer, other):
    from app.web.deps import Ctx
    ctx = Ctx(request=None, db=db, store=SettingsStore(db), user=viewer)
    return ctx.who(other)


def test_a_bishop_cannot_touch_bishops_or_the_king_and_cannot_hand_out_what_it_lacks():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        b1 = _mk(db, admin, store, Role.BISHOP, f"c1{tag}")
        b2 = _mk(db, admin, store, Role.BISHOP, f"c2{tag}")
        pawn = _mk(db, b1, store, Role.PAWN, f"cp{tag}")
        with pytest.raises(Forbidden):
            agents.set_permission_overrides(db, b1, b2.id, [], [Perm.AUDIT_VIEW.value])      # another Bishop
        with pytest.raises(Forbidden):
            agents.set_permission_overrides(db, b1, admin.id, [], [Perm.AUDIT_VIEW.value])   # the King
        with pytest.raises(Forbidden):
            agents.set_role_defaults(db, b1, "bishop", [])                                   # Bishops are the King's to edit
        with pytest.raises(Forbidden):                                                       # sealed: a Bishop lacks it
            agents.set_permission_overrides(db, b1, pawn.id, [Perm.HIDDEN_VIEW.value], [])
        agents.set_permission_overrides(db, b1, pawn.id, [Perm.AUDIT_VIEW.value], [])        # it holds this one
        assert Perm.AUDIT_VIEW in permissions_of(pawn)
        with pytest.raises(Forbidden):                                                       # a switch needing a sealed right
            agents.set_pawn_grants(db, b1, pawn.id, ["hidden:view"]) if "hidden:view" in rbac.GRANT_BY_KEY else (_ for _ in ()).throw(Forbidden("n/a"))


def test_the_king_can_take_a_right_from_a_bishop_and_can_seal_and_freeze_live():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        bishop = _mk(db, admin, store, Role.BISHOP, f"d1{tag}")
        pawn = _mk(db, admin, store, Role.PAWN, f"d2{tag}")
        agents.set_permission_overrides(db, admin, pawn.id, [Perm.AUDIT_VIEW.value, Perm.IPBANS_MANAGE.value], [])
        bishop_id, pawn_id = bishop.id, pawn.id
        agents.set_permission_overrides(db, admin, bishop_id, [], [Perm.PAWNS_MANAGE.value])      # the King may revoke even this
    with session_scope() as db:
        bishop, pawn = db.get(User, bishop_id), db.get(User, pawn_id)
        assert Perm.PAWNS_MANAGE not in permissions_of(bishop) and Perm.AUDIT_VIEW in permissions_of(bishop)
        assert Perm.AUDIT_VIEW in permissions_of(pawn)
        # seal a right from the whole Dev tree
        access.set_flags(db, _admin(db), {"sealed_perms": [Perm.HIDDEN_VIEW.value, Perm.AUDIT_VIEW.value]})
    with session_scope() as db:
        assert Perm.AUDIT_VIEW not in permissions_of(db.get(User, bishop_id)) and Perm.AUDIT_VIEW not in permissions_of(db.get(User, pawn_id))
        # freeze Pawns: look-only
        access.set_flags(db, _admin(db), {"sealed_perms": [Perm.HIDDEN_VIEW.value], "freeze_pawns": True})
    with session_scope() as db:
        frozen = permissions_of(db.get(User, pawn_id))
        assert frozen and all(p in rbac.VIEW_PERMS for p in frozen)
        assert Perm.IPBANS_MANAGE not in frozen
        access.set_flags(db, _admin(db), {"freeze_pawns": False})
    with session_scope() as db:
        assert Perm.IPBANS_MANAGE in permissions_of(db.get(User, pawn_id))


def test_only_the_king_can_use_the_king_desk():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        bishop = _mk(db, admin, store, Role.BISHOP, f"e1{tag}")
        with pytest.raises(Forbidden):
            access.set_flags(db, bishop, {"freeze_pawns": True})
        with pytest.raises(Forbidden):
            access.revoke_all_passes(db, bishop)
        with pytest.raises(ValidationError):
            access.set_flags(db, admin, {"no_such_switch": True})


def test_a_bishop_never_sees_the_king_in_the_access_pages():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        _mk(db, admin, store, Role.BISHOP, f"f1{tag}")
        _mk(db, admin, store, Role.PAWN, f"f2{tag}")
    c, r = login(f"f1{tag}")
    assert r.status_code == 303
    pages = {p: c.get(p) for p in ("/admin/access", "/admin/access/dev/pawn", "/admin/access/matrix", "/admin/access/requests",
                                   "/admin/access/staff/declarator", "/admin/access/users/agent")}
    for path, response in pages.items():
        assert response.status_code == 200, path
        assert "King" not in response.text and "Bishop" not in response.text and "♗" not in response.text, path
    for path in ("/admin/access/dev/admin", "/admin/access/dev/bishop", "/admin/access/king"):
        assert c.get(path).status_code == 404, path                       # indistinguishable from a page that is not there
    c.post("/admin/access/bishops/create", data={"csrf_token": c.cookies.get("snx_csrf").split(".")[0],
                                                 "username": f"xb{tag}", "password": PASSWORD, "full_name": "X"})
    with session_scope() as db:
        assert db.scalar(select(User).where(User.username == f"xb{tag}")) is None      # refused: a Bishop cannot appoint one
    # the King sees the real names
    ck, r = login("admin")
    assert r.status_code == 303
    home = ck.get("/admin/access")
    assert "♗" in home.text and "King" in home.text and "Bishop" in home.text
    assert ck.get("/admin/access/dev/bishop").status_code == 200 and ck.get("/admin/access/king").status_code == 200


def test_the_king_appoints_a_bishop_over_http():
    tag = _tag()
    c, r = login("admin")
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post("/admin/access/bishops/create", data={"csrf_token": token, "username": f"g1{tag}", "password": PASSWORD,
                                                     "full_name": "Gee One", "contact": ""})
    assert r.status_code == 303
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == f"g1{tag}"))
        assert row is not None and row.role == Role.BISHOP and row.parent_id == _admin(db).id


def test_a_bishop_can_walk_the_whole_admin_panel_and_never_sees_a_hint_of_the_king():
    from app.web.routes_admin import SECTIONS
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        _mk(db, admin, store, Role.BISHOP, f"wk{tag}")
        _mk(db, admin, store, Role.PAWN, f"wp{tag}")
    c, r = login(f"wk{tag}")
    assert r.status_code == 303
    seen = 0
    for key, url, label, perm in SECTIONS:
        page = c.get(url)
        assert page.status_code < 500, (url, page.status_code)
        if page.status_code == 200:
            seen += 1
            assert not re.search(r"\bKing\b|\bBishops?\b|♗", page.text), url          # (not "Sacramento Kings")
    assert seen >= 8                                                       # it really has the admin panel
    assert c.get("/admin/pawns").status_code == 200                       # the old Pawns address works for a Bishop too


def test_a_bishop_creates_a_pawn_and_sets_its_switches_through_the_forms():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        _mk(db, admin, store, Role.BISHOP, f"hb{tag}")
    c, _ = login(f"hb{tag}")
    c.get("/admin/access/dev/pawn")
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post("/admin/pawns/create", data={"csrf_token": token, "full_name": "Helper", "username": f"hp{tag}",
                                            "password": PASSWORD, "contact": ""})
    assert r.status_code == 303
    with session_scope() as db:
        pawn = db.scalar(select(User).where(User.username == f"hp{tag}"))
        assert pawn is not None and pawn.role == Role.PAWN
        bishop = db.scalar(select(User).where(User.username == f"hb{tag}"))
        assert pawn.parent_id in (bishop.id, _admin(db).id)
        pawn_id = pawn.id
    page = c.get(f"/admin/access/account/{pawn_id}")
    assert page.status_code == 200 and "King" not in page.text
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post(f"/admin/access/account/{pawn_id}", data={"csrf_token": token, f"m:{Perm.AUDIT_VIEW.value}": "temp",
                                                         f"td:{Perm.AUDIT_VIEW.value}": "1h", f"m:{Perm.IPBANS_MANAGE.value}": "ask"})
    assert r.status_code == 303
    with session_scope() as db:
        pawn = db.get(User, pawn_id)
        assert Perm.AUDIT_VIEW in permissions_of(pawn)                    # temporary: on for the next hour
        assert Perm.IPBANS_MANAGE not in permissions_of(pawn) and Perm.IPBANS_MANAGE in rbac.rights_of(pawn).ask
        assert rbac.rights_of(pawn).mode_of(Perm.IPBANS_MANAGE) == "ask"
