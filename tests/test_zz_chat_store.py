"""Store (Battle Pass + Chat Token) and the livestream chat inside the fight view.
Named test_zz_ so it runs after the tests that count open games."""
import datetime as dt

from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import ChatMessage, ChatPass, ChatToken, EventStatus, Fight, FightStatus, Location, Result, Role, User
from app.db.session import session_scope
from app.services import accounts, agents, events, ledger, settlement
from app.services import store as store_svc
from tests.conftest import PASSWORD
from tests.test_web import login

JSON = {"Accept": "application/json"}


def _csrf(c):
    return c.cookies.get("snx_csrf").split(".")[0]


def _tag():
    return f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]


def _setup(credit=2_000_00):
    """A Master with credit, plus an open event with two games."""
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        name = f"cs{_tag()}"
        user = agents.create_agent(db, admin, username=name, password=PASSWORD, role=Role.MASTER,
                                   full_name=name.upper(), settings=store)
        user.must_change_password = False
        wallet = accounts.wallet_for_user(db, user)
        if credit:
            ledger.post(db, kind="request.in", entries=[(accounts.gcash_clearing(db), +credit), (wallet, -credit)],
                        actor=admin, memo="test load")
        loc = db.scalar(select(Location).where(Location.is_active.is_(True)))
        extra = {"location_id": loc.id} if loc else {}
        ev = events.create_event(db, admin, title="Chat test", event_date=dt.date.today(), settings=store,
                                 fight_count=2, **extra)
        events.set_event_status(db, admin, ev.id, EventStatus.OPEN)
        fights = list(db.scalars(select(Fight).where(Fight.event_id == ev.id).order_by(Fight.number)))
        return name, ev.id, [f.id for f in fights]


def _cleanup(event_id):
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        events.cancel_event(db, admin, event_id, reason="test cleanup", settings=SettingsStore(db))


def _balance(name):
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == name))
        return accounts.wallet_for_user(db, user).balance_cents


def test_store_page_shows_both_offers_and_the_default_prices():
    name, event_id, _ = _setup()
    try:
        c, _ = login(name)
        page = c.get("/store")
        assert page.status_code == 200
        assert "Battle Pass" in page.text and "Chat Token" in page.text
        assert "999.00" in page.text and "99.00" in page.text and "30 days" in page.text and "30 chats" in page.text and "2,999.00" in page.text and "Lux VIP" in page.text
    finally:
        _cleanup(event_id)


def test_battle_pass_charges_999_unlocks_chat_for_30_days_and_stacks():
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        h = {"X-CSRF-Token": _csrf(c)}
        before = _balance(name)
        r = c.post("/store/pass", headers=h, data={"idempotency_key": "pass-1"})
        assert r.status_code == 303
        assert _balance(name) == before - 99_900
        # same key twice must not charge twice
        c.post("/store/pass", headers=h, data={"idempotency_key": "pass-1"})
        assert _balance(name) == before - 99_900
        with session_scope() as db:
            user = db.scalar(select(User).where(User.username == name))
            row = db.scalar(select(ChatPass).where(ChatPass.user_id == user.id))
            assert (row.expires_at - row.starts_at).days == 30 and row.price_cents == 99_900
            first_end = row.expires_at
        # a renewal starts when the old pass ends
        c.post("/store/pass", headers=h, data={"idempotency_key": "pass-2"})
        with session_scope() as db:
            user = db.scalar(select(User).where(User.username == name))
            ends = sorted(p.expires_at for p in db.scalars(select(ChatPass).where(ChatPass.user_id == user.id)))
            assert (ends[1] - ends[0]).days == 30 and ends[0] == first_end
        # chat now works on any game without spending anything
        sent = c.post(f"/fights/{fight_id}/chat/send", headers={**h, **JSON}, json={"body": "hello fight"})
        assert sent.status_code == 200, sent.text
        poll = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()
        assert poll["can_send"] and poll["via"] == "pass"
        assert poll["messages"][-1]["body"] == "hello fight" and poll["messages"][-1]["pass"] is True
    finally:
        _cleanup(event_id)


