"""Dashboard 'Games' tiles: Meron/Wala is live and clickable; the number game says 'Coming soon'
while it is dev-only (or switched off) and becomes a link on its own once it is launched."""
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
    """The HTML of the game tile whose label is `name` (a <a> or a <div> with class game-tile)."""
    for m in re.finditer(r'<(a|div) class="game-tile[^"]*"[^>]*>.*?</\1>', html, re.S):
        if name in m.group(0):
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
    assert "Sell ticket" in hero and "Wallet" in hero          # the other quick actions are untouched
    assert 'href="/change_name"' in html                       # still reachable (Live games card, sidebar)


def test_meron_wala_tile_is_a_live_link_to_the_games_lobby():
    tile = _tile(_dash(), "Meron")
    assert tile is not None
    assert tile.startswith('<a class="game-tile is-live"') and 'href="/events"' in tile
    assert "Wala" in tile and "game" in tile and "open" in tile


def test_number_game_says_coming_soon_and_is_not_clickable_while_dev_only():
    assert get_settings().BOLA_DEV_ONLY is True
    tile = _tile(_dash(), "Number game")
    assert tile is not None
    assert tile.startswith('<div class="game-tile is-soon"') and 'aria-disabled="true"' in tile
    assert "Coming soon" in tile and "<a " not in tile and "/bola" not in tile


def test_number_game_stays_coming_soon_when_gate_is_lifted_but_game_is_switched_off(gate_off):
    before = _set_bola_enabled(False)
    try:
        tile = _tile(_dash(), "Number game")
        assert tile.startswith('<div class="game-tile is-soon"') and "Coming soon" in tile
    finally:
        _set_bola_enabled(before)


def test_number_game_becomes_a_link_once_launched(gate_off):
    before = _set_bola_enabled(True)
    try:
        tile = _tile(_dash(), "Number game")
        assert tile.startswith('<a class="game-tile is-live"') and 'href="/bola"' in tile
        assert "Coming soon" not in tile and "draw" in tile
    finally:
        _set_bola_enabled(before)


def test_non_dev_roles_get_no_number_game_tile_while_dev_only_but_keep_meron_wala():
    """Ships dark: only the dev (Admin) sees 'Coming soon'. Everyone else sees nothing about it."""
    import datetime as dt
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        store = SettingsStore(db)
        master = agents.create_agent(db, admin, username=f"dm{tag}", password=PASSWORD, role=Role.MASTER,
                                     full_name="M", settings=store)
        cavo = agents.create_agent(db, master, username=f"dc{tag}", password=PASSWORD, role=Role.CAVO,
                                   full_name="C", settings=store)
        agents.create_agent(db, cavo, username=f"dk{tag}", password=PASSWORD, role=Role.KUBRADOR,
                            full_name="K", settings=store)
    for who in (f"dm{tag}", f"dk{tag}"):
        html = _dash(who)
        assert 'href="/events"' in _tile(html, "Meron"), who
        assert "Number game" not in html and "Coming soon" not in html and "Tumbok" not in html, who


def test_number_game_is_coming_soon_even_when_switched_on_while_dev_only():
    """The dev gate alone must keep the tile dark: bola.enabled=true must not open it."""
    assert get_settings().BOLA_DEV_ONLY is True
    before = _set_bola_enabled(True)
    try:
        tile = _tile(_dash(), "Number game")
        assert tile.startswith('<div class="game-tile is-soon"') and "Coming soon" in tile
        assert "<a " not in tile and "/bola" not in tile
    finally:
        _set_bola_enabled(before)
