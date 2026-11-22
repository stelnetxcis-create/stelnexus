"""Live polling endpoints, the early-bet panel and the undo toast. Named test_zz_ so it runs last: it leaves a
cancelled event behind and must not disturb tests that count open games."""
import datetime as dt

from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import EventStatus, Fight, FightStatus, Location, Result, Role, Ticket, TicketStatus, User
from app.db.session import session_scope
from app.services import events, settlement
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
        store.set("payout.platform_cut_bps", "0", admin.id)       # the platform cut has its own tests
        load_credit(db, admin, store, player, 50_000_00)
        # the livestream panel bets as the logged-in account, so the admin account needs credit of its own
        from app.services import accounts, ledger
        ledger.post(db, kind="request.in", entries=[(accounts.gcash_clearing(db), +50_000_00),
                                                    (accounts.wallet_for_user(db, admin), -50_000_00)],
                    actor=admin, memo="test load (admin own credit)")
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
    # the panel posts to /bets/live/*; there is no Seller Desk
    assert "data-bp" in html and 'data-place-url="/bets/live/place"' in html
    assert "/sell/place" not in html and "Sell tickets" not in html
    assert 'data-self="1"' in html and 'data-sl="player"' not in html   # bets as the logged-in account, no picker
    assert "Start game" in html and "pre-show" in html


def test_early_bet_follows_the_game_order():
    """Only the current game takes early bets, and only before it starts. Game 2 opens when game 1 is declared."""
    ev_id, fight_id, player_id = _setup()
    with session_scope() as db:
        second_id = db.scalar(select(Fight.id).where(Fight.event_id == ev_id, Fight.number == 2))
    c, r = login("admin")
    tok = _csrf(c)
    hdr = {"Accept": "application/json", "X-CSRF-Token": tok}

    def early(fid, key):
        t = _csrf(c)
        return c.post("/bets/live/place", data={"fight_id": fid, "side": "meron", "amount": "100",
                                                 "idempotency_key": key, "csrf_token": t},
                      headers={"Accept": "application/json", "X-CSRF-Token": t})

    assert early(fight_id, "e-1").status_code == 200                      # current game, pre-show: open
    nxt = early(second_id, "e-2")                                         # later game: not open yet
    assert nxt.status_code >= 400 and "opens when game #1 is done" in nxt.text
    rows = {f["n"]: f for f in c.get(f"/_live/event/{ev_id}").json()["fights"]}
    assert rows[1]["ph"] == "open" and rows[2]["ph"] == "waiting"
    assert rows[1]["mm"] and rows[1]["tt"] >= 10000 and "np" in rows[1]    # live payout figures ride on the poll
    # game 1 goes on air: everything closed
    assert c.post(f"/events/{ev_id}/stream", data={"action": "start", "fight_id": fight_id,
                                                   "csrf_token": _csrf(c)}).status_code in (302, 303)
    page = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
    assert "is on air" in page and 'data-phase="onair"' in page and 'data-live="1"' in page
    late = early(fight_id, "e-3")
    assert late.status_code >= 400 and "game #1 has started" in late.text
    assert early(second_id, "e-4").status_code >= 400                      # still waiting
    # only the current game can go on air
    bad = c.post(f"/events/{ev_id}/stream", data={"action": "start", "fight_id": second_id, "csrf_token": _csrf(c)})
    assert bad.status_code in (303, 409, 422)                              # refused (flash redirect for forms)
    rows = {f["n"]: f for f in c.get(f"/_live/event/{ev_id}").json()["fights"]}
    assert rows[1]["ph"] == "onair" and rows[1]["sd"] and rows[2]["ph"] == "waiting" and not rows[2]["sd"]
    # normal betting is not tied to the stream
    t = _csrf(c)
    normal = c.post("/bets/place", data={"fight_id": fight_id, "side": "meron", "amount": "100", "player_id": player_id,
                                         "idempotency_key": "norm-1", "csrf_token": t}, follow_redirects=False)
    assert normal.status_code in (302, 303)
    # back to pre-show reopens game 1
    assert c.post(f"/events/{ev_id}/stream", data={"action": "reset", "fight_id": fight_id,
                                                   "csrf_token": _csrf(c)}).status_code in (302, 303)
    assert early(fight_id, "e-5").status_code == 200
    # game 1 declared: game 2 becomes the current game and opens; game 3+ would still wait
    with session_scope() as db:
        db.get(Fight, fight_id).status = FightStatus.SETTLED
        db.flush()
    assert early(second_id, "e-6").status_code == 200
    rows = {f["n"]: f for f in c.get(f"/_live/event/{ev_id}").json()["fights"]}
    assert rows[1]["ph"] == "ended" and rows[2]["ph"] == "open"


