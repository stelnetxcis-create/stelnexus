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
    for path in ["/dashboard", "/events", "/games", "/history", "/archives", "/claim", "/chat",
                 "/cashin_out", "/settings", "/agents", "/betlogs", "/cancelT", "/cashflow"]:
        assert c.get(path).status_code == 200, path


def test_sidebar_has_no_account_block_and_menu_has_signout():
    c, _ = login("admin")
    html = c.get("/dashboard").text
    assert "sidebar-foot" not in html
    assert 'class="menu account-menu"' in html and 'action="/logout"' in html
    assert 'src="/static/js/nav-init.js"' in html


def test_claim_and_chat_scripts_load_via_scripts_block():
    c, _ = login("admin")
    assert "/static/js/claim.js" in c.get("/claim").text
    assert "/static/js/chat.js" in c.get("/chat").text


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
    for path in ["/dashboard", "/betlogs", "/claim", "/cashflow", "/chat", "/archives"]:
        assert c.get(path).status_code == 403, path
    assert c.get("/games").status_code == 200
    assert c.get("/history").status_code == 200


def test_legacy_change_name_url_redirects_to_games_keeping_query():
    c, _ = login("admin")
    r = c.get("/change_name?event_id=3&fight_id=7")
    assert r.status_code == 308
    assert r.headers["location"] == "/games?event_id=3&fight_id=7"
    assert c.get("/change_name").headers["location"] == "/games"


def test_terminology_labels_are_consistent():
    c, _ = login("admin")
    nav = c.get("/dashboard").text
    assert 'href="/games"' in nav and "/change_name" not in nav
    assert "Wallet Stations" in nav and "Game View" in nav
    assert "Betting List" in c.get("/betlogs").text and "Betting Master List" not in c.get("/betlogs").text
    assert "Previous Game History" not in c.get("/history").text
    assert "Requests History" in c.get("/cashin_out?t=hist").text
    assert "Wallet Stations" in c.get("/stations").text
    # the spendable balance is "load credit"; bare "credit" is reserved for station-collected cash
    assert "Load credit" in c.get("/agents").text
    assert "Remit credit" in c.get("/cashflow").text


def test_game_view_forms_post_to_games_urls():
    c, _ = login("admin")
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post("/events/create", data={"csrf_token": token, "title": "Terminology smoke",
                                       "event_date": "2030-01-01", "fight_count": "2",
                                       "payout_model": "pool"})
    assert r.status_code == 303, r.text[:200]
    event_id = int(re.search(r"/events/(\d+)", r.headers["location"]).group(1))
    html = c.get(f"/games?event_id={event_id}").text
    assert "/fights/" not in html and "/change_name" not in html
    m = re.search(r'action="/games/(\d+)/lock"', html)
    assert m, "lock form should post to /games/<id>/lock"
    token = c.cookies.get("snx_csrf").split(".")[0]  # the cookie rotates between requests
    r = c.post(f"/games/{m.group(1)}/lock", data={"csrf_token": token, "locked": "1"})
    assert r.status_code == 303 and r.headers["location"].startswith("/games?event_id=")
    token = c.cookies.get("snx_csrf").split(".")[0]
    assert c.post(f"/fights/{m.group(1)}/lock", data={"csrf_token": token}).status_code in (404, 405)
