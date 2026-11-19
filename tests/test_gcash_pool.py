"""GCash pool: limit tracker, smart rotation, teller scoping and the Staff Panel gates."""
import datetime as dt

import pytest

from app.core.errors import Conflict, Forbidden, ValidationError
from app.db.models import CashRequest, RequestDirection, RequestStatus, Role
from app.services import agents, cashdesk, gcash
from tests.conftest import PASSWORD
from tests.test_web import login

PESO = 100


def _user(db, admin, store, role, name, **kw):
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-8:]
    u = agents.create_agent(db, admin, username=f"{name}{tag}", password=PASSWORD, role=role,
                            full_name=name, settings=store, **kw)
    u.must_change_password = False
    db.flush()
    return u


def _teller(db, admin, store, number="09170000001", name="tl"):
    return _user(db, admin, store, Role.TELLER, name, gcash_number=number)


def _load(world, pesos, ref="GC-POOL"):
    return cashdesk.create_request(world.db, world.admin, direction=RequestDirection.IN,
                                   amount_cents=pesos * PESO, settings=world.store,
                                   player_id=world.player.id, method="gcash", reference=ref)


def _small_pool(world, *numbers, daily=1000, monthly=100_000):
    """Teller with several numbers, each limited to `daily` pesos a day."""
    teller = _teller(world.db, world.admin, world.store, numbers[0])
    gcash.list_accounts(world.db, owner_id=teller.id)[0].daily_limit_cents = daily * PESO
    gcash.list_accounts(world.db, owner_id=teller.id)[0].monthly_limit_cents = monthly * PESO
    for i, n in enumerate(numbers[1:], start=2):
        gcash.add_account(world.db, teller, number=n, label=f"N{i}", daily_limit_cents=daily * PESO,
                          monthly_limit_cents=monthly * PESO)
    world.db.flush()
    return teller


# ---------------------------------------------------------------- defaults & numbers

def test_defaults_are_100k_a_day_and_1m_a_month(world):
    assert int(world.store.get("gcash.daily_limit_cents")) == 100_000 * PESO
    assert int(world.store.get("gcash.monthly_limit_cents")) == 1_000_000 * PESO


def test_number_formats_are_normalised_and_duplicates_refused(world):
    teller = _teller(world.db, world.admin, world.store, "09170000001")
    acct = gcash.add_account(world.db, teller, number="+63 917-000-0002", label="Second")
    assert gcash.number_of(acct) == "09170000002"
    with pytest.raises(Conflict):
        gcash.add_account(world.db, teller, number="0917 000 0002", label="Dupe")
    with pytest.raises(ValidationError):
        gcash.add_account(world.db, teller, number="12345", label="Bad")


# ---------------------------------------------------------------- rotation

def test_fill_strategy_cycles_to_the_next_number_before_the_limit(world):
    teller = _small_pool(world, "09170000011", "09170000012", daily=1000)   # 900 = the 90% mark
    first, second = gcash.list_accounts(world.db, owner_id=teller.id)
    assert _load(world, 500).gcash_account_id == first.id
    assert _load(world, 300).gcash_account_id == first.id          # 800: still under 90%
    assert _load(world, 200).gcash_account_id == second.id         # would reach 1000 -> next number
    assert _load(world, 100).gcash_account_id == first.id          # 900 is exactly 90%: still ok


def test_a_number_that_cannot_fit_is_skipped_and_none_left_is_refused(world):
    _small_pool(world, "09170000021", "09170000022", daily=1000)
    _load(world, 1000)
    _load(world, 1000)
    with pytest.raises(ValidationError, match="No GCash number"):
        _load(world, 50)


def test_when_every_number_is_past_the_threshold_the_one_with_most_room_still_serves(world):
    teller = _small_pool(world, "09170000031", "09170000032", daily=1000)
    a, b = gcash.list_accounts(world.db, owner_id=teller.id)
    _load(world, 950)                      # a at 95%
    _load(world, 600)                      # goes to b (600 <= 90%)
    r = _load(world, 380)                  # a would be 1330 (no); b 980 (past 90%, but fits)
    assert r.gcash_account_id == b.id


