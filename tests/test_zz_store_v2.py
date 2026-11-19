"""Store v2: greyed-out inferior options (enforced server side), slow mode with a Lux exemption,
the one-time Alias Token, the consumables section, and the DEV revoke tool.
Named test_zz_ so it runs after the tests that count open games."""
import pytest
from sqlalchemy import select

from app.core.assets import asset_urls
from app.core.errors import Conflict, ValidationError
from app.core.settings_registry import SettingsStore
from app.db.models import AliasToken, ChatAlias, ChatPass, Role, User
from app.db.session import session_scope
from app.services import store as store_svc
from tests.test_web import login
from tests.test_zz_chat_store import JSON, _balance, _cleanup, _csrf, _setup


def _h(c):
    return {"X-CSRF-Token": _csrf(c)}


def _user(db, name):
    return db.scalar(select(User).where(User.username == name))


def _send(c, fight_id, text):
    return c.post(f"/fights/{fight_id}/chat/send", headers={**_h(c), **JSON}, json={"body": text})


# ---- inferior options are refused (and greyed out) ----------------------------------------------

def test_lux_holder_cannot_buy_battle_pass_or_tokens():
    name, event_id, _ = _setup(credit=9_000_00)
    try:
        c, _ = login(name)
        assert c.post("/store/lux", headers=_h(c), data={"idempotency_key": "l1"}).status_code == 303
        before = _balance(name)
        assert c.post("/store/pass", headers=_h(c), data={"idempotency_key": "p1"}).status_code == 303
        assert c.post("/store/token", headers=_h(c), data={"idempotency_key": "t1"}).status_code == 303
        assert _balance(name) == before                                  # nothing was charged
        with session_scope() as db:
            u = _user(db, name)
            assert store_svc.token_count(db, u) == 0
            assert db.scalar(select(ChatPass).where(ChatPass.user_id == u.id, ChatPass.tier == "battle")) is None
            with pytest.raises(Conflict):
                store_svc.buy_pass(db, u, SettingsStore(db))
        page = c.get("/store").text
        assert page.count("is-blocked") >= 2                              # Battle Pass card + Chat Tokens tile
        assert "Included in your Lux VIP" in page and "Not available" in page
    finally:
        _cleanup(event_id)


def test_battle_pass_holder_cannot_buy_tokens_but_can_upgrade_to_lux():
    name, event_id, _ = _setup(credit=9_000_00)
    try:
        c, _ = login(name)
        c.post("/store/pass", headers=_h(c), data={"idempotency_key": "p1"})
        before = _balance(name)
        assert c.post("/store/token", headers=_h(c), data={"idempotency_key": "t1"}).status_code == 303
        assert _balance(name) == before
        page = c.get("/store").text
        assert "Your Battle Pass already lets you chat" in page
        c.post("/store/lux", headers=_h(c), data={"idempotency_key": "l1"})
        assert _balance(name) == before - 299_900                         # the upgrade is allowed
    finally:
        _cleanup(event_id)


def test_battle_pass_card_shows_only_its_own_days_not_the_lux_days():
    name, event_id, _ = _setup(credit=9_000_00)
    try:
        with session_scope() as db:
            u = _user(db, name)
            store_svc.buy_lux(db, u, SettingsStore(db), idempotency_key="x")
            st = store_svc.state(db, u, SettingsStore(db))
            assert st.has_lux and st.has_pass and not st.has_battle and st.battle_expires is None
    finally:
        _cleanup(event_id)


# ---- slow mode ------------------------------------------------------------------------------------

def test_slow_mode_throttles_battle_pass_but_never_lux_vip():
    pass_user, ev1, (g1, _) = _setup(credit=9_000_00)
    lux_user, ev2, (g2, _) = _setup(credit=9_000_00)
    try:
        cp, _ = login(pass_user)
        cp.post("/store/pass", headers=_h(cp), data={"idempotency_key": "p"})
        assert _send(cp, g1, "one").status_code == 200
        r = _send(cp, g1, "two")
        assert r.status_code == 422 and "Slow mode" in r.json()["detail"]
        st = cp.get(f"/fights/{g1}/chat", headers=JSON).json()
        assert st["slow"] == 3 and 1 <= st["slow_left"] <= 3

        cl, _ = login(lux_user)
        cl.post("/store/lux", headers=_h(cl), data={"idempotency_key": "l"})
        for i in range(4):                                                # back to back: never slowed
            assert _send(cl, g2, f"flex {i}").status_code == 200
        st = cl.get(f"/fights/{g2}/chat", headers=JSON).json()
        assert st["slow"] == 0 and st["slow_left"] == 0 and st["slow_global"] == 3
    finally:
        _cleanup(ev1)
        _cleanup(ev2)