def test_chat_is_locked_without_pass_or_token_but_still_readable():
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        h = {"X-CSRF-Token": _csrf(c), **JSON}
        state = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()
        assert state["can_send"] is False and state["reason"] == "locked"
        denied = c.post(f"/fights/{fight_id}/chat/send", headers=h, json={"body": "let me in"})
        assert denied.status_code == 422 and "locked" in denied.json()["detail"].lower()
        page = c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}")
        assert "data-fightchat" in page.text and "is-locked" in page.text and "999.00" in page.text
    finally:
        _cleanup(event_id)


def test_99_buys_thirty_chat_uses_one_game_each():
    name, event_id, (g1, g2) = _setup()
    try:
        c, _ = login(name)
        h = {"X-CSRF-Token": _csrf(c), **JSON}
        before = _balance(name)
        assert c.post("/store/token", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "tok-1"}).status_code == 303
        c.post("/store/token", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "tok-1"})   # replay: no double charge
        assert _balance(name) == before - 9_900
        with session_scope() as db:
            user = db.scalar(select(User).where(User.username == name))
            assert store_svc.token_count(db, user) == 30
            assert sum(t.price_cents for t in db.scalars(select(ChatToken).where(ChatToken.user_id == user.id))) == 9_900
        assert c.post(f"/fights/{g1}/chat/unlock", headers=h).json()["tokens"] == 29
        assert c.post(f"/fights/{g1}/chat/send", headers=h, json={"body": "first game"}).status_code == 200
        assert c.post(f"/fights/{g1}/chat/unlock", headers=h).json()["tokens"] == 29    # same game: not charged again
        assert c.post(f"/fights/{g2}/chat/unlock", headers=h).json()["tokens"] == 28    # second game uses the next one
    finally:
        _cleanup(event_id)


def test_no_tokens_means_no_unlock():
    name, event_id, (g1, _) = _setup()
    try:
        c, _ = login(name)
        r = c.post(f"/fights/{g1}/chat/unlock", headers={"X-CSRF-Token": _csrf(c), **JSON})
        assert r.status_code == 422
    finally:
        _cleanup(event_id)


def test_chat_closes_when_the_declarator_announces_the_winner():
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        h = {"X-CSRF-Token": _csrf(c), **JSON}
        c.post("/store/pass", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "pass-x"})
        assert c.post(f"/fights/{fight_id}/chat/send", headers=h, json={"body": "go meron"}).status_code == 200
        with session_scope() as db:
            admin = db.scalar(select(User).where(User.role == Role.ADMIN))
            settlement.declare(db, admin, fight_id=fight_id, result=Result.MERON, settings=SettingsStore(db))
        state = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()
        assert state["locked"] is True and state["can_send"] is False and state["reason"] == "closed"
        assert state["result"]["side"] == "meron" and state["messages"][-1]["body"] == "go meron"   # history stays
        late = c.post(f"/fights/{fight_id}/chat/send", headers=h, json={"body": "too late"})
        assert late.status_code == 422 and "closed" in late.json()["detail"].lower()
        page = c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}")
        assert "is-closed" in page.text and "Declarator announced" in page.text
    finally:
        _cleanup(event_id)


def test_cannot_buy_without_enough_credit_and_declarators_have_no_wallet():
    name, event_id, _ = _setup(credit=0)
    try:
        c, _ = login(name)
        before = _balance(name)
        r = c.post("/store/pass", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "poor-1"})
        assert r.status_code == 303 and _balance(name) == before == 0
        with session_scope() as db:
            user = db.scalar(select(User).where(User.username == name))
            assert store_svc.active_pass_until(db, user) is None
        with session_scope() as db:
            store = SettingsStore(db)
            admin = db.scalar(select(User).where(User.role == Role.ADMIN))
            dec = agents.create_agent(db, admin, username=f"dc{_tag()}", password=PASSWORD, role=Role.DECLARATOR,
                                      full_name="DEC", settings=store)
            try:
                store_svc.buy_token(db, dec, store)
                raise AssertionError("declarator bought a token")
            except Exception as exc:                      # Forbidden: no balance
                assert "no balance" in str(exc)
    finally:
        _cleanup(event_id)


