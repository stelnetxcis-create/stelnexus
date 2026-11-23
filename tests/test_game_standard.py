"""Phase 7 of the Games Standard: a game without a complete adapter fails CI."""
import pytest
from fastapi.testclient import TestClient

from app import games
from app.games import RECORD_TABS, RIGHTS, WORKSPACE_TABS
from app.games.base import GameAdapter
from tests.test_web import login

ADAPTER_METHODS = ("workspace_urls", "summary_today", "tickets", "cancelled", "history",
                   "report_daily", "report_monthly", "report_session")


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
        assert r.status_code == 303 and r.headers["location"].startswith("/"), (key, tab)


def test_every_records_tab_renders_for_the_admin():
    c, _ = login("admin")
    for tab in RECORD_TABS:
        for game in ("all", *games.GAMES):
            assert c.get(f"/records?tab={tab}&game={game}").status_code == 200, (tab, game)