def test_payout_scene_live_and_final():
    """The payout scene sits in the video area: live figures while betting is open, the real settlement once ended."""
    ev_id, fight_id, player_id = _setup()
    try:
        c, _ = login("admin")
        tok = _csrf(c)
        r = c.post("/bets/live/place", data={"fight_id": fight_id, "side": "meron", "amount": "100",
                                              "idempotency_key": "sc-1", "csrf_token": tok},
                   headers={"Accept": "application/json", "X-CSRF-Token": tok})
        assert r.status_code == 200, r.text
        page = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
        assert "data-pbs" in page and 'data-state="open"' in page and 'data-pbn="mm"' in page
        row = {f["id"]: f for f in c.get(f"/_live/event/{ev_id}").json()["fights"]}[fight_id]
        assert row["pm"] == 10000 and row["tt"] == 10000 and row["cm"] == 1
        with session_scope() as db:
            admin = db.scalar(select(User).where(User.role == Role.ADMIN))
            settlement.declare(db, admin, fight_id=fight_id, result=Result.MERON, settings=SettingsStore(db))
        final = c.get(f"/_live/fight/{fight_id}/final").json()
        assert final["res"] == "meron" and final["tt"] == 10000 and final["tickets"] == 1
        assert {"mult", "paid", "cut", "winners", "np"} <= set(final)
        page = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
        assert 'data-state="ended"' in page and "MERON WINS" in page
    finally:
        _cleanup(event_id=ev_id)


def test_payout_max_llamado_and_dehado():
    """Pool model: the Llamado side pays at most 1.50x and the Dehado side at most 2.50x; the rest stays with the house."""
    from app.core.settings_registry import SettingsStore
    from app.services import betting
    snap = {"meron": 9_000_00, "wala": 1_000_00, "main": 10_000_00}
    with session_scope() as db:
        store = SettingsStore(db)
        store.set("payout.max_llamado_bps", "15000", None)
        store.set("payout.max_dehado_bps", "25000", None)
        m = betting.side_multipliers(snap, 500, store, "meron")
        # natural: net 9,500 -> meron 1.055x (below the 1.5x max), wala 9.50x -> capped at 2.50x
        assert m["meron"]["bps"] == 10555 and not m["meron"]["capped"]
        assert m["wala"]["bps"] == 25000 and m["wala"]["capped"] and m["wala"]["natural"] == 95000
        # no Llamado set: the bigger pool is the favourite
        m2 = betting.side_multipliers({"meron": 4_000_00, "wala": 6_000_00, "main": 10_000_00}, 0, store)
        assert m2["wala"]["bps"] == 15000 and m2["wala"]["capped"]      # 1.667x -> 1.50x
        assert m2["meron"]["bps"] == 25000 and not m2["meron"]["capped"]  # exactly 2.50x


def test_account_bets_as_itself_from_its_own_credit():
    """No player picker: an Agent / Master bets from their OWN wallet; the stake and payout use it (undo is dev-only),
    and the bettor earns no commission on their own bet."""
    from app.db.models import Role as R
    from app.services import accounts, agents, betting, ledger
    from tests.conftest import PASSWORD as PW
    ev_id, fight_id, _ = _setup()
    try:
        tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-8:]
        name = f"sb{tag}"
        with session_scope() as db:
            store = SettingsStore(db)
            admin = db.scalar(select(User).where(User.role == Role.ADMIN))
            master = agents.create_agent(db, admin, username=f"m{name}", password=PW, role=R.MASTER,
                                         full_name="M" + name.upper(), settings=store)
            user = agents.create_agent(db, master, username=name, password=PW, role=R.AGENT,
                                       full_name=name.upper(), settings=store)
            user.must_change_password = False
            wallet = accounts.wallet_for_user(db, user)
            ledger.post(db, kind="request.in", entries=[(accounts.gcash_clearing(db), +50_000), (wallet, -50_000)],
                        actor=admin, memo="test load")
            uid = user.id
        c, r = login(name)
        assert r.status_code == 303
        page = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
        assert 'data-self="1"' in page and 'data-sl="player"' not in page and "Your credit" in page
        tok = _csrf(c)
        hdr = {"Accept": "application/json", "X-CSRF-Token": tok}
        put = c.post("/bets/live/place", headers=hdr, data={"fight_id": fight_id, "side": "meron", "amount": "100",
                                                             "idempotency_key": "self-1", "csrf_token": tok})
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["balance"] == 50_000 - 10_000
        again = c.post("/bets/live/place", headers=hdr, data={"fight_id": fight_id, "side": "meron", "amount": "100",
                                                               "idempotency_key": "self-1", "csrf_token": tok})
        assert again.json()["ticket"]["id"] == body["ticket"]["id"]          # a double tap never bets twice
        with session_scope() as db:
            tk = db.get(Ticket, body["ticket"]["id"])
            assert tk.user_id == uid and tk.player_id is None and tk.agent_id == uid
            assert accounts.wallet_for_user(db, db.get(User, uid)).balance_cents == 40_000
            # a win or a refund lands in the bettor's own wallet, not the central account
            assert betting.beneficiary_account(db, tk).id == accounts.wallet_for_user(db, db.get(User, uid)).id
        # undoing a placed bet is dev-only: an Agent cannot take it back, so the stake stays on the bet
        undo = c.post(f"/bets/live/undo/{body['ticket']['id']}", headers=hdr)
        assert undo.status_code == 403
        with session_scope() as db:
            assert accounts.wallet_for_user(db, db.get(User, uid)).balance_cents == 40_000
        # a bet that is bigger than the credit is refused, not silently charged to someone else
        big = c.post("/bets/live/place", headers=hdr, data={"fight_id": fight_id, "side": "wala", "amount": "9999",
                                                             "idempotency_key": "self-2", "csrf_token": tok})
        assert big.status_code >= 400
    finally:
        _cleanup(ev_id)


