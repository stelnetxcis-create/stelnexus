"""Game visibility switches (Variables > Games) and Achievements badges."""
import pytest
from sqlalchemy import select

from app import games
from app.core.errors import LockedState, ValidationError
from app.core.settings_registry import BY_KEY, GROUPS
from app.db.models import Role, ScatterRound, TicketStatus, User, UserBadge
from app.services import accounts, badges, scatter
from tests.conftest import fund_user
from tests.test_web import login


# ---------------------------------------------------------------- the three visibility switches
def test_the_games_group_has_one_switch_per_game_and_defaults_to_shown():
    assert "Games" in GROUPS
    for key in ("games.show_sabong", "games.show_bola", "games.show_scatter"):
        assert BY_KEY[key].kind == "bool" and BY_KEY[key].default is True and BY_KEY[key].group == "Games"


def test_a_hidden_game_disappears_for_everyone_the_admin_included(world):
    w = world
    try:
        w.store.set("games.show_scatter", "false", w.admin.id)
        w.db.commit()
        c, _ = login("admin")
        assert c.get("/scatter").status_code == 404                       # the page is gone
        assert 'data-game="scatter"' not in c.get("/games").text           # and its card
        with pytest.raises(LockedState):                                    # and nobody can play it
            scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store)
        assert "scatter" not in [g.key for g in games.GAMES.values() if g.shown(_Ctx(w))]
    finally:
        w.store.set("games.show_scatter", "true", w.admin.id)
        w.db.commit()


def test_switching_a_game_back_on_brings_it_and_its_records_back(world):
    w = world
    fund_user(w.db, w.admin, w.admin, 500_000)
    rnd, _, _ = scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store)
    w.store.set("games.show_scatter", "false", w.admin.id)
    w.db.commit()
    c, _ = login("admin")
    assert rnd.code not in c.get("/records?tab=tickets&game=scatter").text   # records hide with the game
    w.store.set("games.show_scatter", "true", w.admin.id)
    w.db.commit()
    assert rnd.code in c.get("/records?tab=tickets&game=scatter").text       # and return untouched


def test_hidden_sabong_refuses_pages_and_bets(world):
    w = world
    try:
        w.store.set("games.show_sabong", "false", w.admin.id)
        w.db.commit()
        c, _ = login("admin")
        assert c.get("/events").status_code == 404
        assert c.get("/games/sabong/live").status_code == 404
        assert 'href="/games/sabong/live"' not in c.get("/dashboard").text
    finally:
        w.store.set("games.show_sabong", "true", w.admin.id)
        w.db.commit()


class _Ctx:
    def __init__(self, w):
        self.store, self.user = w.store, w.admin


# ---------------------------------------------------------------- the catalog
def test_every_badge_has_a_unique_icon_and_a_hard_condition():
    assert len(badges.CATALOG) >= 40
    assert len({badges.GLYPHS[b.key] for b in badges.CATALOG}) == len(badges.CATALOG)   # no two icons alike
    assert {b.tier for b in badges.CATALOG} == set(badges.TIERS)
    for b in badges.CATALOG:
        assert b.target >= 1 and b.game in badges.GAME_LABELS and b.name and b.desc
        assert "<svg" in str(badges.icon(b.key)) and "locked" in str(badges.icon(b.key, locked=True))


def test_streaks_ignore_refunds_and_break_on_losses():
    assert badges._streak([True, True, None, True, False, True]) == 3
    assert badges._streak([False, False]) == 0


# ---------------------------------------------------------------- earning and showing badges
def _scatter_round(w, win_x100=0, free=0, capped=False, n=1):
    for _ in range(n):
        w.db.add(ScatterRound(
            code=f"T{w.tag[-5:]}{(w.db.scalar(select(ScatterRound.id).order_by(ScatterRound.id.desc()).limit(1)) or 0) + 1}",
            user_id=w.admin.id, stake_cents=1000, win_cents=win_x100 * 10, win_x100=win_x100, status=TicketStatus.SETTLED,
            spins=1 + free, free_spins=free, top_multiplier=1, capped=capped, seed="s", max_win_x=5000))
        w.db.flush()


def test_a_big_win_earns_its_badges_once_and_they_can_be_shown(world):
    w = world
    have = set(badges.earned_rows(w.db, w.admin.id))
    _scatter_round(w, win_x100=60_000, free=10)                    # a 600x round with a bonus
    new = {b.key for b in badges.sync(w.db, w.admin, w.store, games={"scatter"})}
    assert {"hundred_x", "five_hundred_x"} <= new and "thousand_x" not in new
    assert not (new & have)
    assert badges.sync(w.db, w.admin, w.store, games={"scatter"}) == []      # never awarded twice
    shown = badges.set_showcase(w.db, w.admin, ["five_hundred_x", "hundred_x"], w.store)
    assert [b.key for b in shown] == ["five_hundred_x", "hundred_x"]
    assert [b.key for b in badges.my_showcase(w.db, w.admin, w.store)] == ["five_hundred_x", "hundred_x"]
    badges.set_showcase(w.db, w.admin, ["hundred_x", "five_hundred_x"], w.store)        # swapping order is fine
    assert [b.key for b in badges.my_showcase(w.db, w.admin, w.store)] == ["hundred_x", "five_hundred_x"]


def test_only_three_earned_badges_can_be_shown(world):
    w = world
    _scatter_round(w, win_x100=120_000, free=10, capped=True)
    badges.sync(w.db, w.admin, w.store, games={"scatter"})
    with pytest.raises(ValidationError):
        badges.set_showcase(w.db, w.admin, ["hundred_x", "five_hundred_x", "thousand_x", "max_win"], w.store)
    with pytest.raises(ValidationError):
        badges.set_showcase(w.db, w.admin, ["pit_legend"], w.store)          # not earned
    with pytest.raises(ValidationError):
        badges.set_showcase(w.db, w.admin, ["nonsense"], w.store)


def test_badges_of_a_hidden_game_are_hidden_and_not_awarded(world):
    w = world
    _scatter_round(w, win_x100=60_000)
    badges.sync(w.db, w.admin, w.store, games={"scatter"})
    badges.set_showcase(w.db, w.admin, ["hundred_x"], w.store)
    try:
        w.store.set("games.show_scatter", "false", w.admin.id)
        assert badges.my_showcase(w.db, w.admin, w.store) == []                # not next to the name
        assert all(b.game != "scatter" for b in badges.listed(w.store))        # not on the page
        _scatter_round(w, win_x100=120_000)
        assert badges.sync(w.db, w.admin, w.store, games={"scatter"}) == []    # nothing new while it is off
    finally:
        w.store.set("games.show_scatter", "true", w.admin.id)
    assert [b.key for b in badges.my_showcase(w.db, w.admin, w.store)] == ["hundred_x"]


def test_achievements_page_and_profile_picker_over_http(world):
    w = world
    _scatter_round(w, win_x100=60_000)
    w.db.commit()
    c, _ = login("admin")
    page = c.get("/achievements")
    assert page.status_code == 200 and "Hundred Club" in page.text and 'href="/achievements"' in page.text
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post("/profile/badges", data={"badge": "hundred_x", "csrf_token": token})
    assert r.status_code == 303
    assert 'title="Hundred Club"' in c.get("/profile").text                    # shown next to the name
    too_many = c.post("/profile/badges", data=[("badge", k) for k in ("a", "b", "c", "d")] + [("csrf_token", token)])
    assert too_many.status_code in (400, 422)
