"""One card, one "Enter": the Games hub and the Dashboard show every game the same way, and playing a game and
managing it are the same place (Sabong's events list with Create event is the Sabong page)."""
import datetime as dt
import re

from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import Role, User
from app.db.session import session_scope
from app.services import agents
from tests.conftest import PASSWORD
from tests.test_web import login


def _cards(html):
    return re.findall(r'<article class="gcard[^"]*"[^>]*>.*?</article>', html, re.S)


def test_hub_has_one_enter_button_per_game_and_no_play_or_manage():
    c, _ = login("admin")
    html = c.get("/games").text
    cards = [x for x in _cards(html) if "gcard-soon" not in x]
    assert len(cards) >= 1
    for card in cards:
        assert card.count("gcard-enter") == 1 and "Enter" in card
        assert ">Play<" not in card and ">Manage<" not in card and ">Watch<" not in card
    sabong = next(x for x in cards if "gcard-sabong" in x)
    assert 'href="/games/sabong/live"' in sabong and "Tickets" in sabong


def test_dashboard_and_hub_use_the_same_card():
    c, _ = login("admin")
    hub = [x for x in _cards(c.get("/games").text) if "gcard-sabong" in x][0]
    dash = [x for x in _cards(c.get("/dashboard").text) if "gcard-sabong" in x][0]
    assert hub == dash                                          # same markup, same numbers


def test_sabong_game_page_has_create_event_for_a_host_and_not_for_a_declarator():
    c, _ = login("admin")
    page = c.get("/games/sabong/live?pick=1")
    assert page.status_code == 200 and "Create event" in page.text
    assert 'data-modal="event-modal"' in page.text
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        agents.create_agent(db, admin, username=f"gd{tag}", password=PASSWORD, role=Role.DECLARATOR, full_name="D",
                            settings=SettingsStore(db))
    c2, r = login(f"gd{tag}")
    assert r.status_code == 303
    page = c2.get("/games/sabong/live?pick=1")
    assert page.status_code == 200 and "Create event" not in page.text and 'data-modal="event-modal"' not in page.text


def test_the_old_manage_address_and_the_create_link_land_on_the_game_page():
    c, _ = login("admin")
    for old, new in (("/games/sabong/schedule", "/games/sabong/live?pick=1"),
                     ("/games/sabong/schedule?create=1", "/games/sabong/live?pick=1&create=1")):
        r = c.get(old)
        assert r.status_code == 303 and r.headers["location"] == new


def test_number_game_manager_reaches_the_schedule_from_the_game_page():
    c, _ = login("admin")
    page = c.get("/bola")
    assert page.status_code == 200 and 'href="/games/bola/schedule"' in page.text
    assert c.get("/games/bola/schedule").status_code == 200       # the Bola schedule page is unchanged