def test_staff_room_does_not_show_fight_chat_messages():
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        c.post("/store/pass", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "pass-room"})
        c.post(f"/fights/{fight_id}/chat/send", headers={"X-CSRF-Token": _csrf(c), **JSON}, json={"body": "fight-only-msg"})
        a, _ = login("admin")
        room = a.get("/chat/messages?after=0", headers=JSON).json()
        assert all(m["body"] != "fight-only-msg" for m in room["messages"])
    finally:
        _cleanup(event_id)


def test_new_number_game_ui_assets_are_served():
    c, _ = login("admin")
    js = c.get("/static/js/bola.js").text
    assert "bola-ball" in js and "data-bola-lucky" in js
    assert ".bball" in c.get("/static/css/bola-ui.css").text
    assert "fc-lock" in c.get("/static/css/store.css").text


# ---- Store rename, video player, pass perks, stale-asset fix -------------------------------------

def test_store_sits_next_to_dashboard_and_is_called_store():
    c, _ = login("admin")
    html = c.get("/dashboard").text
    nav = html[html.index('class="nav"'):]
    assert "Chat Store" not in html
    assert nav.index('title="Dashboard"') < nav.index('title="Store"') < nav.index('title="Events"')
    store_page = c.get("/store").text
    assert "<h1>Store</h1>" in store_page and "Chat Store" not in store_page


def test_fight_view_has_a_playable_dummy_player_and_pass_only_skip_buttons():
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        page = c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}").text
        assert "data-player" in page and 'data-pass="0"' in page
        for hook in ("data-vp-play", "data-vp-prev", "data-vp-next"):
            assert hook in page
        assert "Battle Pass" in page and "stream.js" in page
        c.post("/store/pass", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "pass-vp"})
        page = c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}").text
        assert 'data-pass="1"' in page and "Previous and forward come with" not in page
    finally:
        _cleanup(event_id)


def test_battle_pass_chats_on_every_stream_including_esports():
    name, event_id, _ = _setup()
    try:
        with session_scope() as db:
            user = db.scalar(select(User).where(User.username == name))
            assert store_svc.stream_access(db, user, "esports")["can_send"] is False
            assert store_svc.has_stream_perks(db, user) is False
            store_svc.buy_pass(db, user, SettingsStore(db), idempotency_key="pass-es")
            assert store_svc.stream_access(db, user, "esports") == {"can_send": True, "reason": "ok", "via": "pass", "scope": "esports"}
            assert store_svc.has_stream_perks(db, user) is True
        assert any("Esports" in p for p in store_svc.PASS_PERKS)
    finally:
        _cleanup(event_id)


def test_scripts_are_versioned_and_service_worker_is_network_first_for_code():
    c, _ = login("admin")
    sw = c.get("/static/sw.js").text
    assert "royale8" in sw and "fetch(request).then" in sw
    lobby = c.get("/bola").text
    assert "bola.js?v=2" in lobby and "bola-ui.css?v=2" in lobby


def _extra_event(title):
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        store = SettingsStore(db)
        loc = db.scalar(select(Location).where(Location.is_active.is_(True)))
        extra = {"location_id": loc.id} if loc else {}
        ev = events.create_event(db, admin, title=title, event_date=dt.date.today(), settings=store, fight_count=1, **extra)
        events.set_event_status(db, admin, ev.id, EventStatus.OPEN)
        fid = db.scalar(select(Fight.id).where(Fight.event_id == ev.id))
        return ev.id, fid


