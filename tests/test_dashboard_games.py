"""Dashboard game cards: the same polished card as the Games hub, one "Enter" button each (playing and managing a game
are the same place). Sabong is always there; the number game appears when it is switched on and not dev-only-gated
for the viewer; Esports / NBA are quiet "Soon" cards."""
import re

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.settings_registry import SettingsStore
from app.db.models import Role, User
from app.db.session import session_scope
from app.services import agents
from tests.conftest import PASSWORD
from tests.test_web import login


def _dash(username="admin"):
    c, r = login(username)
    assert r.status_code == 303
    page = c.get("/dashboard")
    assert page.status_code == 200
    return page.text


def _tile(html, name):
    """The HTML of the game card whose label is `name` (an <article> with class gcard)."""
    for m in re.finditer(r'<article class="gcard[^"]*"[^>]*>.*?</article>', html, re.S):
        if f"<h3>{name}</h3>" in m.group(0):
            return m.group(0)
    return None


def _set_bola_enabled(value):
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        store = SettingsStore(db)
        before = bool(store.get("bola.enabled"))
        store.set("bola.enabled", "true" if value else "false", admin.id)
        return before


@pytest.fixture()
def gate_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", False)


def test_fight_view_button_is_gone_from_the_hero_but_the_page_still_links_to_it():
    html = _dash()
    hero = html.split('<section class="hero">')[1].split("</section>")[0]
    assert "Fight view" not in hero
    assert "Sell ticket" not in hero and "Wallet" in hero      # Seller Desk is gone; the other quick actions are untouched
    assert 'href="/games/sabong/live"' in html                       # still reachable (Live games card, sidebar)


def test_sabong_card_has_one_enter_button_and_no_play_or_manage():
    tile = _tile(_dash(), "Sabong")
    assert tile is not None and tile.startswith('<article class="gcard gcard-sabong ')
    assert 'class="btn btn-brand gcard-enter" href="/games/sabong/live"' in tile and "Enter" in tile
    assert ">Play<" not in tile and ">Manage<" not in tile
    assert "fight" in tile and "open" in tile and "Tickets" in tile


def test_number_game_card_enters_the_number_game_when_switched_on_and_never_says_coming_soon():
    before = _set_bola_enabled(True)
    try:
        tile = _tile(_dash(), "Number game")
        assert tile is not None and tile.startswith('<article class="gcard gcard-bola')
        assert 'href="/games/bola/live"' in tile and "Coming soon" not in tile and "Enter" in tile
        assert ">Play<" not in tile and ">Manage<" not in tile
    finally:
        _set_bola_enabled(before)


def test_number_game_tile_is_absent_when_switched_off():
    before = _set_bola_enabled(False)
    try:
        html = _dash()
        assert _tile(html, "Number game") is None and "Tumbok" not in html
    finally:
        _set_bola_enabled(before)


def test_number_game_card_shows_its_draws():
    before = _set_bola_enabled(True)
    try:
        tile = _tile(_dash(), "Number game")
        assert 'href="/games/bola/live"' in tile and "Coming soon" not in tile and "draw" in tile
    finally:
        _set_bola_enabled(before)


def test_esports_and_nba_are_quiet_soon_cards_in_the_same_style():
    html = _dash()
    for name in ("Esports", "NBA"):
        card = _tile(html, name)
        assert card is not None and card.startswith('<article class="gcard gcard-soon"') and "Soon" in card
        assert "<a " not in card                                   # nothing to click yet


def test_non_dev_roles_get_no_number_game_card_while_the_dev_gate_is_on_but_keep_sabong(monkeypatch):
    """With BOLA_DEV_ONLY switched on, only the Admin has the tile; everyone else sees nothing."""
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", True)
    import datetime as dt
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        store = SettingsStore(db)
        master = agents.create_agent(db, admin, username=f"dm{tag}", password=PASSWORD, role=Role.MASTER,
                                     full_name="M", settings=store)
        cavo = agents.create_agent(db, master, username=f"dc{tag}", password=PASSWORD, role=Role.AGENT,
                                   full_name="C", settings=store)
        agents.create_agent(db, cavo, username=f"dk{tag}", password=PASSWORD, role=Role.SUB_AGENT,
                            full_name="K", settings=store)
    for who in (f"dm{tag}", f"dk{tag}"):
        html = _dash(who)
        assert 'href="/games/sabong/live"' in _tile(html, "Sabong"), who
        # The Esports tile also says "Coming soon", so assert on the number-game tile itself.
        assert _tile(html, "Number game") is None and "Tumbok" not in html, who


def test_admin_still_has_the_tile_while_the_dev_gate_is_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", True)
    before = _set_bola_enabled(True)
    try:
        assert _tile(_dash(), "Number game") is not None          # the Admin still has it
    finally:
        _set_bola_enabled(before)
