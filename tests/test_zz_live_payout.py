"""The payout on the livestream page: it must move with the bet's own reply, and the shared poll payload must
never serve a pool older than the bet that was just placed. Named test_zz_ so it runs last (see test_zz_live_sell)."""
from app.core import ttlcache
from app.core.config import get_settings
from app.web import routes_live
from tests.test_web import login
from tests.test_zz_live_sell import _cleanup, _csrf, _setup

JSON = {"Accept": "application/json"}


def _place(c, fight_id, amount, key, side="meron"):
    t = _csrf(c)
    return c.post("/bets/live/place", data={"fight_id": fight_id, "side": side, "amount": amount,
                                            "idempotency_key": key, "csrf_token": t},
                  headers={**JSON, "X-CSRF-Token": t})


def _poll_row(c, event_id, fight_id):
    rows = {f["id"]: f for f in c.get(f"/_live/event/{event_id}", headers=JSON).json()["fights"]}
    return rows[fight_id]


def test_bet_and_undo_replies_carry_the_games_fresh_payout_row():
    event_id, fight_id, _ = _setup()
    try:
        c, _ = login("admin")
        placed = _place(c, fight_id, "100", "lp-1")
        assert placed.status_code == 200
        row = placed.json()["row"]
        assert row["id"] == fight_id and row["pm"] == 100_00 and row["cm"] == 1   # this very bet is in the pool
        assert row["tt"] == 100_00 and "mm" in row and "mw" in row                # live multiples ride along

        t = _csrf(c)
        undone = c.post(f"/bets/live/undo/{placed.json()['ticket']['id']}", data={"csrf_token": t},
                        headers={**JSON, "X-CSRF-Token": t})
        assert undone.status_code == 200
        assert undone.json()["row"]["pm"] == 0 and undone.json()["row"]["cm"] == 0   # and the undo takes it out again
    finally:
        _cleanup(event_id)


def test_poll_right_after_a_bet_is_never_stale_even_with_the_cache_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "LIVE_CACHE_SECONDS", 30)     # long TTL: only invalidation can keep it fresh
    ttlcache.clear("live")
    event_id, fight_id, _ = _setup()
    try:
        c, _ = login("admin")
        assert _poll_row(c, event_id, fight_id).get("pm", 0) == 0     # builds and caches the payload
        assert _place(c, fight_id, "100", "lp-2").status_code == 200
        assert _poll_row(c, event_id, fight_id)["pm"] == 100_00       # the commit cleared it: next poll is current
    finally:
        _cleanup(event_id)
        ttlcache.clear("live")


def test_viewers_share_one_build_and_a_change_rebuilds(monkeypatch):
    monkeypatch.setattr(get_settings(), "LIVE_CACHE_SECONDS", 30)
    ttlcache.clear("live")
    calls = []
    real = routes_live._live_event_pack
    monkeypatch.setattr(routes_live, "_live_event_pack", lambda *a, **k: calls.append(1) or real(*a, **k))
    event_id, fight_id, _ = _setup()
    try:
        c, _ = login("admin")
        for _ in range(3):
            c.get(f"/_live/event/{event_id}", headers=JSON)
        assert len(calls) == 1                                        # three polls, one build
        assert _place(c, fight_id, "100", "lp-3").status_code == 200
        c.get(f"/_live/event/{event_id}", headers=JSON)
        assert len(calls) == 2                                        # a bet invalidates, so exactly one rebuild
    finally:
        _cleanup(event_id)
        ttlcache.clear("live")


def test_a_value_computed_across_a_clear_is_not_stored():
    """A poll that started before a commit must not re-cache the pre-commit numbers after the commit cleared them."""
    ttlcache.clear("race")
    runs = []

    def compute():
        runs.append(1)
        ttlcache.clear("race")            # the commit lands while this build is still running
        return {"v": len(runs)}

    assert ttlcache.cached(("race", 1), 30, compute) == {"v": 1}
    assert ttlcache.cached(("race", 1), 30, compute) == {"v": 2}      # not served from the stale first build
    ttlcache.clear("race")


# ---- undoing a bet is dev-only ---------------------------------------------------------------------------------
import datetime as dt

import pytest
from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import Role, Ticket, TicketStatus, User
from app.db.session import session_scope
from app.services import accounts, agents, ledger
from tests.conftest import PASSWORD