def test_balance_strategy_uses_the_number_with_most_room(world):
    teller = _small_pool(world, "09170000041", "09170000042", daily=1000)
    world.store.set("gcash.strategy", "balance", world.admin.id)
    a, b = gcash.list_accounts(world.db, owner_id=teller.id)
    assert _load(world, 400).gcash_account_id == a.id
    assert _load(world, 100).gcash_account_id == b.id              # b has more room now


def test_monthly_limit_also_forces_rotation(world):
    teller = _small_pool(world, "09170000051", "09170000052", daily=10_000, monthly=1000)
    a, b = gcash.list_accounts(world.db, owner_id=teller.id)
    assert _load(world, 800).gcash_account_id == a.id
    assert _load(world, 300).gcash_account_id == b.id


def test_paused_numbers_are_skipped(world):
    teller = _small_pool(world, "09170000061", "09170000062", daily=1000)
    a, b = gcash.list_accounts(world.db, owner_id=teller.id)
    gcash.set_active(world.db, teller, a.id, False)
    assert _load(world, 100).gcash_account_id == b.id


# ---------------------------------------------------------------- tracker

def test_rejected_requests_free_their_room_and_stale_pending_ones_expire(world):
    teller = _small_pool(world, "09170000071", daily=1000)
    a = gcash.list_accounts(world.db, owner_id=teller.id)[0]
    req = _load(world, 800)
    usage = lambda: gcash.usage_for(world.db, [a], world.store)[0]       # noqa: E731
    assert usage().day_used == 800 * PESO
    cashdesk.review(world.db, teller, request_id=req.id, approve=False, settings=world.store, note="no")
    assert usage().day_used == 0
    stale = _load(world, 700)
    stale.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=500)
    world.db.flush()
    assert usage().day_used == 0                                           # hold window is 120 min


def test_outside_usage_counts_and_cashouts_can_be_excluded(world):
    teller = _small_pool(world, "09170000081", daily=1000)
    a = gcash.list_accounts(world.db, owner_id=teller.id)[0]
    gcash.log_outside_usage(world.db, teller, a.id, amount_cents=300 * PESO, note="personal")
    assert gcash.usage_for(world.db, [a], world.store)[0].day_used == 300 * PESO
    out = CashRequest(player_id=world.player.id, requested_by_id=world.admin.id, direction=RequestDirection.OUT,
                      method="gcash", amount_cents=200 * PESO, status=RequestStatus.PENDING,
                      gcash_account_id=a.id)
    world.db.add(out)
    world.db.flush()
    assert gcash.usage_for(world.db, [a], world.store)[0].day_used == 500 * PESO
    world.store.set("gcash.count_cashout", "false", world.admin.id)
    assert gcash.usage_for(world.db, [a], world.store)[0].day_used == 300 * PESO


def test_no_pool_means_the_old_behaviour(world):
    assert not gcash.pool_exists(world.db)
    assert _load(world, 5000).gcash_account_id is None


# ---------------------------------------------------------------- ownership

def test_a_teller_only_sees_and_reviews_requests_on_their_own_numbers(world):
    t1 = _teller(world.db, world.admin, world.store, "09170000091", "ta")
    t2 = _teller(world.db, world.admin, world.store, "09170000092", "tb")
    own = gcash.list_accounts(world.db, owner_id=t1.id)[0]
    gcash.set_active(world.db, t2, gcash.list_accounts(world.db, owner_id=t2.id)[0].id, False)
    req = _load(world, 100)
    assert req.gcash_account_id == own.id
    assert req.id in {r.id for r in cashdesk.list_requests(world.db, t1, view="in")}
    assert req.id not in {r.id for r in cashdesk.list_requests(world.db, t2, view="in")}
    with pytest.raises(Forbidden):
        cashdesk.review(world.db, t2, request_id=req.id, approve=True, settings=world.store)
    cashdesk.review(world.db, t1, request_id=req.id, approve=True, settings=world.store)
    assert req.status == RequestStatus.APPROVED


