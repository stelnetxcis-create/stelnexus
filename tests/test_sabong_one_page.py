"""Sabong has one page per event, like a Bola draw: watch and bet on /events/{id}. What an operator does to the event
(names, Llamado, corrections, summary, expenses, cancel, delete) is in the Declarator part of the Staff Panel.

Before, the Fight View (/change_name) and the betting sheet (/events/{id}) were two pages that linked to each other.
"""
import re

import pytest
from sqlalchemy import select

from app.db.models import Event, EventStatus, Role
from app.db.session import session_scope
from tests.test_bola_dev_gate import _users
from tests.test_web import login


def _event_status(event_id: int, status: EventStatus) -> None:
    with session_scope() as db:
        db.get(Event, event_id).status = status
        db.commit()


def test_the_event_page_carries_play_and_manage_together(world):
    c, _ = login("admin")
    w = world
    page = c.get(f"/events/{w.event.id}?fight_id={w.games[0].id}")
    assert page.status_code == 200
    html = page.text
    assert 'data-region="fight-panel"' in html                                  # the fight: watch, bet, declare
    assert 'data-region="event-head"' in html and 'data-region="event-body"' in html    # the event: pools, summary
    assert 'id="pools"' in html and 'id="manage"' not in html                   # all fights stay; cancel event is operator tooling
    assert "Declare result" not in html and "Close betting (lock)" not in html and "Save names" not in html
    assert 'id="summary"' not in html and "Event expenses" not in html and "/dev/purge" not in html
    assert f'href="/staff/declarator/events/{w.event.id}/tools' in html         # the way to the operator page
    assert 'event-nav' not in html and 'href="#sell"' not in html              # no tab buttons on the page
    assert 'id="sell"' not in html and "Sell ticket" not in html                # and no Sell ticket card for anyone
    assert "Betting sheet" not in html and "/change_name?" not in html          # no second page to hop to


def test_the_old_fight_view_urls_land_on_the_one_page_with_their_query():
    c, _ = login("admin")
    r = c.get("/change_name?event_id=7&fight_id=21&gs=3")
    assert r.status_code == 303
    assert r.headers["location"] == "/events/7?fight_id=21&gs=3"
    r = c.get("/change_name?id=7")
    assert r.status_code == 303 and r.headers["location"] == "/events/7"
    r = c.get("/change_name")                                                   # no event: the picker
    assert r.status_code == 303 and r.headers["location"] == "/games/sabong/live?pick=1"
    assert c.get("/games/sabong/live?event_id=7&fight_id=21").headers["location"] == "/events/7?fight_id=21"
    assert c.get("/events/create").headers["location"] == "/games/sabong/live?pick=1&create=1"


def test_play_goes_straight_into_the_one_open_event_and_pick_shows_the_cards(world):
    c, _ = login("admin")
    with session_scope() as db:
        others = [e for e in db.scalars(select(Event)).all() if e.id != world.event.id and e.status == EventStatus.OPEN]
        for e in others:                                                        # leave exactly one open event
            e.status = EventStatus.CLOSED
        db.get(Event, world.event.id).status = EventStatus.OPEN
        db.commit()
    r = c.get("/games/sabong/live")
    assert r.status_code == 303 and r.headers["location"] == f"/events/{world.event.id}"
    cards = c.get("/games/sabong/live?pick=1")
    assert cards.status_code == 200 and f'href="/events/{world.event.id}"' in cards.text
    # with two open events there is no single obvious one, so Play shows the cards
    with session_scope() as db:
        extra = Event(title="Second open", event_date=world.event.event_date, status=EventStatus.OPEN, fight_count=1,
                      payout_model=world.event.payout_model)
        db.add(extra)
        db.commit()
        extra_id = extra.id
    try:
        assert c.get("/games/sabong/live").status_code == 200
    finally:
        with session_scope() as db:
            db.delete(db.get(Event, extra_id))
            db.commit()