def _funded_user(role):
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        parent = None
        if role in (Role.AGENT, Role.SUB_AGENT):
            master = agents.create_agent(db, admin, username=f"m{tag}", password=PASSWORD, role=Role.MASTER,
                                         full_name="M", settings=store)
            parent = master
            if role == Role.SUB_AGENT:
                parent = agents.create_agent(db, master, username=f"a{tag}", password=PASSWORD, role=Role.AGENT,
                                             full_name="A", settings=store, parent_id=master.id)
        u = agents.create_agent(db, admin, username=f"u{tag}", password=PASSWORD, role=role, full_name="U",
                                settings=store, parent_id=parent.id if parent else None)
        ledger.post(db, kind="request.in", entries=[(accounts.gcash_clearing(db), +50_000_00),
                                                    (accounts.wallet_for_user(db, u), -50_000_00)],
                    actor=admin, memo="test load")
        return u.username, tag


@pytest.mark.parametrize("role", [Role.MASTER, Role.AGENT, Role.SUB_AGENT])
def test_only_the_admin_can_undo_a_bet(role):
    event_id, fight_id, _ = _setup()
    try:
        username, tag = _funded_user(role)
        c, r = login(username)
        assert r.status_code == 303
        placed = _place(c, fight_id, "100", f"undo-{tag}")
        assert placed.status_code == 200
        ticket_id = placed.json()["ticket"]["id"]

        # the page does not even offer Undo (data-undo="-1") ...
        page = c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}").text
        assert 'data-undo="-1"' in page
        # ... and the endpoints refuse it, so the bet stays
        t = _csrf(c)
        assert c.post(f"/bets/live/undo/{ticket_id}", data={"csrf_token": t},
                      headers={**JSON, "X-CSRF-Token": t}).status_code == 403
        t = _csrf(c)
        c.post(f"/bets/{ticket_id}/cancel", data={"csrf_token": t, "reason": "changed my mind"},
               headers={"X-CSRF-Token": t})                      # the form route answers a refusal with a redirect + message
        with session_scope() as db:
            assert db.get(Ticket, ticket_id).status == TicketStatus.ACTIVE
    finally:
        _cleanup(event_id)


# ---- audit: nobody but the admin can cancel a placed bet, by any route ------------------------------------------
from app.core import rbac
from app.core.errors import Forbidden
from app.services import betting


def test_no_role_except_admin_holds_the_bet_cancel_permission():
    for role in Role:
        has = rbac.Perm.BETS_CANCEL in rbac.permissions_for(role)
        assert has == (role == Role.ADMIN), f"{role} {'has' if has else 'lacks'} bets.cancel"
    assert rbac.Perm.BETS_CANCEL not in rbac.player_perms()      # online players
    assert rbac.Perm.BETS_CANCEL not in rbac.PLAYER_PERMS


def test_the_service_itself_refuses_a_non_admin_even_if_a_route_forgot_to_check():
    event_id, fight_id, _ = _setup()
    try:
        username, tag = _funded_user(Role.MASTER)
        c, _ = login(username)
        ticket_id = _place(c, fight_id, "100", f"svc-{tag}").json()["ticket"]["id"]
        with session_scope() as db:
            master = db.scalar(select(User).where(User.username == username))
            with pytest.raises(Forbidden):
                betting.cancel_ticket(db, master, ticket_id=ticket_id, reason="calling the service directly",
                                      settings=SettingsStore(db))
            assert db.get(Ticket, ticket_id).status == TicketStatus.ACTIVE
    finally:
        _cleanup(event_id)