def test_a_teller_cannot_touch_another_tellers_number(world):
    t1 = _teller(world.db, world.admin, world.store, "09170000101", "ta")
    t2 = _teller(world.db, world.admin, world.store, "09170000102", "tb")
    other = gcash.list_accounts(world.db, owner_id=t2.id)[0]
    with pytest.raises(Forbidden):
        gcash.set_active(world.db, t1, other.id, False)


def test_removing_a_used_number_pauses_it_and_an_unused_one_is_deleted(world):
    teller = _small_pool(world, "09170000111", "09170000112", daily=1000)
    a, b = gcash.list_accounts(world.db, owner_id=teller.id)
    _load(world, 100)
    assert "paused" in gcash.remove_account(world.db, teller, a.id)
    assert not a.is_active
    assert "removed" in gcash.remove_account(world.db, teller, b.id)


# ---------------------------------------------------------------- Staff Panel gates

def _csrf(c):
    return c.cookies.get("snx_csrf").split(".")[0]


def test_staff_panel_sections_open_only_for_their_role(world):
    db, admin, store = world.db, world.admin, world.store
    teller = _teller(db, admin, store, "09170000121")
    decl = _user(db, admin, store, Role.DECLARATOR, "dc")
    master = _user(db, admin, store, Role.MASTER, "ms")
    pawn = _user(db, admin, store, Role.PAWN, "pw")
    pawn_ok = _user(db, admin, store, Role.PAWN, "pg")
    agents.set_pawn_grants(db, admin, pawn_ok.id, ["staff:teller"])
    db.commit()

    def code(user, path):
        c, r = login(user.username)
        assert r.status_code == 303
        return c.get(path).status_code

    assert code(teller, "/staff") == 200 and code(teller, "/staff/teller") == 200
    assert code(teller, "/staff/declarator") == 403
    assert code(decl, "/staff") == 200 and code(decl, "/staff/declarator") == 200
    assert code(decl, "/staff/teller") == 403
    assert code(master, "/staff") == 403 and code(master, "/staff/teller") == 403
    assert code(pawn, "/staff") == 403 and code(pawn, "/staff/teller") == 403
    assert code(pawn_ok, "/staff/teller") == 200 and code(pawn_ok, "/staff/declarator") == 403
    c, _ = login("admin")
    assert c.get("/staff/teller").status_code == 200 and c.get("/staff/declarator").status_code == 200


def test_teller_adds_a_number_through_the_page_and_a_pawn_cannot(world):
    db, admin, store = world.db, world.admin, world.store
    teller = _teller(db, admin, store, "09170000131")
    pawn = _user(db, admin, store, Role.PAWN, "pr")
    agents.set_pawn_grants(db, admin, pawn.id, ["staff:teller"])
    db.commit()
    c, _ = login(teller.username)
    r = c.post("/staff/teller/accounts", data={"csrf_token": _csrf(c), "label": "Backup",
                                               "number": "09170000132", "daily": "50000", "monthly": ""})
    assert r.status_code == 303
    page = c.get("/staff/teller")
    assert "Backup" in page.text and "09170000132" in page.text and "Next up" in page.text
    pc, _ = login(pawn.username)
    assert "09170000132" not in pc.get("/staff/teller").text.replace("•", "")      # masked for a pawn
    denied = pc.post("/staff/teller/accounts", data={"csrf_token": _csrf(pc), "label": "X", "number": "09170000133"})
    assert denied.status_code == 303          # form errors come back as a flash redirect
    db.expire_all()
    assert len(gcash.list_accounts(db, owner_id=teller.id)) == 2
    assert not any(gcash.number_of(a) == "09170000133" for a in gcash.list_accounts(db))


def test_wallet_shows_the_assigned_number_to_the_requester(world):
    db, admin, store = world.db, world.admin, world.store
    _teller(db, admin, store, "09170000141")
    db.commit()
    c, _ = login("admin")
    r = c.post("/wallet/request", data={"csrf_token": _csrf(c), "direction": "in", "subject": f"player:{world.player.id}",
                                        "method": "gcash", "amount": "250", "reference": "GC-WEB-1"})
    assert r.status_code == 303
    page = c.get("/wallet?t=load")
    assert "09170000141" in page.text
