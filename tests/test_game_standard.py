"""Phase 7 of the Games Standard: a game without a complete adapter fails CI."""
import pytest
from fastapi.testclient import TestClient

from app import games
from app.games import RECORD_TABS, RIGHTS, WORKSPACE_TABS
from app.games.base import GameAdapter
from tests.test_web import login

ADAPTER_METHODS = ("workspace_urls", "summary_today", "tickets", "cancelled", "history",
                   "report_daily", "report_monthly", "report_session", "schedule", "create_spec")


def test_registry_is_not_empty_and_keys_match():
    assert {"sabong", "bola"} <= set(games.GAMES)
    for key, adapter in games.GAMES.items():
        assert adapter.key == key and adapter.label and adapter.icon


@pytest.mark.parametrize("key", sorted(games.GAMES))
def test_every_game_implements_every_adapter_method(key):
    adapter = games.GAMES[key]
    for name in ADAPTER_METHODS:
        assert getattr(type(adapter), name) is not getattr(GameAdapter, name), f"{key} must implement {name}"


@pytest.mark.parametrize("key", sorted(games.GAMES))
def test_every_game_defines_all_six_rights_and_vocabulary(key):
    adapter = games.GAMES[key]
    for right in RIGHTS:
        assert right in adapter.perms, f"{key} is missing the {right} right"
    assert {"game", "session", "round", "ticket"} <= set(adapter.vocabulary)


def test_unknown_right_is_rejected():
    with pytest.raises(ValueError):
        games.GAMES["sabong"].can(object(), "nonsense")


@pytest.mark.parametrize("key", sorted(games.GAMES))
def test_every_game_exposes_all_workspace_urls_to_the_admin(key):
    c, _ = login("admin")
    from app.web.deps import Ctx  # noqa: F401  (the route builds the Ctx; here we check through the web layer)
    for tab in WORKSPACE_TABS:
        r = c.get(f"/games/{key}/{tab}")
        if tab == "schedule":                       # the shared Schedule page renders in place
            assert r.status_code == 200, (key, tab)
        else:
            assert r.status_code == 303 and r.headers["location"].startswith("/"), (key, tab)


def test_every_records_tab_renders_for_the_admin():
    c, _ = login("admin")
    for tab in RECORD_TABS:
        for game in ("all", *games.GAMES):
            assert c.get(f"/records?tab={tab}&game={game}").status_code == 200, (tab, game)


class _FakeCtx:
    """Just enough of Ctx for the visibility rules."""
    def __init__(self, perms, enabled=True, user=None):
        self.perms, self.enabled, self.user = set(perms), enabled, user
        self.store = self

    def get(self, key, default=None):
        return self.enabled if key == "bola.enabled" else default

    def can(self, perm):
        return getattr(perm, "value", perm) in self.perms


def test_number_game_is_hidden_when_switched_off_or_without_the_right():
    from types import SimpleNamespace
    from app.core.rbac import Perm
    bola_game = games.GAMES["bola"]
    user = SimpleNamespace(role=None, is_top=True)
    assert not bola_game.visible(_FakeCtx({Perm.BOLA_VIEW.value}, enabled=False, user=user))
    assert not bola_game.visible(_FakeCtx(set(), enabled=True, user=user))


def test_sabong_visible_with_fights_or_events_view_only():
    from app.core.rbac import Perm
    sabong = games.GAMES["sabong"]
    assert sabong.visible(_FakeCtx({Perm.FIGHTS_VIEW.value}))
    assert sabong.visible(_FakeCtx({Perm.EVENTS_VIEW.value}))
    assert not sabong.visible(_FakeCtx(set()))


def test_bola_schedule_page_renders_with_every_filter_and_a_create_drawer():
    c, _ = login("admin")
    for when in ("today", "upcoming", "past", "nonsense"):     # unknown filter falls back to today
        r = c.get(f"/games/bola/schedule?when={when}")
        assert r.status_code == 200, when
        assert 'data-modal="create-modal"' in r.text and 'data-modal="generate-modal"' in r.text


def test_sabong_schedule_is_the_shared_page_with_the_events_lobby():
    c, _ = login("admin")
    page = c.get("/games/sabong/schedule")
    assert page.status_code == 200
    # the pieces pages.js and the realtime refresh look for must be on the shared page
    for needle in ('data-modal="event-modal"', 'id="ev-location"', 'id="loc-quick-link"', 'id="loc-quick-modal"'):
        assert needle in page.text, needle
    for when in ("today", "upcoming", "past", "all", "nonsense"):
        assert c.get(f"/games/sabong/schedule?when={when}").status_code == 200, when


def test_old_events_url_redirects_and_keeps_its_query():
    c, _ = login("admin")
    r = c.get("/events?create=1")
    assert r.status_code == 303 and r.headers["location"] == "/games/sabong/schedule?create=1"


def test_vocabulary_no_game_number_in_templates():
    """Plan 4.1: 'Game' only ever means the product; a fight is 'Fight #n'."""
    import pathlib
    import re
    bad = [p.name for p in pathlib.Path("app/web/templates").rglob("*.html")
           if re.search(r"\b[Gg]ame #|\bGame \{\{", p.read_text())]
    assert not bad, bad


def test_hub_play_or_watch_follows_the_place_right():
    from app.core.rbac import Perm
    sabong = games.GAMES["sabong"]
    assert sabong.can(_FakeCtx({Perm.BETS_PLACE.value}), "place")
    assert not sabong.can(_FakeCtx({Perm.FIGHTS_VIEW.value}), "place")
    c, _ = login("admin")
    assert "Play" in c.get("/games").text or "Watch" in c.get("/games").text
