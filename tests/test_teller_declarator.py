"""Teller (GCash wallet desk), Declarator opens events, cancel logs are dev-only, NBA tile."""
import datetime as dt

from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import EventStatus, RequestDirection, RequestStatus, Role, User
from app.services import agents, cashdesk, events
from tests.conftest import PASSWORD
from tests.test_web import login


def _mk(db, admin, store, role, name, **kw):
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-8:]
    u = agents.create_agent(db, admin, username=f"{name}{tag}", password=PASSWORD, role=role,
                            full_name=name, settings=store, **kw)
    u.must_change_password = False
    db.flush()
    return u


def test_teller_approves_requests_and_sees_nothing_else(world):
    db, admin, store = world.db, world.admin, world.store
    teller = _mk(db, admin, store, Role.TELLER, "tl", gcash_number="09171234567")
    req = cashdesk.create_request(db, admin, direction=RequestDirection.IN, amount_cents=50_000,
                                  settings=store, player_id=world.player.id, method="gcash",
                                  reference="GC-TELLER-1")
    db.commit()
    c, r = login(teller.username)
    assert r.status_code == 303 and r.headers["location"].startswith("/wallet")
    page = c.get("/wallet?t=approvals")
    assert page.status_code == 200 and "GC-TELLER-1" in page.text
    assert c.get("/agents").status_code == 200                       # Users list: Admin and Teller
    for path in ("/events", "/betlogs", "/cancelT", "/bola", "/admin", "/clp", "/cashflow"):
        assert c.get(path).status_code in (403, 404), path           # the wallet desk and nothing else
    token = c.cookies.get("snx_csrf").split(".")[0]
    done = c.post(f"/wallet/{req.id}/review",
                  data={"decision": "approve", "csrf_token": token})
    assert done.status_code == 303
    db.expire_all()
    assert db.get(type(req), req.id).status == RequestStatus.APPROVED


def test_teller_without_gcash_cannot_approve(world):
    import pytest
    from app.core.errors import Forbidden
    from app.services import gcash
    db, admin, store = world.db, world.admin, world.store
    teller = _mk(db, admin, store, Role.TELLER, "tn", gcash_number="09171234567")
    req = cashdesk.create_request(db, admin, direction=RequestDirection.IN, amount_cents=50_000,
                                  settings=store, player_id=world.player.id, method="gcash",
                                  reference="GC-TELLER-2")
    teller.gcash_enc = None
    for account in gcash.list_accounts(db, owner_id=teller.id):   # no live number left in the pool
        account.is_active = False
    db.flush()
    with pytest.raises(Forbidden):
        cashdesk.review(db, teller, request_id=req.id, approve=True, settings=store)


def test_declarator_opens_an_event_but_cannot_close_it(world):
    db, admin, store = world.db, world.admin, world.store
    decl = _mk(db, admin, store, Role.DECLARATOR, "dc")
    events.set_event_status(db, admin, world.event.id, EventStatus.DRAFT)
    db.commit()
    c, _ = login(decl.username)
    assert "Open event" in c.get(f"/events/{world.event.id}").text
    token = c.cookies.get("snx_csrf").split(".")[0]
    bad = c.post(f"/events/{world.event.id}/status", data={"status": "closed", "csrf_token": token})
    db.expire_all()
    assert events.get_event(db, world.event.id).status == EventStatus.DRAFT   # refused, not closed
    ok = c.post(f"/events/{world.event.id}/status", data={"status": "open", "csrf_token": token})
    assert ok.status_code == 303, ok.text[:300]
    db.expire_all()
    assert events.get_event(db, world.event.id).status == EventStatus.OPEN


def test_cancel_logs_are_admin_only_and_nba_tile_is_on_the_dashboard(world):
    db, admin, store = world.db, world.admin, world.store
    from app.db.models import Role as R
    master = _mk(db, admin, store, R.MASTER, "ms")
    db.commit()
    m, _ = login(master.username)
    assert m.get("/cancelT").status_code == 403 and m.get("/bola/cancelled").status_code == 403
    a, _ = login("admin")
    assert a.get("/cancelT").status_code == 200 and a.get("/bola/cancelled").status_code == 200
    html = a.get("/dashboard").text
    assert 'data-game="nba"' in html and "Coming soon" in html
