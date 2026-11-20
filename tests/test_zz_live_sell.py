"""Live polling endpoints, the seller desk and the undo toast. Named test_zz_ so it runs last: it leaves a
cancelled event behind and must not disturb tests that count open games."""
import datetime as dt

from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import EventStatus, Fight, Location, Role, Ticket, TicketStatus, User
from app.db.session import session_scope
from app.services import events
from app.services import players as player_service
from tests.conftest import PASSWORD, load_credit
from tests.test_web import login


def _csrf(c):
    return c.cookies.get("snx_csrf").split(".")[0]


def _setup():
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
        player = player_service.register_online_player(
            db, admin, username=f"zz{tag}", password=PASSWORD, full_name="Zz Player",
            birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store)
        player.kyc_verified = True
        db.flush()
        load_credit(db, admin, store, player, 50_000_00)
        # earlier tests may have created venues; once one exists an event must name its venue
        loc = db.scalar(select(Location).where(Location.is_active.is_(True)))
        extra = {"location_id": loc.id} if loc else {}
        ev = events.create_event(db, admin, title="Live test", event_date=dt.date.today(), settings=store,
                                 fight_count=2, **extra)
        events.set_event_status(db, admin, ev.id, EventStatus.OPEN)
        first = db.scalar(select(Fight).where(Fight.event_id == ev.id).order_by(Fight.number))
        return ev.id, first.id, player.id


def _cleanup(event_id):
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        events.cancel_event(db, admin, event_id, reason="test cleanup", settings=SettingsStore(db))


def test_live_event_json_and_unchanged_short_circuit():
    event_id, fight_id, _ = _setup()
    try:
        c, _ = login("admin")
        r = c.get(f"/_live/event/{event_id}", headers={"Accept": "application/json"})
        assert r.status_code == 200
        body = r.json()
        assert [f["n"] for f in body["fights"]] == [1, 2] and body["fights"][0]["st"] == "open"
        again = c.get(f"/_live/event/{event_id}?rev={body['rev']}", headers={"Accept": "application/json"})
        assert again.json() == {"same": True, "rev": body["rev"]}
    finally:
        _cleanup(event_id)


def test_live_endpoints_need_a_login():
    from fastapi.testclient import TestClient
    from tests.test_web import app
    r = TestClient(app).get("/_live/lobby", headers={"Accept": "application/json"}, follow_redirects=False)
    assert r.status_code in (401, 403)


def test_seller_desk_sells_then_undoes_a_ticket():
    event_id, fight_id, player_id = _setup()
    try:
        c, _ = login("admin")
        page = c.get(f"/sell?event_id={event_id}")
        assert page.status_code == 200 and 'id="sell"' in page.text
        headers = {"Accept": "application/json", "X-CSRF-Token": _csrf(c)}
        sold = c.post("/sell/place", headers=headers, data={
            "fight_id": fight_id, "side": "meron", "amount": "100", "player_id": player_id, "idempotency_key": "zz-key-1"})
        assert sold.status_code == 200, sold.text
        ticket = sold.json()["ticket"]
        assert ticket["stake"] == 10_000 and ticket["side"] == "meron"
        # the same idempotency key must never sell twice (double tap on a tablet)
        twice = c.post("/sell/place", headers=headers, data={
            "fight_id": fight_id, "side": "meron", "amount": "100", "player_id": player_id, "idempotency_key": "zz-key-1"})
        assert twice.status_code == 200 and twice.json()["ticket"]["id"] == ticket["id"]
        live = c.get(f"/_live/event/{event_id}", headers={"Accept": "application/json"}).json()
        assert live["fights"][0]["pm"] == 10_000 and live["fights"][0]["cm"] == 1
        undo = c.post(f"/sell/undo/{ticket['id']}", headers=headers)
        assert undo.status_code == 200 and undo.json()["ok"] is True
        with session_scope() as db:
            assert db.get(Ticket, ticket["id"]).status == TicketStatus.CANCELED
    finally:
        _cleanup(event_id)


def test_seller_desk_rejects_a_bad_sale_with_json_not_a_redirect():
    event_id, fight_id, player_id = _setup()
    try:
        c, _ = login("admin")
        r = c.post("/sell/place", headers={"Accept": "application/json", "X-CSRF-Token": _csrf(c)}, data={
            "fight_id": fight_id, "side": "nonsense", "amount": "100", "player_id": player_id})
        assert r.status_code == 422 and "detail" in r.json()
    finally:
        _cleanup(event_id)


def test_locking_a_game_offers_an_undo_toast():
    event_id, fight_id, _ = _setup()
    try:
        c, _ = login("admin")
        r = c.post(f"/fights/{fight_id}/lock", data={"csrf_token": _csrf(c), "locked": "1"})
        assert r.status_code == 303
        page = c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}")
        assert f'data-undo-action="/fights/{fight_id}/lock"' in page.text
        assert '"locked":"0"' in page.text.replace(" ", "")
    finally:
        _cleanup(event_id)


def test_fight_view_has_strip_early_bet_panel_and_no_seller_panel():
    ev_id, fight_id, player_id = _setup()
    c, r = login("admin")
    assert r.status_code == 303
    html = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
    assert "data-fv-strip" in html and 'class="fv-chip' in html and "data-vp-orient" in html
    assert "game-tile" not in html
    # the panel is the EARLY BET panel (posts to /bets/early/*), the Seller Desk endpoints are not wired to this page
    assert "data-bp" in html and 'data-place-url="/bets/early/place"' in html
    assert "/sell/place" not in html and "Sell tickets" not in html
    assert f'data-id="{player_id}"' in html
    assert "Start stream" in html and "Pre-show" in html


def test_early_bet_closes_when_stream_starts():
    ev_id, fight_id, player_id = _setup()
    c, r = login("admin")
    tok = _csrf(c)
    body = {"fight_id": fight_id, "side": "meron", "amount": "100", "player_id": player_id,
            "idempotency_key": "early-1", "csrf_token": tok}
    ok = c.post("/bets/early/place", data=body, headers={"Accept": "application/json", "X-CSRF-Token": tok})
    assert ok.status_code == 200 and ok.json()["ok"], ok.text
    # start the stream: the panel disappears, the endpoint refuses, the live poll says so
    assert c.post(f"/events/{ev_id}/stream", data={"action": "start", "csrf_token": tok}).status_code in (302, 303)
    page = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
    assert "data-bp" not in page and "Stream is live" in page
    tok = _csrf(c); body["csrf_token"] = tok
    body["idempotency_key"] = "early-2"
    late = c.post("/bets/early/place", data=body, headers={"Accept": "application/json", "X-CSRF-Token": tok})
    assert late.status_code >= 400 and "stream has started" in late.text
    assert c.get(f"/_live/event/{ev_id}").json()["event"]["live"] is True
    # ordinary betting elsewhere is unchanged by the stream flag
    tok = _csrf(c); body["csrf_token"] = tok
    normal = c.post("/bets/place", data={**body, "idempotency_key": "norm-1"}, follow_redirects=False)
    assert normal.status_code in (302, 303)
    # back to pre-show reopens early bets
    assert c.post(f"/events/{ev_id}/stream", data={"action": "reset", "csrf_token": _csrf(c)}).status_code in (302, 303)
    tok = _csrf(c); body["csrf_token"] = tok
    body["idempotency_key"] = "early-3"
    again = c.post("/bets/early/place", data=body, headers={"Accept": "application/json", "X-CSRF-Token": tok})
    assert again.status_code == 200