def test_one_token_pack_only_works_on_three_events():
    name, event_id, (g1, _) = _setup()
    extra = [_extra_event(f"Tok ev {i}") for i in range(3)]
    try:
        c, _ = login(name)
        h = {"X-CSRF-Token": _csrf(c), **JSON}
        c.post("/store/token", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "tok-ev"})
        assert c.post(f"/fights/{g1}/chat/unlock", headers=h).status_code == 200            # event 1
        assert c.post(f"/fights/{extra[0][1]}/chat/unlock", headers=h).status_code == 200   # event 2
        assert c.post(f"/fights/{extra[1][1]}/chat/unlock", headers=h).status_code == 200   # event 3
        r = c.post(f"/fights/{extra[2][1]}/chat/unlock", headers=h)                         # event 4: refused
        assert r.status_code == 422 and "3 events" in r.json()["detail"]
        with session_scope() as db:
            user = db.scalar(select(User).where(User.username == name))
            assert store_svc.token_count(db, user) == 27                                    # still held, just not for event 4
    finally:
        _cleanup(event_id)
        for ev_id, _f in extra:
            _cleanup(ev_id)


def test_lux_vip_costs_2999_has_pass_perks_flashy_chat_and_a_thank_you():
    name, event_id, (fight_id, _) = _setup(credit=5_000_00)
    try:
        c, _ = login(name)
        before = _balance(name)
        r = c.post("/store/lux", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "lux-1"})
        assert r.status_code == 303
        c.post("/store/lux", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "lux-1"})   # replay
        assert _balance(name) == before - 299_900
        page = c.get("/store").text
        assert "humbly thank you" in page and "Extend Lux VIP" in page
        with session_scope() as db:
            user = db.scalar(select(User).where(User.username == name))
            assert store_svc.active_lux_until(db, user) is not None
            assert store_svc.has_stream_perks(db, user) and store_svc.stream_access(db, user)["can_send"]
        h = {"X-CSRF-Token": _csrf(c), **JSON}
        assert c.post(f"/fights/{fight_id}/chat/send", headers=h, json={"body": "flex"}).status_code == 200
        msgs = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()["messages"]
        assert msgs[-1]["lux"] is True and msgs[-1]["pass"] is True
        assert 'data-pass="1"' in c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}").text
    finally:
        _cleanup(event_id)


def test_nav_has_admin_panel_next_to_dashboard_and_no_livestream_chat_link():
    c, _ = login("admin")
    html = c.get("/dashboard").text
    nav = html[html.index('class="nav"'):html.index("</nav>")]
    assert nav.index('title="Dashboard"') < nav.index('title="Store"') < nav.index('title="Admin Panel"') < nav.index('title="Events"')
    assert 'title="Livestream Chat"' not in nav and "nav-exp" not in nav
    assert 'title="Commission"' not in nav and 'title="Referral Codes"' not in nav


def test_commission_in_wallet_menu_and_referrals_in_account_menu_only_for_those_who_have_them():
    name, event_id, _ = _setup()               # a Master: has both
    try:
        c, _ = login(name)
        html = c.get("/dashboard").text
        wallet = html[html.index("wallet-panel"):html.index("account-menu")]
        account = html[html.index("account-panel"):]
        assert 'href="/commissions"' in wallet and 'href="/referrals"' in account
        assert 'href="/commissions"' not in html[:html.index("wallet-panel")]
    finally:
        _cleanup(event_id)
    # a player holds neither permission
    from app.core.rbac import ROLE_PERMS, Perm
    for role in ROLE_PERMS:
        if str(role).lower().endswith("player"):
            assert Perm.COMMISSIONS_VIEW not in ROLE_PERMS[role] and Perm.REFERRALS_MANAGE not in ROLE_PERMS[role]


def test_header_menus_close_each_other():
    c, _ = login("admin")
    js = c.get("/static/js/menus.js").text
    assert "closeAll" in js and "menus.js" in c.get("/dashboard").text
