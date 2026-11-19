import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services import agents
from app.db.models import Role
from tests.conftest import PASSWORD

app = create_app()


def login(username, password=PASSWORD, stale_cookie=False):
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    if stale_cookie:
        c.cookies.set(get_settings().SESSION_COOKIE, "stale-old-session-value")
    c.get("/login")
    token = c.cookies.get("snx_csrf").split(".")[0]
    return c, c.post("/login", data={"username": username, "password": password, "csrf_token": token})


def test_login_works_with_clean_and_stale_cookie():
    assert login("admin")[1].status_code == 303
    assert login("admin", stale_cookie=True)[1].status_code == 303


def test_bad_password_then_retry_with_stale_cookie():
    c, r = login("admin", "wrong-password", stale_cookie=True)
    assert r.status_code == 401
    token = c.cookies.get("snx_csrf").split(".")[0]
    assert c.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token}).status_code == 303


def test_pages_render_for_admin():
    c, _ = login("admin")
    for path in ["/dashboard", "/events", "/change_name", "/history", "/archives", "/chat",
                 "/wallet", "/admin/variables", "/agents", "/betlogs", "/cancelT", "/cashflow"]:
        assert c.get(path).status_code == 200, path


def test_sidebar_has_no_account_block_and_menu_has_signout():
    c, _ = login("admin")
    html = c.get("/dashboard").text
    assert "sidebar-foot" not in html
    assert 'class="menu account-menu"' in html and 'action="/logout"' in html
    from app.core.assets import asset_urls
    assert f'src="{asset_urls("js", "nav-init")[0]}' in html


def test_chat_script_loads_via_scripts_block():
    c, _ = login("admin")
    from app.core.assets import asset_urls
    assert asset_urls("js", "chat")[0] in c.get("/chat").text


def test_counter_pages_are_gone():
    """Online-only: no stations, booklets, ticket-claim desk or counter cash."""
    c, _ = login("admin")
    for path in ["/stations", "/booklets", "/claim"]:
        assert c.get(path).status_code == 404, path
    html = c.get("/dashboard").text
    for word in ("Teller", "Booklets", "Claim Ticket", "Cash desk"):
        assert word not in html, word


def test_live_board_is_public_and_hides_names():
    r = TestClient(app).get("/live")
    assert r.status_code == 200 and "Admin" not in r.text


def test_chat_roundtrip_escapes_nothing_server_side_but_returns_plain_text():
    c, _ = login("admin")
    token = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/dashboard").text).group(1)
    r = c.post("/chat/send", json={"body": "hi <b>x</b>"},
               headers={"X-CSRF-Token": token, "Accept": "application/json"})
    assert r.status_code == 200
    msgs = c.get("/chat/messages?after=0", headers={"Accept": "application/json"}).json()["messages"]
    assert msgs[-1]["body"] == "hi <b>x</b>"    # the browser inserts it via textContent


def test_declarator_is_restricted():
    from app.db.session import session_scope
    from sqlalchemy import select
    from app.db.models import User
    from app.core.settings_registry import SettingsStore
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        agents.create_agent(db, admin, username="decl_user", password=PASSWORD, role=Role.DECLARATOR,
                            full_name="Decl", settings=SettingsStore(db))
        # skip the forced password change for the test
        db.scalar(select(User).where(User.username == "decl_user")).must_change_password = False
    c, r = login("decl_user")
    assert r.status_code == 303
    for path in ["/dashboard", "/betlogs", "/cashflow", "/chat", "/archives"]:
        assert c.get(path).status_code == 403, path
    assert c.get("/change_name").status_code == 200
    assert c.get("/history").status_code == 200