def test_slow_mode_seconds_follow_the_setting_and_staff_are_exempt():
    name, event_id, _ = _setup()
    try:
        with session_scope() as db:
            store = SettingsStore(db)
            u = _user(db, name)
            assert store_svc.slow_mode_seconds(db, u, store) == 3
            assert store_svc.slow_mode_seconds(db, _user(db, "admin"), store) == 0
    finally:
        _cleanup(event_id)


# ---- alias consumable ----------------------------------------------------------------------------

def test_alias_token_costs_200_is_consumed_once_and_replaces_the_name_in_chat():
    name, event_id, (fight_id, _) = _setup(credit=9_000_00)
    try:
        c, _ = login(name)
        c.post("/store/pass", headers=_h(c), data={"idempotency_key": "p"})
        before = _balance(name)
        c.post("/store/alias-token", headers=_h(c), data={"idempotency_key": "a1"})
        c.post("/store/alias-token", headers=_h(c), data={"idempotency_key": "a1"})   # replay: no double charge
        assert _balance(name) == before - 20_000
        assert "One-time use" in c.get("/store").text

        assert c.post("/store/alias", headers=_h(c), data={"alias": "Tigre Loco"}).status_code == 303
        with session_scope() as db:
            u = _user(db, name)
            assert store_svc.alias_of(db, u.id) == "Tigre Loco" and store_svc.alias_token_count(db, u) == 0
        # the token is gone: a second change is refused and nothing changes
        c.post("/store/alias", headers=_h(c), data={"alias": "Other Name"})
        with session_scope() as db:
            assert store_svc.alias_of(db, _user(db, name).id) == "Tigre Loco"

        assert _send(c, fight_id, "hola").status_code == 200
        mine = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()["messages"][-1]
        assert mine["user"] == "Tigre Loco" and mine["real"] == ""        # players never see the real account
        admin_c, _ = login("admin")
        seen = admin_c.get(f"/fights/{fight_id}/chat", headers=JSON).json()["messages"][-1]
        assert seen["user"] == "Tigre Loco" and seen["real"] == name      # staff do
    finally:
        _cleanup(event_id)


def test_alias_rules_refuse_staff_lookalikes_taken_names_and_spend_nothing():
    a, ev1, _ = _setup(credit=9_000_00)
    b, ev2, _ = _setup(credit=9_000_00)
    try:
        with session_scope() as db:
            ua, ub = _user(db, a), _user(db, b)
            store = SettingsStore(db)
            store_svc.buy_alias_token(db, ua, store, idempotency_key="k1")
            store_svc.buy_alias_token(db, ub, store, idempotency_key="k2")
            for bad in ("ad", "Admin", "Mr. Ad min", "S.y.s.t.e.m", "bad<script>", "x" * 25, " ", b.upper(), b):
                with pytest.raises(ValidationError):
                    store_svc.set_alias(db, ua, store, bad)
            assert store_svc.alias_token_count(db, ua) == 1               # refusals cost nothing
            store_svc.set_alias(db, ub, store, "El Gallo")
            with pytest.raises(ValidationError):
                store_svc.set_alias(db, ua, store, "el  gallo")           # alias already taken (case/space blind)
            assert store_svc.alias_token_count(db, ua) == 1
            store_svc.set_alias(db, ua, store, "Gallo Fino")
            assert store_svc.alias_token_count(db, ua) == 0
    finally:
        _cleanup(ev1)
        _cleanup(ev2)


def test_store_shows_consumables_as_tiles_with_rules_always_visible():
    name, event_id, _ = _setup()
    try:
        c, _ = login(name)
        page = c.get("/store").text
        assert "st-mats" in page and "Consumables" in page
        assert page.index("Battle Pass") < page.index("Consumables") < page.index("Chat Tokens")
        for rule in ("one game", "events", "One-time use", "Changing it later uses another token"):
            assert rule in page
        assert "Alias Token" in page and "/store/alias-token" in page
    finally:
        _cleanup(event_id)


# ---- DEV revoke -----------------------------------------------------------------------------------

def test_dev_can_revoke_lux_with_and_without_refund_and_players_cannot():
    name, event_id, (fight_id, _) = _setup(credit=9_000_00)
    try:
        c, _ = login(name)
        c.post("/store/lux", headers=_h(c), data={"idempotency_key": "l"})
        assert c.get("/dev/store").status_code == 403                      # not for players
        assert c.post("/dev/store/revoke", headers=_h(c), data={"user_id": "1", "what": "lux", "reason": "x"}).status_code in (303, 403)

        admin_c, _ = login("admin")
        with session_scope() as db:
            uid = _user(db, name).id
        page = admin_c.get(f"/dev/store?q={name}").text
        assert "Lux VIP" in page and "Revoke" in page
        paid = _balance(name)
        r = admin_c.post("/dev/store/revoke", headers=_h(admin_c),
                         data={"user_id": uid, "what": "lux", "reason": "bought by mistake", "refund": "1"})
        assert r.status_code == 303
        assert _balance(name) == paid + 299_900                            # refunded through the ledger
        with session_scope() as db:
            assert store_svc.active_lux_until(db, _user(db, name)) is None
        assert c.get(f"/fights/{fight_id}/chat", headers=JSON).json()["can_send"] is False
    finally:
        _cleanup(event_id)


