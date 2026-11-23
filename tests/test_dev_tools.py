"""Dev-only house money, the reports menu, the Games Events label, the sidebar Install button,
and the DEV 'view as' preview (read-only, Admin-only, cannot be forged)."""
import re

import pytest
from sqlalchemy import func, select

from app.core.settings_registry import SettingsStore
from app.db.models import Event, Role, User
from app.db.session import session_scope
from app.services import agents
from app.web.deps import VIEWAS_COOKIE, make_viewas_cookie_value
from tests.conftest import PASSWORD
from tests.test_web import login


@pytest.fixture(scope="module")
def ids():
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        master = agents.create_agent(db, admin, username="dt_master", password=PASSWORD, role=Role.MASTER,
                                     full_name="DT Master", settings=store)
        cavo = agents.create_agent(db, master, username="dt_cavo", password=PASSWORD, role=Role.AGENT,
                                   full_name="DT Cavo", settings=store)
        master.must_change_password = False
        cavo.must_change_password = False
        db.flush()
        return {"admin": admin.id, "master": master.id, "cavo": cavo.id}


@pytest.fixture(scope="module")
def admin_c(ids):
    return login("admin")[0]


@pytest.fixture(scope="module")
def master_c(ids):
    return login("dt_master")[0]


@pytest.fixture(autouse=True)
def _no_leftover_preview(request):
    """Every test starts and ends with no preview active, whatever the previous one did."""
    yield
    for name in ("admin_c", "master_c"):
        if name in request.fixturenames:
            try:
                request.getfixturevalue(name).cookies.delete(VIEWAS_COOKIE)
            except Exception:
                pass


def csrf(client, path="/dashboard"):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get(path).text).group(1)


def event_count():
    with session_scope() as db:
        return db.scalar(select(func.count(Event.id)))


def test_master_has_no_house_money(master_c):
    c = master_c
    assert c.get("/cashflow").status_code == 403
    assert c.get("/archives", follow_redirects=True).status_code == 403
    html = c.get("/dashboard").text
    for hidden in ("Player balances", "House revenue", "Stake today", "Last 7 days"):
        assert hidden not in html, hidden
    assert "Awaiting result" in html and "Needs attention" in html
    assert 'class="menu dev-menu"' not in html
    assert "/cashflow" not in html and "/archives" not in html   # no reports links in any menu


def test_admin_keeps_house_money_and_reports_live_in_account_menu(admin_c):
    c = admin_c
    html = c.get("/dashboard").text
    for shown in ("Player balances", "House revenue", "Stake today", "Last 7 days"):
        assert shown in html, shown
    assert '<div class="nav-title">Reports</div>' not in html          # gone from the sidebar
    menu = html[html.index('class="menu account-menu"'):]
    assert "Profile &amp; security" in menu and 'href="/cashflow"' in menu and 'tab=reports' in menu and 'view=archive' in menu
    assert menu.index("Profile &amp; security") < menu.index('href="/cashflow"') < menu.index("Sign out")
    assert c.get("/cashflow").status_code == 200 and c.get("/archives", follow_redirects=True).status_code == 200


def test_games_events_label_and_install_in_sidebar(admin_c):
    c = admin_c
    for path in ("/dashboard", "/games/sabong/schedule", "/agents"):
        html = c.get(path).text
        aside = html[html.index('<aside class="sidebar"'):html.index("</aside>")]
        assert "<span>Sabong</span>" in aside and "<span>All games</span>" in aside and "Games Events" not in html and "Main Event" not in html
        assert aside.count("data-install") >= 1 and "sidebar-install" in aside
        topbar = html[html.index('<header class="topbar">'):html.index("</header>")]
        assert "data-install" not in topbar


def test_view_as_is_read_only_and_exits(admin_c):
    c = admin_c
    token = csrf(c)
    r = c.post("/dev/view-as", data={"csrf_token": token, "role": "master"})
    assert r.status_code == 303
    html = c.get("/dashboard").text
    assert "Previewing as" in html and "dt_master" in html
    assert "Player balances" not in html                      # exactly what the Master sees
    assert c.get("/cashflow").status_code == 403
    assert "Exit preview" in html and "dev-menu" in html      # the DEV menu stays reachable

    form = {"title": "Preview write test", "event_date": "2026-12-01", "fight_count": "2"}
    before = event_count()
    r = c.post("/events/create", data={"csrf_token": csrf(c), **form})
    assert r.status_code == 303 and event_count() == before             # refused while previewing

    assert c.post("/dev/view-as/exit", data={"csrf_token": csrf(c)}).status_code == 303
    assert "Previewing as" not in c.get("/dashboard").text
    assert c.get("/cashflow").status_code == 200
    # control: the very same request works once the preview is over, so the refusal above was the preview
    r = c.post("/events/create", data={"csrf_token": csrf(c), **form})
    assert r.status_code == 303 and event_count() == before + 1


def test_view_as_specific_account_shows_agent_dashboard(ids, admin_c):
    c = admin_c
    assert c.post("/dev/view-as", data={"csrf_token": csrf(c), "user_id": str(ids["cavo"])}).status_code == 303
    html = c.get("/dashboard").text
    assert "My credit" in html and "Stake today" not in html and "dt_cavo" in html
    c.post("/dev/view-as/exit", data={"csrf_token": csrf(c)})


def test_view_as_is_admin_only_and_cookie_cannot_be_forged(ids, master_c, admin_c):
    c = master_c
    token = csrf(c)
    c.post("/dev/view-as", data={"csrf_token": token, "role": "agent"})
    assert "Previewing as" not in c.get("/dashboard").text
    # a validly signed cookie minted for the Admin means nothing on someone else's session
    c.cookies.set(VIEWAS_COOKIE, make_viewas_cookie_value(ids["admin"], ids["cavo"]))
    html = c.get("/dashboard").text
    assert "Previewing as" not in html and "Awaiting result" in html
    # garbage cookie on the Admin's own session is ignored, not an error
    a = admin_c
    a.cookies.set(VIEWAS_COOKIE, "not-a-signed-value")
    assert a.get("/dashboard").status_code == 200 and "Previewing as" not in a.get("/dashboard").text


def test_cannot_preview_the_admin_or_missing_accounts(ids, admin_c):
    c = admin_c
    for bad in (str(ids["admin"]), "999999"):
        c.post("/dev/view-as", data={"csrf_token": csrf(c), "user_id": bad})
        assert "Previewing as" not in c.get("/dashboard").text


def test_topbar_does_not_wrap_rules_exist():
    css = open("app/static/css/base.css").read()
    assert ".topbar{flex-wrap:nowrap" in css