@pytest.mark.parametrize("role", [Role.AGENT, Role.SUB_AGENT, Role.TELLER, Role.DECLARATOR, Role.PAWN])
def test_no_staff_role_can_cancel_or_remove_someone_elses_bet(role):
    """Players have no console login at all; the accounts that do are staff. A bet placed by a Sub Agent stays put
    when any other non-admin account tries every cancel / void / purge route."""
    event_id, fight_id, _ = _setup()
    try:
        owner, tag = _funded_user(Role.SUB_AGENT)
        oc, _ = login(owner)
        tid = _place(oc, fight_id, "100", f"own-{tag}").json()["ticket"]["id"]

        if role in (Role.AGENT, Role.SUB_AGENT):
            name, _ = _funded_user(role)                 # a different account from the bet's owner
        else:
            with session_scope() as db:
                store = SettingsStore(db)
                admin = db.scalar(select(User).where(User.role == Role.ADMIN))
                name = f"x{tag}"
                kw = {"gcash_number": "09171234567"} if role == Role.TELLER else {}
                agents.create_agent(db, admin, username=name, password=PASSWORD, role=role, full_name="X",
                                    settings=store, **kw)
        c, r = login(name)
        assert r.status_code == 303
        t = _csrf(c)
        h = {**JSON, "X-CSRF-Token": t}
        for a in (
            c.post(f"/bets/live/undo/{tid}", data={"csrf_token": t}, headers=h),
            c.post(f"/bets/{tid}/cancel", data={"csrf_token": t, "reason": "trying to cancel it"}, headers=h),
            c.post(f"/events/{event_id}/cancel", data={"csrf_token": t, "reason": "trying to cancel it"}, headers=h),
            c.post(f"/dev/purge/fight/{fight_id}", data={"csrf_token": t}, headers=h),
            c.post(f"/dev/purge/event/{event_id}", data={"csrf_token": t}, headers=h),
        ):
            assert a.status_code in (302, 303, 401, 403, 404, 405), a.status_code   # refused (never a 200 / success)
        with session_scope() as db:
            from app.db.models import Fight
            assert db.get(Ticket, tid).status == TicketStatus.ACTIVE      # the bet is still standing
            assert db.get(Fight, fight_id) is not None                     # and nothing was purged
    finally:
        _cleanup(event_id)


def test_players_see_pools_after_the_platform_cut_and_only_admin_sees_gross_and_the_cut_amount():
    """The platform cut is disclosed as a rate; the pools / total / house cut / payout pool on screen are the worked
    calculation AFTER it; the gross stakes and the platform amount ride only on the Admin's (granted Pawn's) poll."""
    from app.db.models import User
    from app.db.session import session_scope
    event_id, fight_id, player_id = _setup()
    try:
        with session_scope() as db:
            from app.core.settings_registry import SettingsStore
            admin = db.scalar(__import__("sqlalchemy").select(User).where(User.username == "admin"))
            SettingsStore(db).set("payout.platform_cut_bps", "500", admin.id)
            SettingsStore(db).set("payout.house_cut_bps", "500", admin.id)
            name = db.get(User, player_id).username
        ttl = {}
        c, _ = login("admin")
        assert _place(c, fight_id, "100", "hc-1").status_code == 200
        arow = _poll_row(c, event_id, fight_id)
        assert arow["gt"] == 100_00 and arow["pc"] == 5_00 and arow["tt"] == 95_00 and arow["pm"] == 95_00
        assert arow["cut"] == 4_75 and arow["np"] == 90_25
        p, _ = login(name)
        row = _poll_row(p, event_id, fight_id)
        assert row["pm"] == 95_00 and row["tt"] == 95_00 and row["cut"] == 4_75 and row["np"] == 90_25
        assert row["gt"] is None and row["pc"] is None
        ev = p.get(f"/_live/event/{event_id}", headers=JSON).json()["event"]
        assert ev["cutbps"] == 500 and ev["pcbps"] == 500           # the RATES are on screen for everyone
    finally:
        with session_scope() as db:
            from app.core.settings_registry import SettingsStore
            SettingsStore(db).set("payout.platform_cut_bps", "0", 1)
        _cleanup(event_id)


def test_fight_page_gross_and_platform_rows_are_for_the_admin_only():
    event_id, fight_id, player_id = _setup()
    try:
        from app.db.models import User
        from app.db.session import session_scope
        with session_scope() as db:
            name = db.get(User, player_id).username
        with session_scope() as db:
            from app.core.settings_registry import SettingsStore
            SettingsStore(db).set("payout.platform_cut_bps", "500", 1)
        c, _ = login("admin")
        page = c.get(f"/change_name?event_id={event_id}&fight_id={fight_id}").text
        assert 'data-pbn="cut"' in page and "Payout pool" in page and 'data-pbn="gt"' in page
        p, _ = login(name)
        page = p.get(f"/change_name?event_id={event_id}&fight_id={fight_id}").text
        assert 'data-pbn="cut"' in page and "Payout pool" in page      # the house cut calculation is public
        assert 'data-pbn="gt"' not in page and 'data-pbn="pc"' not in page
    finally:
        with session_scope() as db:
            from app.core.settings_registry import SettingsStore
            SettingsStore(db).set("payout.platform_cut_bps", "0", 1)
        _cleanup(event_id)
