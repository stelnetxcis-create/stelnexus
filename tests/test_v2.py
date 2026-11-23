"""Wallet hub, locations, Pawn/Declarator rules, Admin Panel access, presence."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import Perm, hides_totals, permissions_of
from app.db.models import CashRequest, RequestDirection, Role, User
from app.main import app
from app.services import accounts, agents, cashdesk, events, locations, presence, wallet
from tests.conftest import PASSWORD
from tests.test_network import chain  # noqa: F401  (fixture)


def _login(username):
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    c.get("/login")
    token = c.cookies.get("snx_csrf").split(".")[0]
    assert c.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token}).status_code == 303
    return c


def _csrf(c):
    return c.cookies.get("snx_csrf").split(".")[0]


def _mk(w, role, name, actor=None):
    u = agents.create_agent(w.db, actor or w.admin, username=f"{name}{w.tag}", password=PASSWORD, role=role,
                            full_name=name, settings=w.store)
    u.must_change_password = False
    return u


def _fund(w, user, cents):
    req = cashdesk.create_request(w.db, user, direction=RequestDirection.IN, amount_cents=cents, settings=w.store,
                                  agent_id=user.id, target="wallet", reference="GC-1")
    cashdesk.review(w.db, w.admin, request_id=req.id, approve=True, settings=w.store)


# ------------------------------------------------ declarator / pawn: no money at all
def test_declarator_and_pawn_have_no_balance(chain):
    w = chain
    for role in (Role.DECLARATOR, Role.PAWN):
        u = _mk(w, role, role.value)
        assert not u.has_wallet
        assert accounts.get_by_code(w.db, f"wallet:user:{u.id}") is None
        with pytest.raises(Forbidden):
            accounts.wallet_for_user(w.db, u)
        assert agents.wallet_balance(w.db, u) == 0
        with pytest.raises(Forbidden):
            wallet.transfer(w.db, u, amount_cents=5000, settings=w.store, target_id=w.cavo.id)
        with pytest.raises(Forbidden):
            cashdesk.create_request(w.db, u, direction=RequestDirection.IN, amount_cents=5000, settings=w.store,
                                    agent_id=u.id, target="wallet", reference="x")
        with pytest.raises(ValidationError):                       # nobody can send to them either
            wallet.transfer(w.db, w.master, amount_cents=5000, settings=w.store, target_id=u.id)
        assert Perm.BETS_PLACE not in permissions_of(u)


def test_only_admin_creates_pawns(chain):
    w = chain
    with pytest.raises(Forbidden):
        agents.create_agent(w.db, w.master, username="px" + w.tag, password=PASSWORD, role=Role.PAWN,
                            full_name="P", settings=w.store)
    assert _mk(w, Role.PAWN, "pawn").role == Role.PAWN


def test_pawn_grants_are_per_account_and_default_to_nothing(chain):
    w = chain
    p = _mk(w, Role.PAWN, "pawn")
    assert Perm.PANEL_VIEW not in permissions_of(p) and hides_totals(p)
    agents.set_pawn_grants(w.db, w.admin, p.id, ["cuts:edit", "bogus", "vars:Payout:edit", "totals:view"])
    assert p.grants == ["vars:Payout:view", "vars:Payout:edit", "cuts:view", "cuts:edit", "totals:view"]
    perms = permissions_of(p)
    assert {Perm.PANEL_VIEW, Perm.CUTS_MANAGE, Perm.SETTINGS_MANAGE} <= perms
    assert Perm.AUDIT_VIEW not in perms and Perm.PAWNS_MANAGE not in perms and not hides_totals(p)
    with pytest.raises(Forbidden):
        agents.set_pawn_grants(w.db, w.master, p.id, ["cuts:edit"])


def test_pawn_web_access_follows_grants_and_variable_groups(chain):
    w = chain
    p = _mk(w, Role.PAWN, "pawn")
    agents.set_pawn_grants(w.db, w.admin, p.id, ["vars:Payout:edit", "locations:view"])
    w.db.flush(); w.db.commit()
    c = _login(p.username)
    assert c.get("/admin").status_code == 200
    html = c.get("/admin/variables").text
    assert "Minimum bet" in html and "Tong (house cut)" in html and "Idle session timeout" not in html
    assert c.get("/admin/locations").status_code == 200
    for path in ["/admin/cuts", "/admin/audit", "/admin/pawns", "/clp", "/clp/online", "/wallet", "/dashboard", "/agents"]:
        assert c.get(path).status_code == 403, path
    assert c.get("/change_name").status_code == 200
    # saving only touches granted groups, even if other keys are posted
    before = w.store.get("security.max_login_attempts")
    r = c.post("/admin/variables", data={"csrf_token": _csrf(c), "payout.min_stake_cents": "3000",
                                         "security.max_login_attempts": "19"})
    assert r.status_code == 303
    w.db.expire_all()
    w.store.invalidate()
    assert w.store.get("payout.min_stake_cents") == 3000 and w.store.get("security.max_login_attempts") == before
    c.post("/admin/locations/create", data={"csrf_token": _csrf(c), "name": f"Nope{w.tag}"})   # flashed refusal
    w.db.expire_all()
    assert w.db.scalar(select(locations.Location.id).where(locations.Location.name == f"Nope{w.tag}")) is None
    w.store.set("payout.min_stake_cents", "2000", None)


# ------------------------------------------------ wallet: transfer, requests, history
def test_everyone_with_a_wallet_can_transfer_and_history_shows_both_sides(chain):
    w = chain
    _fund(w, w.kub, 100_000)
    w.store.set("wallet.transfer_scope", "anyone", None)
    wallet.transfer(w.db, w.kub, username=w.master.username.upper(), amount_cents=25_000, settings=w.store,
                    note="thanks")                                  # kubrador -> master: upwards, not downline
    assert agents.wallet_balance(w.db, w.kub) == 75_000 and agents.wallet_balance(w.db, w.master) == 25_000
    out = wallet.history(w.db, w.kub, flow="out")[0]
    inn = wallet.history(w.db, w.master, flow="in")[0]
    assert (out.amount_cents, out.direction, out.label) == (25_000, "out", "Transfer")
    assert (inn.amount_cents, inn.direction) == (25_000, "in")
    s = wallet.summary(w.db, w.kub)
    assert s["in"] == 100_000 and s["out"] == 25_000 and s["net"] == 75_000
    with pytest.raises(ValidationError):
        wallet.transfer(w.db, w.kub, username=w.kub.username, amount_cents=5000, settings=w.store)
    with pytest.raises(Exception):                                  # cannot overdraw
        wallet.transfer(w.db, w.kub, username=w.cavo.username, amount_cents=900_000, settings=w.store)


def test_network_scope_limits_recipients_and_hidden_admin_is_invisible(chain):
    w = chain
    _fund(w, w.kub, 50_000)
    other = _mk(w, Role.MASTER, "othermaster")
    w.store.set("wallet.transfer_scope", "network", None)
    try:
        with pytest.raises(Forbidden):
            wallet.transfer(w.db, w.kub, username=other.username, amount_cents=5000, settings=w.store)
        wallet.transfer(w.db, w.kub, username=w.cavo.username, amount_cents=5000, settings=w.store)   # upline ok
        names = {u.username for u in wallet.recipients(w.db, w.kub, w.store)}
        assert w.cavo.username in names and other.username not in names
    finally:
        w.store.set("wallet.transfer_scope", "anyone", None)
    with pytest.raises(NotFound):
        wallet.transfer(w.db, w.kub, username="admin", amount_cents=5000, settings=w.store)
    assert "admin" not in {u.username for u in wallet.recipients(w.db, w.master, w.store)}


def test_own_wallet_load_and_cash_out_need_the_admin(chain):
    w = chain
    req = cashdesk.create_request(w.db, w.master, direction=RequestDirection.IN, amount_cents=40_000,
                                  settings=w.store, agent_id=w.master.id, target="wallet", reference="GC-9")
    with pytest.raises(Forbidden):                                   # not even a Master approves their own top-up
        cashdesk.review(w.db, w.master, request_id=req.id, approve=True, settings=w.store)
    cashdesk.review(w.db, w.admin, request_id=req.id, approve=True, settings=w.store)
    assert agents.wallet_balance(w.db, w.master) >= 40_000
    before = agents.wallet_balance(w.db, w.master)
    out = cashdesk.create_request(w.db, w.master, direction=RequestDirection.OUT, amount_cents=10_000,
                                  settings=w.store, agent_id=w.master.id, target="wallet", method="bank")
    cashdesk.review(w.db, w.admin, request_id=out.id, approve=True, settings=w.store)
    assert agents.wallet_balance(w.db, w.master) == before - 10_000
    with pytest.raises(ValidationError):
        cashdesk.create_request(w.db, w.master, direction=RequestDirection.OUT, amount_cents=10**9,
                                settings=w.store, agent_id=w.master.id, target="wallet", method="bank")
    with pytest.raises(Forbidden):                                   # only for yourself
        cashdesk.create_request(w.db, w.master, direction=RequestDirection.IN, amount_cents=5000,
                                settings=w.store, agent_id=w.cavo.id, target="wallet", reference="x")


def test_wallet_pages_and_forms(chain):
    w = chain
    _fund(w, w.cavo, 60_000)
    w.db.flush(); w.db.commit()
    c = _login(w.cavo.username)
    for t in ("overview", "load", "cashout", "transfer", "history"):
        assert c.get(f"/wallet?t={t}").status_code == 200, t
    assert 'href="/cashin_out"' not in c.get("/dashboard").text          # not in the side panel any more
    html = c.get("/dashboard").text
    assert "wallet-chip" in html and "/wallet?t=transfer" in html
    r = c.post("/wallet/transfer", data={"csrf_token": _csrf(c), "username": w.kub.username, "amount": "100"})
    assert r.status_code == 303
    r = c.post("/wallet/request", data={"csrf_token": _csrf(c), "direction": "in", "subject": "me",
                                        "method": "gcash", "amount": "100", "reference": "GC-77"})
    assert r.status_code == 303
    w.db.expire_all()
    assert w.db.scalar(select(CashRequest).where(CashRequest.reference == "GC-77")).target == "wallet"
    assert c.get("/wallet?t=approvals").status_code == 200           # falls back to overview: no review rights
    assert "Approvals" not in c.get("/wallet").text.split('class="subtabs"')[1].split("</div>")[0]


# ------------------------------------------------ locations and events
def test_locations_events_and_permissions(chain):
    w = chain
    loc = locations.create_location(w.db, w.master, name=f"Arena {w.tag}", city="Cebu")     # master may register
    with pytest.raises(Conflict):
        locations.create_location(w.db, w.admin, name=f"arena {w.tag}")
    with pytest.raises(Forbidden):
        locations.update_location(w.db, w.master, loc.id, name="x", address=None, city=None, notes=None,
                                  custom_fields=[], default_fight_count=None, default_payout_model=None,
                                  default_house_cut_bps=None)
    # Agents and Sub Agents host: they may register a venue (editing it stays the Admin Panel's job)
    own = locations.create_location(w.db, w.cavo, name=f"Agent Arena {w.tag}")
    with pytest.raises(Forbidden):
        locations.update_location(w.db, w.cavo, own.id, name="x", address=None, city=None, notes=None,
                                  custom_fields=[], default_fight_count=None, default_payout_model=None,
                                  default_house_cut_bps=None)
    locations.delete_location(w.db, w.admin, own.id)
    fields = locations.parse_custom_fields(["Capacity", "Contact", ""], ["800", "09171112222", "ignored"])
    locations.update_location(w.db, w.admin, loc.id, name=loc.name, address="Main St", city="Cebu", notes=None,
                              custom_fields=fields, default_fight_count="8", default_payout_model="fixed",
                              default_house_cut_bps="300")
    with pytest.raises(ValidationError):
        events.create_event(w.db, w.master, title="No venue", event_date=dt.date.today(), settings=w.store)
    ev = events.create_event(w.db, w.master, title="At arena", event_date=dt.date.today(), settings=w.store,
                             location_id=loc.id)
    assert (ev.fight_count, ev.payout_model.value, ev.house_cut_bps, ev.location.name) == (8, "fixed", 300, loc.name)
    hosted = events.create_event(w.db, w.cavo, title="Agent hosted", event_date=dt.date.today(), settings=w.store,
                                 location_id=loc.id)            # an Agent may host (create) an event ...
    assert hosted.created_by_id == w.cavo.id
    assert Perm.EVENTS_MANAGE not in permissions_of(w.cavo)     # ... but not run it: lock/odds/cancel stay Master/Admin
    with pytest.raises(Conflict):
        locations.delete_location(w.db, w.admin, loc.id)               # has events: archive instead
    locations.set_active(w.db, w.admin, loc.id, False)
    with pytest.raises(ValidationError):
        events.create_event(w.db, w.master, title="y", event_date=dt.date.today(), settings=w.store, location_id=loc.id)
    for e in w.db.scalars(select(events.Event).where(events.Event.location_id == loc.id)).all():
        e.location_id = None
    w.db.flush()
    locations.delete_location(w.db, w.admin, loc.id)


def test_events_page_create_button_and_quick_location(chain):
    w = chain
    for u in (w.master, w.cavo):
        u.must_change_password = False
    w.db.flush(); w.db.commit()
    m = _login(w.master.username)
    assert "event-modal" in m.get("/events", follow_redirects=True).text
    r = m.post("/locations/quick", data={"csrf_token": _csrf(m), "name": f"Quick {w.tag}", "city": "Davao"})
    assert r.status_code == 303 and f"Quick {w.tag}" in m.get("/events", follow_redirects=True).text
    cv = _login(w.cavo.username)
    assert "event-modal" in cv.get("/events", follow_redirects=True).text              # Agents host: they get the Create event button
    cv.post(f"/events/{w.event.id}/cancel", data={"csrf_token": _csrf(cv), "reason": "not allowed to run it"})
    w.db.expire_all()
    assert w.db.get(events.Event, w.event.id).status.value != "canceled"   # but cannot run / cancel events
    a = _login("admin")
    page = a.get("/admin/locations").text
    assert f"Quick {w.tag}" in page
    row = w.db.scalar(select(locations.Location).where(locations.Location.name == f"Quick {w.tag}"))
    assert a.post(f"/admin/locations/{row.id}/delete", data={"csrf_token": _csrf(a)}).status_code == 303
    assert a.post("/admin/locations/create", data={"csrf_token": _csrf(a), "name": f"Full {w.tag}", "cf_label": "Cap",
                                                   "cf_value": "5", "default_fight_count": "6"}).status_code == 303
    w.db.expire_all()
    full = w.db.scalar(select(locations.Location).where(locations.Location.name == f"Full {w.tag}"))
    assert full.custom_fields == [{"label": "Cap", "value": "5"}]
    locations.delete_location(w.db, w.admin, full.id)


# ------------------------------------------------ presence + nav
def test_presence_is_admin_only_and_nav_is_reorganised(chain):
    w = chain
    w.cavo.must_change_password = False
    w.db.flush(); w.db.commit()
    _login(w.cavo.username)
    a = _login("admin")
    page = a.get("/clp/online").text
    assert w.cavo.username in page
    with pytest.raises(Forbidden):
        presence.online_accounts(w.db, w.master, w.store)
    nav = a.get("/dashboard").text.split('<nav class="nav">')[1].split("</nav>")[0]
    assert "Cash In/Out" not in nav and "Settings" not in nav and ">Cuts<" not in nav and "Admin Panel" in nav
    cav = _login(w.cavo.username).get("/dashboard").text.split('<nav class="nav">')[1].split("</nav>")[0]
    assert ">Users<" not in cav and ">Players<" not in cav and "Admin Panel" not in cav
