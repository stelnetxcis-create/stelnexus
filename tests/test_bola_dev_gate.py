"""BOLA_DEV_ONLY is an opt-in switch (default off since the game opened to everyone). While it is
on, only the hidden dev (Admin) sees the number game. Every test here runs with it switched on."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.core import rbac
from app.core.config import get_settings
from app.core.rbac import BOLA_PERMS, Perm, offered_grants, permissions_for, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import Role, User
from app.db.session import session_scope
from app.services import agents
from tests.conftest import PASSWORD
from tests.test_bola import _web_setup, _web_teardown
from tests.test_web import login


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", True)


@pytest.fixture()
def gate_off(monkeypatch, gate_on):
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", False)


def test_gate_can_be_switched_on():
    assert rbac.bola_dev_only()


@pytest.mark.parametrize("role", [Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.DECLARATOR, Role.PAWN])
def test_no_non_dev_role_holds_a_bola_permission(role):
    assert not (permissions_for(role) & BOLA_PERMS)


def test_admin_keeps_every_bola_permission():
    assert BOLA_PERMS <= permissions_for(Role.ADMIN)
    assert Perm.BOLA_MANAGE in BOLA_PERMS and Perm.BOLA_EXPOSURE in BOLA_PERMS


@pytest.mark.parametrize("role", [Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.DECLARATOR])
def test_flag_off_restores_each_roles_own_permissions(gate_off, role):
    assert permissions_for(role) & BOLA_PERMS


def test_a_pawn_cannot_be_granted_bola_even_directly(db, world):
    pawn = agents.create_agent(db, world.admin, username=f"gp{world.tag}", password=PASSWORD,
                               role=Role.PAWN, full_name="Pawn", settings=world.store)
    every = [g.key for g in rbac.GRANTS if g.section == "Bola" or g.key.startswith("vars:Bola:")]
    assert every, "the grants exist in code"
    agents.set_pawn_grants(db, world.admin, pawn.id, every)
    assert not (permissions_of(pawn) & BOLA_PERMS)
    assert "Bola" not in rbac.variable_groups_visible(pawn)
    assert "Bola" not in rbac.variable_groups_editable(pawn)


def test_admin_panel_does_not_offer_bola_grants_or_group_while_gated():
    keys = {g.key for g in offered_grants()}
    assert not any(k.startswith("bola:") or k.startswith("vars:Bola:") for k in keys)
    assert "cashflow:view" in keys                      # everything else is still offered
    assert "Bola" in rbac.variable_groups_visible(_admin_user())


def _admin_user():
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        db.expunge(user)
        return user


def test_offered_grants_return_when_gate_is_lifted(gate_off):
    assert any(g.section == "Bola" for g in offered_grants())


def _users():
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    out = {}
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        store = SettingsStore(db)
        master = agents.create_agent(db, admin, username=f"gm{tag}", password=PASSWORD, role=Role.MASTER,
                                     full_name="M", settings=store)
        cavo = agents.create_agent(db, master, username=f"gc{tag}", password=PASSWORD, role=Role.AGENT,
                                   full_name="C", settings=store)
        kub = agents.create_agent(db, cavo, username=f"gk{tag}", password=PASSWORD, role=Role.SUB_AGENT,
                                  full_name="K", settings=store)
        decl = agents.create_agent(db, admin, username=f"gd{tag}", password=PASSWORD, role=Role.DECLARATOR,
                                   full_name="D", settings=store)
        pawn = agents.create_agent(db, admin, username=f"gw{tag}", password=PASSWORD, role=Role.PAWN,
                                   full_name="W", settings=store)
        agents.set_pawn_grants(db, admin, pawn.id, [g.key for g in rbac.GRANTS])
        for key, user in dict(master=master, cavo=cavo, kub=kub, decl=decl, pawn=pawn).items():
            user.must_change_password = False
            out[key] = user.username
    return out


def test_every_page_and_endpoint_is_closed_to_everyone_but_dev():
    ids = _web_setup()
    names = _users()
    try:
        urls = ["/bola", "/bola/results", "/bola/betlogs", "/bola/reports",
                f"/bola/draws/{ids['draw']}", f"/bola/draws/{ids['draw']}/report.csv",
                f"/bola/draws/{ids['draw']}/proof", f"/bola/draws/{ids['draw']}/preview?kind=pair&numbers=1,2",
                "/admin/bola/schedules"]
        for who, username in names.items():
            c, r = login(username)
            assert r.status_code == 303, who
            for url in urls:
                assert c.get(url).status_code in (403, 404), (who, url)
            token = c.cookies.get("snx_csrf").split(".")[0]
            for url in ("/bola/bets/place", f"/bola/draws/{ids['draw']}/declare",
                        f"/bola/draws/{ids['draw']}/open", "/bola/draws/generate",
                        f"/bola/draws/{ids['draw']}/expenses", f"/bola/draws/{ids['draw']}/proof",
                        "/admin/bola/schedules/save"):
                # a denied form POST is flashed back to the page; ask for JSON to see the real status
                r = c.post(url, data={"csrf_token": token}, headers={"Accept": "application/json"})
                assert r.status_code in (403, 404), (who, url, r.status_code)
            # nothing in the navigation, dashboard or settings reveals it
            if who in ("master", "cavo", "kub", "pawn"):
                html = c.get("/dashboard").text
                assert "Number game" not in html and "/bola" not in html and "Bola" not in html, who
            if who == "pawn":
                assert "Bola" not in c.get("/admin/variables").text
        # the dev sees it all
        c, _ = login("admin")
        html = c.get("/dashboard").text
        assert "Number game · dev only" in html and "bola-card" in html
        assert all(c.get(u).status_code == 200 for u in urls[:4] + [urls[4], urls[5], "/admin/bola/schedules"])
    finally:
        _web_teardown(ids)


def test_the_public_live_board_never_shows_bola_while_gated():
    from fastapi.testclient import TestClient
    from tests.test_web import app
    ids = _web_setup()
    try:
        with session_scope() as db:
            store = SettingsStore(db)
            admin = db.scalar(select(User).where(User.username == "admin"))
            store.set("bola.enabled", "true", admin.id)
            store.set("live.show_bola", "true", admin.id)
        assert "NUMBER GAME" not in TestClient(app).get("/live").text
    finally:
        _web_teardown(ids)


def test_the_live_board_shows_it_once_the_gate_is_lifted(gate_off):
    from fastapi.testclient import TestClient
    from tests.test_web import app
    ids = _web_setup()
    try:
        with session_scope() as db:
            store = SettingsStore(db)
            admin = db.scalar(select(User).where(User.username == "admin"))
            store.set("bola.enabled", "true", admin.id)
            store.set("live.show_bola", "true", admin.id)
        html = TestClient(app).get("/live").text
        assert "NUMBER GAME" in html and "Web Area" in html
        for banned in ("Staked", "Web Player", "wbp", "BET-", "liability"):
            assert banned not in html
    finally:
        _web_teardown(ids)


def test_api_is_closed_to_non_dev_tokens():
    from fastapi.testclient import TestClient
    from tests.test_web import app
    names = _users()
    c = TestClient(app)
    for who, username in names.items():
        tok = c.post("/api/v1/auth/token", json={"username": username, "password": PASSWORD}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        for path in ("/api/v1/bola/draws", "/api/v1/bola/results", "/api/v1/bola/tickets"):
            assert c.get(path, headers=h).status_code == 403, (who, path)
