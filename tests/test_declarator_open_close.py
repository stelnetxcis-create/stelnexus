"""A Declarator opens and closes games: events (sabong) and number-game draws. Nothing beyond that."""
import datetime as dt

import pytest

from app.core.config import get_settings
from app.core.rbac import Perm, permissions_for
from app.db.models import DrawStatus, EventStatus, Role
from app.services import bola, events, locations
from tests.test_declarator_desk import JSON, _desk


@pytest.fixture()
def gate_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", False)


@pytest.fixture()
def draw(world, gate_off):
    """An OPEN number-game draw. Its area is switched off afterwards: an active area makes every later
    event fixture demand a location."""
    loc = locations.create_location(world.db, world.admin, name=f"OC Area {world.tag}")
    d = bola.create_draw(world.db, world.admin, location_id=loc.id, draw_date=dt.date.today(), slot=1,
                         settings=world.store, status=DrawStatus.OPEN,
                         scheduled_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2))
    world.store.set("bola.enabled", "true", world.admin.id)
    world.db.commit()
    try:
        yield d
    finally:
        world.db.rollback()
        loc.is_active = False
        world.db.commit()


def _h(c):
    """Fresh headers: every page render re-issues the CSRF cookie, so a token read earlier goes stale."""
    return {"X-CSRF-Token": c.cookies.get("snx_csrf").split(".")[0], **JSON}


def _event_status(world, event_id):
    world.db.expire_all()
    return events.get_event(world.db, event_id).status


def _draw_status(world, draw_id):
    world.db.expire_all()
    return bola.get_draw(world.db, draw_id).status


def test_permissions_are_narrow(gate_off):
    dec, pawn = permissions_for(Role.DECLARATOR), permissions_for(Role.PAWN)
    assert {Perm.EVENTS_OPEN, Perm.BOLA_OPEN, Perm.FIGHTS_LOCK} <= dec
    assert not ({Perm.EVENTS_MANAGE, Perm.BOLA_MANAGE, Perm.BETS_PLACE, Perm.BOLA_PLACE} & dec)
    assert Perm.BOLA_OPEN not in pawn                      # a Pawn only gets it as an Admin grant
    assert Perm.BOLA_OPEN in permissions_for(Role.MASTER) and Perm.BOLA_OPEN in permissions_for(Role.ADMIN)


def test_event_open_and_close_from_the_event_page(world):
    c, _ = _desk(world)
    ev = world.event.id
    assert c.post(f"/events/{ev}/status", data={"status": "closed"}, headers=_h(c)).status_code in (200, 303)
    assert _event_status(world, ev) == EventStatus.CLOSED
    assert c.post(f"/events/{ev}/status", data={"status": "open"}, headers=_h(c)).status_code in (200, 303)
    assert _event_status(world, ev) == EventStatus.OPEN
    for refused in ("draft", "canceled"):                   # drafting / cancelling stays with the managers
        assert c.post(f"/events/{ev}/status", data={"status": refused}, headers=_h(c)).status_code == 403
    assert _event_status(world, ev) == EventStatus.OPEN


def test_event_open_and_close_from_the_desk(world):
    c, _ = _desk(world)
    ev = world.event.id
    assert c.post(f"/staff/declarator/events/{ev}/close", headers=_h(c)).json()["ok"] is True
    assert _event_status(world, ev) == EventStatus.CLOSED
    assert c.post(f"/staff/declarator/events/{ev}/open", headers=_h(c)).json()["ok"] is True
    assert _event_status(world, ev) == EventStatus.OPEN


def test_number_game_draw_open_and_close(world, draw):
    c, _ = _desk(world)
    page = c.get(f"/bola/draws/{draw.id}")
    assert page.status_code == 200 and f"/bola/draws/{draw.id}/lock" in page.text   # the Close betting button
    assert c.post(f"/bola/draws/{draw.id}/lock", headers=_h(c)).status_code in (200, 303)
    assert _draw_status(world, draw.id) == DrawStatus.LOCKED
    assert c.post(f"/staff/declarator/bola/{draw.id}/open", headers=_h(c)).json()["ok"] is True
    assert _draw_status(world, draw.id) == DrawStatus.OPEN
    assert c.post(f"/staff/declarator/bola/{draw.id}/lock", headers=_h(c)).json()["ok"] is True
    assert _draw_status(world, draw.id) == DrawStatus.LOCKED
    board = c.get("/_live/declarator", headers=JSON).json()
    assert board["can"]["bola_open"] is True and board["can"]["open_event"] is True


def test_declarator_still_cannot_void_or_create_draws(world, draw):
    c, _ = _desk(world)
    assert c.post(f"/bola/draws/{draw.id}/void", data={"reason": "no"}, headers=_h(c)).status_code == 403
    assert c.post("/bola/draws/create", data={}, headers=_h(c)).status_code == 403
    assert _draw_status(world, draw.id) == DrawStatus.OPEN
