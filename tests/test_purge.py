"""DEV purge: Admin can hard-delete events / games / draws; others cannot; ledger is untouched."""
import datetime as dt
import re

import pytest
from sqlalchemy import func, select

from app.core.settings_registry import SettingsStore
from app.db.models import Event, Fight, LedgerEntry, Role, User
from app.db.session import session_scope
from app.services import agents, events
from tests.conftest import PASSWORD
from tests.test_web import login


def csrf(client, path="/dashboard"):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get(path).text).group(1)


@pytest.fixture(scope="module")
def admin_c():
    return login("admin")[0]


def _event(n=3):
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        ev = events.create_event(db, admin, title="Purge me", event_date=dt.date(2026, 12, 2), settings=store, fight_count=n)
        db.flush()
        return ev.id


def test_delete_one_game_and_whole_event(admin_c):
    eid = _event(3)
    page = admin_c.get(f"/events/{eid}").text
    assert f"/dev/purge/event/{eid}" in page
    with session_scope() as db:
        fids = [f.id for f in db.scalars(select(Fight).where(Fight.event_id == eid).order_by(Fight.number))]
        ledger_before = db.scalar(select(func.count(LedgerEntry.id)))
    game_page = admin_c.get(f"/change_name?event_id={eid}&fight_id={fids[0]}").text
    assert f"/dev/purge/fight/{fids[0]}" in game_page
    assert all(k in game_page for k in ('data-fc-overlay', 'data-fc-toggle', 'data-fc-alpha', 'data-fc-grip', 'data-stage'))      # chat floats + collapses
    assert admin_c.post(f"/dev/purge/fight/{fids[0]}", data={"csrf_token": csrf(admin_c)}).status_code == 303
    with session_scope() as db:
        assert db.get(Fight, fids[0]) is None
        assert db.get(Event, eid).fight_count == 2
    assert admin_c.post(f"/dev/purge/event/{eid}", data={"csrf_token": csrf(admin_c)}).status_code == 303
    with session_scope() as db:
        assert db.get(Event, eid) is None
        assert db.scalar(select(func.count(Fight.id)).where(Fight.event_id == eid)) == 0
        assert db.scalar(select(func.count(LedgerEntry.id))) == ledger_before


def test_non_admin_is_refused():
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        m = agents.create_agent(db, admin, username="pg_master", password=PASSWORD, role=Role.MASTER,
                                full_name="PG", settings=store)
        m.must_change_password = False
    eid = _event(2)
    c = login("pg_master")[0]
    assert c.post(f"/dev/purge/event/{eid}", data={"csrf_token": csrf(c)}).status_code in (403, 303)
    with session_scope() as db:
        assert db.get(Event, eid) is not None


def test_delete_draw(admin_c):
    from app.db.models import BolaDraw, Location
    from app.services import bola
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        loc = db.scalar(select(Location).limit(1))
        if loc is None:
            pytest.skip("no location seeded")
        d = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date(2030, 1, 1), slot=1,
                             settings=store, draw_time="14:00")
        db.flush(); did = d.id
    assert f"/dev/purge/draw/{did}" in admin_c.get(f"/bola/draws/{did}").text
    assert admin_c.post(f"/dev/purge/draw/{did}", data={"csrf_token": csrf(admin_c)}).status_code == 303
    with session_scope() as db:
        assert db.get(BolaDraw, did) is None