def test_account_daily_limit_caps_an_agent_betting_for_itself():
    """account.daily_bet_limit_cents caps own-credit bets (both games); 0 = unlimited; the Admin is never capped."""
    from app.db.models import Role as R
    from app.services import accounts, agents, ledger
    ev_id, fight_id, _ = _setup()
    try:
        tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-8:]
        name = f"lm{tag}"
        with session_scope() as db:
            store = SettingsStore(db)
            admin = db.scalar(select(User).where(User.role == Role.ADMIN))
            master = agents.create_agent(db, admin, username=f"m{name}", password=PASSWORD, role=R.MASTER,
                                         full_name="M" + name.upper(), settings=store)
            user = agents.create_agent(db, master, username=name, password=PASSWORD, role=R.AGENT,
                                       full_name=name.upper(), settings=store)
            user.must_change_password = False
            ledger.post(db, kind="request.in", entries=[(accounts.gcash_clearing(db), +50_000),
                                                        (accounts.wallet_for_user(db, user), -50_000)],
                        actor=admin, memo="test load")
            store.set("account.daily_bet_limit_cents", "15000", admin.id)      # P150 a day
        c, _ = login(name)
        tok = _csrf(c)
        hdr = {"Accept": "application/json", "X-CSRF-Token": tok}

        def bet(key, amount):
            return c.post("/bets/live/place", headers=hdr, data={"fight_id": fight_id, "side": "meron",
                          "amount": amount, "idempotency_key": key, "csrf_token": tok})
        assert bet("lim-1", "100").status_code == 200          # P100 of P150
        over = bet("lim-2", "100")                              # would reach P200
        assert over.status_code == 403 and "Daily betting limit" in over.text
        assert bet("lim-3", "50").status_code == 200            # exactly P150: still allowed
        with session_scope() as db:                             # 0 = unlimited again
            SettingsStore(db).set("account.daily_bet_limit_cents", "0", db.scalar(select(User.id).where(User.role == Role.ADMIN)))
        assert bet("lim-4", "100").status_code == 200
    finally:
        with session_scope() as db:
            SettingsStore(db).set("account.daily_bet_limit_cents", "0", db.scalar(select(User.id).where(User.role == Role.ADMIN)))
        _cleanup(ev_id)


def test_fight_view_has_the_dock_and_a_fixed_scene():
    """Chat and the bet slip open from a bottom dock bar (dock.js); the slip shows each side's live multiple and a
    'Wins about' line; the chat pill no longer claims ON AIR before the game is on air."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    bundles = (root / "app" / "asset_bundles.json").read_text()
    assert '"dock"' in bundles
    dock = (root / "app" / "static" / "js" / "dock.js").read_text()
    assert "dk-bar" in dock and "dk-sheet" in dock and "dk-side" in dock
    slip = (root / "app" / "web" / "templates" / "_betpanel.html").read_text()
    assert 'data-bp-x="meron"' in slip and 'data-bp-x="wala"' in slip and "data-bp-est" in slip and "data-bp-other" in slip
    page = (root / "app" / "web" / "templates" / "change_name.html").read_text()
    assert "js('dock')" in page and "ON AIR" not in page
    css = (root / "app" / "static" / "css" / "store.css").read_text()
    assert re.search(r"\.fc-stagewrap\.dk-sheet \.fc-overlay\{", css) and "dk-short" in css