def test_the_page_opens_on_the_fight_that_is_happening_now(world):
    c, _ = login("admin")
    page = c.get(f"/events/{world.event.id}")
    assert page.status_code == 200 and "fv-chip" in page.text                   # a fight is open without ?fight_id
    assert c.get(f"/events/{world.event.id}?fight_id={world.games[1].id}").status_code == 200
    assert c.get(f"/events/{world.event.id}?fight_id=99999999").status_code in (404, 422)   # never a 500
    other = world.games[0]
    assert c.get(f"/events/999999?fight_id={other.id}").status_code == 404


def test_event_regions_refresh_without_loading_a_fight(world):
    """A refresh of the event head / body never needs the fight panel, so it must not compute it."""
    c, _ = login("admin")
    headers = {"X-Requested-With": "snx", "X-Fragment": "event-body"}
    r = c.get(f"/events/{world.event.id}", headers=headers)
    assert r.status_code == 200 and r.headers.get("x-fragment") == "event-body"
    assert "All fights" in r.text and "fight-panel" not in r.text


@pytest.mark.parametrize("who,sees_manage,sees_money", [
    ("master", True, True), ("cavo", False, True), ("kub", False, True), ("decl", False, False)])
def test_each_role_sees_only_its_part_of_the_event_tools(world, who, sees_manage, sees_money):
    accounts = _users()
    c, _ = login(accounts[who])
    page = c.get(f"/events/{world.event.id}?fight_id={world.games[0].id}")
    assert page.status_code == 200, who
    assert 'id="manage"' not in page.text and "/dev/purge" not in page.text, who   # nothing operator-only on the Sabong page
    r = c.get(f"/staff/declarator/events/{world.event.id}/tools?fight_id={world.games[0].id}")
    if who in ("cavo", "kub"):                                                  # Agents and Sub Agents have no event tools
        assert r.status_code in (302, 303, 403), who
        return
    assert r.status_code == 200, who
    html = r.text
    assert ('id="manage"' in html) is sees_manage, (who, "cancel-event card follows events.manage")
    if not sees_money:                                                          # a Declarator never sees money
        assert 'id="summary"' not in html and "Event expenses" not in html, who
    assert "/dev/purge" not in html, who                                        # the Delete cards are the Admin's alone


def test_the_admin_alone_gets_the_delete_event_and_fight_cards(world):
    c, _ = login("admin")
    html = c.get(f"/staff/declarator/events/{world.event.id}/tools?fight_id={world.games[0].id}").text
    assert "/dev/purge/event/" in html and "/dev/purge/fight/" in html and 'id="manage"' in html
    assert "Save names" in html and "Llamado (favourite)" in html


def test_tools_forms_come_back_to_the_tools_page_and_only_to_staff_paths(world):
    c, _ = login("admin")
    tok = c.cookies.get("snx_csrf").split(".")[0]
    back = f"/staff/declarator/events/{world.event.id}/tools?fight_id={world.games[0].id}"
    r = c.post(f"/fights/{world.games[0].id}/llamado", data={"csrf_token": tok, "side": "meron", "return_to": back},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back
    for evil in ("https://evil.example/", "//evil.example", "/admin", "/staff/\r\nSet-Cookie: x=1"):
        r = c.post(f"/fights/{world.games[0].id}/llamado", data={"csrf_token": tok, "side": "meron", "return_to": evil},
                   follow_redirects=False)
        assert r.status_code == 303 and not r.headers["location"].startswith(("http", "//")) \
            and r.headers["location"].startswith("/events/"), evil


def test_event_status_buttons_follow_the_right_and_return_to_the_page(world):
    c, _ = login("admin")
    tok = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post(f"/events/{world.event.id}/status", data={"csrf_token": tok, "status": "open"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/events/{world.event.id}"
    html = c.get(f"/staff/declarator/events/{world.event.id}/tools").text
    assert f"/events/{world.event.id}/status" in html                           # the status control is on the tools page
    assert f"/events/{world.event.id}/status" not in c.get(f"/events/{world.event.id}").text