def test_dev_revoke_needs_a_reason_and_can_remove_alias_and_tokens_without_refund():
    name, event_id, _ = _setup(credit=9_000_00)
    try:
        with session_scope() as db:
            u = _user(db, name)
            store = SettingsStore(db)
            store_svc.buy_alias_token(db, u, store, idempotency_key="k")
            store_svc.set_alias(db, u, store, "Kuya Bong")
            store_svc.buy_alias_token(db, u, store, idempotency_key="k2")
            store_svc.buy_token(db, u, store, idempotency_key="t")
            admin = _user(db, "admin")
            with pytest.raises(ValidationError):
                store_svc.revoke_effect(db, admin, u, "alias", reason=" ")
        paid = _balance(name)                                             # read after the purchases committed
        assert paid == 9_000_00 - 20_000 - 20_000 - 9_900
        with session_scope() as db:
            u, admin = _user(db, name), _user(db, "admin")
            store_svc.revoke_effect(db, admin, u, "alias", reason="offensive")
            store_svc.revoke_effect(db, admin, u, "alias_tokens", reason="test")
            store_svc.revoke_effect(db, admin, u, "tokens", reason="test")
            assert store_svc.alias_of(db, u.id) is None
            assert store_svc.alias_token_count(db, u) == 0 and store_svc.token_count(db, u) == 0
        assert _balance(name) == paid                                     # no refund unless asked
    finally:
        _cleanup(event_id)


def test_chat_assets_carry_the_new_dense_layout_and_size_controls():
    c, _ = login("admin")
    css = c.get("/static/css/store.css").text
    assert ".fc-msg .fc-name" in css and ".st-mats" in css and ".is-blocked" in css and 'data-size="l"' in css
    js = c.get("/static/js/fightchat.js").text
    assert "fc-colon" in js and "slow_left" in js and "data-fc-size" in js


# ---- account types hidden by default, alias reveal limited to Admin / Pawn --------------------------

def test_account_type_tags_are_hidden_by_default_and_toggle_from_settings():
    name, event_id, (fight_id, _) = _setup(credit=9_000_00)
    try:
        c, _ = login(name)
        c.post("/store/pass", headers=_h(c), data={"idempotency_key": "p"})
        _send(c, fight_id, "hi")
        msg = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()["messages"][-1]
        assert msg["role"] == "" and msg["tone"] == "slate"               # default: no tag, no role colour
        with session_scope() as db:
            SettingsStore(db).set("store.chat_show_roles", "true", _user(db, "admin").id)
        try:
            msg = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()["messages"][-1]
            assert msg["role"] == "Master Agent" and msg["tone"] == "purple"
        finally:
            with session_scope() as db:
                SettingsStore(db).set("store.chat_show_roles", "false", _user(db, "admin").id)
    finally:
        _cleanup(event_id)


def test_only_admin_and_pawn_may_see_through_an_alias():
    name, event_id, (fight_id, _) = _setup(credit=9_000_00)
    try:
        with session_scope() as db:
            u = _user(db, name)
            store = SettingsStore(db)
            store_svc.buy_alias_token(db, u, store, idempotency_key="k")
            store_svc.set_alias(db, u, store, "Mystery Man")
            assert store_svc.can_see_through_alias(_user(db, "admin"))
            assert not store_svc.can_see_through_alias(u)                 # a Master Agent cannot
            assert store_svc.ALIAS_REVEAL_ROLES == (Role.ADMIN, Role.PAWN)
        c, _ = login(name)
        c.post("/store/pass", headers=_h(c), data={"idempotency_key": "p"})
        _send(c, fight_id, "psst")
        m = c.get(f"/fights/{fight_id}/chat", headers=JSON).json()["messages"][-1]
        assert m["user"] == "Mystery Man" and m["real"] == "" and name not in str(m)
    finally:
        _cleanup(event_id)


def test_layout_uses_container_queries_lux_name_flashes_and_page_loads_fresh_css():
    c, _ = login("admin")
    css = c.get("/static/css/store.css").text
    assert "@container fcstage (max-width:560px)" in css and "lux-name" in css
    name, event_id, (fight_id, _) = _setup()
    try:
        cu, _ = login(name)
        page = cu.get(f"/change_name?event_id={event_id}&fight_id={fight_id}").text
        assert asset_urls("css", "store")[0] in page      # hashed (built) or mtime-versioned (unbuilt): never a stale hand-set ?v=
    finally:
        _cleanup(event_id)
