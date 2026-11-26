"""Bola (Tumbok-Sahod) smoke + core-rule tests. Extend per plan section 9."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.core.errors import Forbidden, LockedState, StelError, ValidationError
from app.core.money import payout_of
from app.db.models import (
    BolaCommission, BolaExposure, BolaKind, CommissionStatus, DrawStatus, Role, Status, TicketStatus,
)
from app.services import (
    accounts, agents, bola, bola_settlement, commissions, ledger, locations,
)
from tests.conftest import PASSWORD, fund_user


def test_expand_lines_order_matches_legacy():
    assert bola.expand_lines(BolaKind.PAIR, [12, 24]) == [(12, 24)]
    assert bola.expand_lines(BolaKind.CASAS_3, [12, 5, 36]) == [(12, 5), (12, 36), (5, 36)]
    assert len(bola.expand_lines(BolaKind.CASAS_6, [1, 2, 3, 4])) == 6
    assert len(bola.expand_lines(BolaKind.CASAS_10, [1, 2, 3, 4, 5])) == 10


def test_payout_800x():
    assert payout_of(700, 80000) == 560_000      # legacy dump: PHP 7 x 800 = PHP 5,600


@pytest.fixture()
def bw(world):
    db, admin, store = world.db, world.admin, world.store
    store.set("bola.enabled", "true", admin.id)
    tag = world.tag
    master = agents.create_agent(db, admin, username=f"bm{tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="M", settings=store)
    cavo = agents.create_agent(db, master, username=f"bc{tag}", password=PASSWORD, role=Role.AGENT,
                               full_name="C", settings=store)
    kub = agents.create_agent(db, cavo, username=f"bk{tag}", password=PASSWORD, role=Role.SUB_AGENT,
                              full_name="K", settings=store)
    fund_user(db, admin, kub, 1_000_000)          # bets come out of the bettor's OWN credit
    loc = locations.create_location(db, admin, name=f"Area {tag}")
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    draw = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date.today(), slot=1,
                            settings=store, scheduled_at=soon, status=DrawStatus.OPEN)
    for k, v in dict(master=master, cavo=cavo, kub=kub, draw=draw).items():
        setattr(world, k, v)
    yield world
    loc.is_active = False        # an active area makes later sabong fixtures demand a location
    db.flush()


def _bet(w, numbers, kind, tumbok, sahod):
    return bola.place_bola_bet(w.db, w.kub, draw_id=w.draw.id, kind=kind, numbers=numbers,
                               tumbok_cents=tumbok, sahod_cents=sahod, settings=w.store)


def test_bet_declare_pays_800x_and_books_balance(bw):
    w = bw
    t = _bet(w, [7, 8, 9, 10, 11], BolaKind.CASAS_10, 700, 1900)   # legacy ticket 6, PHP 260
    assert (t.line_count, t.stake_cents) == (10, 26_000)
    bola_settlement.declare(w.db, w.admin, draw_id=w.draw.id, ball_first=7, ball_second=9,
                            settings=w.store, reason="test: declare before draw time")
    w.db.refresh(t)
    assert t.payout_cents == 560_000
    assert ledger.verify_integrity(w.db)["ok"]


def _pool_balance(db):
    from app.services import accounts
    account = accounts.get_by_code(db, "sys:pool")
    return int(account.balance_cents) if account else 0


def test_commissions_written_and_sum_to_pool(bw):
    """Default: the Master is paid too, so the whole network pool (the 10% cut minus the 30% house reserve) is paid
    out, and the reserve is what lands in the system pool."""
    w = bw
    before = _pool_balance(w.db)
    _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)                 # PHP 100 stake
    bola_settlement.declare(w.db, w.admin, draw_id=w.draw.id, ball_first=30, ball_second=31,
                            settings=w.store, reason="test: declare before draw time")
    rows = list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))
    cut = 10_000 * int(w.store.get("bola.commission.pool_bps")) // 10_000
    reserve = cut * int(w.store.get("bola.commission.reserve_bps")) // 10_000
    assert rows and sum(r.amount_cents for r in rows) == cut - reserve
    assert _pool_balance(w.db) - before == reserve
    assert {r.tier for r in rows} == {"master", "agent"}       # the bettor (Sub Agent) earns nothing on its own bet
    assert commissions.totals_for(w.db, w.cavo)["earned"] > 0
    assert ledger.verify_integrity(w.db)["ok"]


def test_master_remainder_stays_with_house_when_pay_master_is_off(bw):
    """With ``bola.commission.pay_master`` off there is no Master line; the Master's share is simply the house's."""
    w = bw
    w.store.set("bola.commission.pay_master", "false", w.admin.id)
    try:
        _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
        bola_settlement.declare(w.db, w.admin, draw_id=w.draw.id, ball_first=30, ball_second=31,
                                settings=w.store, reason="test: declare before draw time")
        rows = list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))
        cut = 10_000 * int(w.store.get("bola.commission.pool_bps")) // 10_000
        network = cut - cut * int(w.store.get("bola.commission.reserve_bps")) // 10_000
        split = commissions.split_pool(network, agent_bps=commissions.effective_agent_bps(w.cavo, w.store, "bola"),
                                       sub_bps=0, has_sub=False)    # the bettor earns nothing on its own bet
        assert {r.tier for r in rows} == {"agent"}
        assert sum(r.amount_cents for r in rows) == split.agent < network
        assert ledger.verify_integrity(w.db)["ok"]
    finally:
        w.store.set("bola.commission.pay_master", "true", w.admin.id)


def test_plan_commission_example_on_a_100_peso_stake():
    """Plan section 9: pool 30%, Agent volume 8333, SOP 4000 on PHP 100."""
    s = commissions.split_pool(3000, agent_bps=8333, sub_bps=4000)
    assert (s.pool, s.agent_gross, s.sub_agent, s.agent, s.master) == (3000, 2499, 999, 1500, 501)
    assert s.paid == s.pool                                        # Master as a commission line
    assert commissions.split_pool(3000, agent_bps=8333, sub_bps=4000, has_master=False).paid == 2499


def test_per_line_limit_enforced(bw):
    w = bw
    for _ in range(15):
        _bet(w, [12, 24], BolaKind.PAIR, 10_000, 0)                # 15 x PHP 100 = PHP 1,500
    with pytest.raises(Exception):
        _bet(w, [12, 24], BolaKind.PAIR, 10_000, 0)                # 16th breaks the PHP 1,500 limit


# ----------------------------------------------------------------- helpers
def _wallet(w):
    return accounts.wallet_for_user(w.db, w.kub).balance_cents


def _declare(w, a, b):
    return bola_settlement.declare(w.db, w.admin, draw_id=w.draw.id, ball_first=a, ball_second=b,
                                   settings=w.store, reason="test: declare before draw time")


def _set(w, key, value):
    w.store.set(key, str(value), w.admin.id)


# -------------------------------------------------------------- validation
@pytest.mark.parametrize("kind,numbers", [
    (BolaKind.PAIR, [0, 5]), (BolaKind.PAIR, [5, 41]), (BolaKind.PAIR, [5, 5]),
    (BolaKind.PAIR, [5]), (BolaKind.CASAS_3, [1, 2]), (BolaKind.CASAS_6, [1, 2, 3, 4, 5]),
])
def test_bad_numbers_rejected_and_nothing_written(bw, kind, numbers):
    before = _wallet(bw)
    with pytest.raises(StelError):
        _bet(bw, numbers, kind, 1000, 1000)
    assert _wallet(bw) == before


@pytest.mark.parametrize("tumbok,sahod", [(0, 0), (50, 0), (0, 50), (-100, 500)])
def test_bad_stakes_rejected(bw, tumbok, sahod):
    before = _wallet(bw)
    with pytest.raises(StelError):
        _bet(bw, [3, 4], BolaKind.PAIR, tumbok, sahod)
    assert _wallet(bw) == before


def test_locked_draw_and_past_cutoff_rejected(bw):
    w = bw
    bola.lock_draw(w.db, w.admin, w.draw.id)
    with pytest.raises(LockedState):
        _bet(w, [1, 2], BolaKind.PAIR, 100, 100)
    bola.open_draw(w.db, w.admin, w.draw.id)
    w.draw.cutoff_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    w.db.flush()
    with pytest.raises(LockedState):
        _bet(w, [1, 2], BolaKind.PAIR, 100, 100)


def test_disabled_switch_blocks_bets(bw):
    _set(bw, "bola.enabled", "false")
    try:
        with pytest.raises(StelError):
            _bet(bw, [1, 2], BolaKind.PAIR, 100, 100)
    finally:
        _set(bw, "bola.enabled", "true")


def test_idempotency_key_returns_same_ticket_and_charges_once(bw):
    w = bw
    kw = dict(draw_id=w.draw.id, kind=BolaKind.PAIR, numbers=[1, 2], tumbok_cents=1000,
              sahod_cents=0, settings=w.store, idempotency_key="bola-idem-1")
    t1 = bola.place_bola_bet(w.db, w.kub, **kw)
    mid = _wallet(w)
    t2 = bola.place_bola_bet(w.db, w.kub, **kw)
    assert t1.id == t2.id and _wallet(w) == mid


# --------------------------------------------------- limits and exposure
def test_liability_cap_boundary(bw):
    w = bw
    _set(w, "bola.max_liability_per_outcome_cents", 10_000_000)       # PHP 100,000 = 125 x 800
    try:
        _bet(w, [7, 9], BolaKind.PAIR, 12_500, 0)                      # exactly the cap: allowed
        with pytest.raises(LockedState):
            _bet(w, [7, 9], BolaKind.PAIR, 100, 0)                     # one peso more: refused
    finally:
        _set(w, "bola.max_liability_per_outcome_cents", 0)


def test_sahod_side_counts_toward_reversed_outcome(bw):
    w = bw
    _set(w, "bola.max_liability_per_outcome_cents", 10_000_000)
    try:
        _bet(w, [9, 7], BolaKind.PAIR, 0, 12_500)                      # sahod of (9,7) pays on 7/9
        with pytest.raises(LockedState):
            _bet(w, [7, 9], BolaKind.PAIR, 100, 0)                     # tumbok of (7,9) pays on 7/9
    finally:
        _set(w, "bola.max_liability_per_outcome_cents", 0)


def test_only_the_admin_can_cancel_a_number_game_bet(bw):
    """Placed bets are final for everyone but the Admin: Sub Agent (owner), Agent and Master are all refused,
    and the stake stays on the ticket."""
    w = bw
    t = _bet(w, [3, 8], BolaKind.PAIR, 10_000, 0)
    for who in (w.kub, w.cavo, w.master):
        with pytest.raises(Forbidden):
            bola.cancel_bola_ticket(w.db, who, ticket_id=t.id, reason="trying to cancel it", settings=w.store)
        assert t.status == TicketStatus.ACTIVE
    from app.core.rbac import Perm, permissions_for
    # the Admin tier: the King, and the Bishops who hold every admin right
    assert [r for r in Role if Perm.BOLA_CANCEL in permissions_for(r)] == [Role.ADMIN, Role.BISHOP]


def test_cancel_refunds_and_frees_exposure(bw):
    w = bw
    start = _wallet(w)
    t = _bet(w, [12, 24], BolaKind.PAIR, 10_000, 0)
    assert _wallet(w) == start - 10_000
    with pytest.raises(Forbidden):                                     # undoing a bet is dev-only: not even its owner
        bola.cancel_bola_ticket(w.db, w.kub, ticket_id=t.id, reason="mistake", settings=w.store)
    assert t.status == TicketStatus.ACTIVE
    bola.cancel_bola_ticket(w.db, w.admin, ticket_id=t.id, reason="mistake", settings=w.store)
    assert _wallet(w) == start and t.status == TicketStatus.CANCELED
    row = w.db.scalar(select(BolaExposure).where(BolaExposure.draw_id == w.draw.id,
                                                 BolaExposure.first == 12, BolaExposure.second == 24))
    assert row is None or (row.tumbok_cents, row.sahod_cents) == (0, 0)
    for _ in range(15):                                                # headroom is back
        _bet(w, [12, 24], BolaKind.PAIR, 10_000, 0)
    assert ledger.verify_integrity(w.db)["ok"]


# ------------------------------------------------------------- settlement
def test_sahod_pays_on_reversed_draw(bw):
    w = bw
    start = _wallet(w)
    t = _bet(w, [24, 12], BolaKind.PAIR, 0, 1000)                      # sahod of (24,12)
    _declare(w, 12, 24)
    w.db.refresh(t)
    assert t.payout_cents == 800_000                                   # PHP 10 x 800 = PHP 8,000
    assert _wallet(w) == start - 1000 + 800_000


def test_losing_ticket_pays_nothing_and_house_keeps_stake(bw):
    w = bw
    start = _wallet(w)
    t = _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    _declare(w, 30, 31)
    w.db.refresh(t)
    assert t.payout_cents == 0 and _wallet(w) == start - 10_000
    assert ledger.verify_integrity(w.db)["ok"]


def test_cannot_declare_twice_or_duplicate_balls(bw):
    w = bw
    with pytest.raises(StelError):
        _declare(w, 5, 5)
    _declare(w, 5, 6)
    with pytest.raises(LockedState):
        _declare(w, 7, 8)


def test_void_refunds_every_stake_and_pays_no_commission(bw):
    w = bw
    start = _wallet(w)
    _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    _bet(w, [3, 4, 5], BolaKind.CASAS_3, 1000, 1000)
    bola_settlement.void_draw(w.db, w.admin, draw_id=w.draw.id, reason="rain delay cancelled",
                              settings=w.store)
    assert _wallet(w) == start and w.draw.status == DrawStatus.VOID
    assert not list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))
    assert ledger.verify_integrity(w.db)["ok"]


def test_redeclare_reverses_old_payout_with_maker_checker(bw):
    w = bw
    start = _wallet(w)
    t = _bet(w, [7, 9], BolaKind.PAIR, 700, 0)
    _declare(w, 30, 31)                                                 # v1: loses, commissions accrue
    assert _wallet(w) == start - 700
    assert list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))
    run = bola_settlement.request_correction(w.db, w.admin, draw_id=w.draw.id, ball_first=7,
                                             ball_second=9, reason="declarator typo",
                                             settings=w.store)
    with pytest.raises(Forbidden):                                      # requester cannot approve
        bola_settlement.approve(w.db, w.admin, run_id=run.id, settings=w.store)
    bola_settlement.approve(w.db, w.master, run_id=run.id, settings=w.store)
    w.db.refresh(t)
    assert t.payout_cents == 560_000 and _wallet(w) == start - 700 + 560_000
    reversed_rows = w.db.scalars(select(BolaCommission).where(
        BolaCommission.draw_id == w.draw.id,
        BolaCommission.status == CommissionStatus.REVERSED)).all()
    assert reversed_rows                                                # v1 commissions reversed
    assert ledger.verify_integrity(w.db)["ok"]


def test_recompute_same_balls_is_idempotent_on_money(bw):
    w = bw
    start = _wallet(w)
    _bet(w, [7, 9], BolaKind.PAIR, 700, 0)
    _declare(w, 7, 9)
    run = bola_settlement.request_correction(w.db, w.admin, draw_id=w.draw.id, ball_first=None,
                                             ball_second=None, reason="re-run the maths",
                                             settings=w.store, action="recompute")
    bola_settlement.approve(w.db, w.master, run_id=run.id, settings=w.store)
    assert _wallet(w) == start - 700 + 560_000
    assert ledger.verify_integrity(w.db)["ok"]


# ------------------------------------------------------------ commissions
def test_suspended_cavo_earns_nothing_and_share_rolls_up(bw):
    """The Cavo's share rolls up to the Master when the Master is paid (D4 alternative), and to
    the house when the Master is not (D4 default). A suspended Cavo never earns either way."""
    w = bw
    w.cavo.status = Status.SUSPENDED
    w.db.flush()
    cut = 10_000 * int(w.store.get("bola.commission.pool_bps")) // 10_000
    pool = cut - cut * int(w.store.get("bola.commission.reserve_bps")) // 10_000      # the network pool
    try:
        w.store.set("bola.commission.pay_master", "true", w.admin.id)
        _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
        _declare(w, 30, 31)
        rows = list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))
        assert rows and all(r.agent_id != w.cavo.id for r in rows)
        assert {r.tier for r in rows} == {"master"} and sum(r.amount_cents for r in rows) == pool
    finally:
        w.store.set("bola.commission.pay_master", "true", w.admin.id)
        w.cavo.status = Status.ACTIVE
        w.db.flush()


def test_suspended_cavo_pays_no_commission_when_the_master_is_not_paid(bw):
    w = bw
    w.cavo.status = Status.SUSPENDED
    w.store.set("bola.commission.pay_master", "false", w.admin.id)     # no Master line: the share goes to the house
    w.db.flush()
    try:
        _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
        _declare(w, 30, 31)
        assert not list(w.db.scalars(select(BolaCommission).where(
            BolaCommission.draw_id == w.draw.id)))
        assert ledger.verify_integrity(w.db)["ok"]
    finally:
        w.store.set("bola.commission.pay_master", "true", w.admin.id)
        w.cavo.status = Status.ACTIVE
        w.db.flush()


def test_no_agent_ticket_pays_no_commission(bw):
    w = bw
    t = _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    t.agent_id = None
    w.db.flush()
    _declare(w, 30, 31)
    assert not list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))


def test_claim_to_wallet_includes_bola_rows(bw):
    w = bw
    _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    _declare(w, 30, 31)
    totals = commissions.totals_for(w.db, w.cavo)
    assert totals["unclaimed"] > 0
    commissions.claim_to_wallet(w.db, w.cavo)
    mine = commissions.list_bola_for(w.db, w.cavo)
    assert mine and all(r.status == CommissionStatus.CLAIMED for r in mine)
    assert commissions.totals_for(w.db, w.cavo)["unclaimed"] == 0
    assert ledger.verify_integrity(w.db)["ok"]


def test_bola_totals_and_commission_page_rows(bw):
    from app.services import reports
    w = bw
    _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    _declare(w, 30, 31)
    totals = reports.bola_totals(w.db)
    assert totals["settled_stake"] >= 10_000 and totals["net"] == (
        totals["house_take"] - totals["commission"])
    assert "bola" in reports.cashflow(w.db)


def test_convert_cut_matches_plan_table():
    from app.cli import convert_cut
    assert convert_cut(0.25, 0.10, 0.30) == (8333, 4000)       # cavo1
    assert convert_cut(0.30, 0.15, 0.30) == (10000, 5000)      # cavo2
    assert convert_cut(0.20, 0.17, 0.30) == (6667, 8500)       # cavo4
    assert convert_cut(0.20, 0.10, 0.30) == (6667, 5000)       # cavo5
    with pytest.raises(ValueError):
        convert_cut(0.0, 0.16, 0.30)                           # cavo3: unset com, needs a ruling


def test_generate_and_tick_services_run(bw):
    w = bw
    out = bola.generate_draws(w.db, None, settings=w.store, days=1)
    assert out["skipped"] >= 0 and isinstance(out["created"], list)
    ticked = bola.tick(w.db, w.store)
    assert set(ticked) == {"opened", "locked"}


# ------------------------------------------------------------------- web
def _web_setup():
    """Committed data (the TestClient uses its own DB session). Returns ids."""
    from sqlalchemy import select as sel
    from app.core.settings_registry import SettingsStore
    from app.db.models import User
    from app.db.session import session_scope
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(sel(User).where(User.username == "admin"))
        store.set("bola.enabled", "true", admin.id)
        # Web / API bets always use the logged-in account's OWN credit, so the account the tests log in as needs some.
        ledger.post(db, kind="request.in", actor=admin, memo="test load",
                    entries=[(accounts.gcash_clearing(db), +1_000_000), (accounts.wallet_for_user(db, admin), -1_000_000)])
        loc = locations.create_location(db, admin, name=f"Web Area {tag}")
        draw = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date.today(), slot=1,
                                settings=store, status=DrawStatus.OPEN,
                                scheduled_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2))
        return {"loc": loc.id, "draw": draw.id}


def _web_teardown(ids):
    from app.db.models import Location
    from app.db.session import session_scope
    with session_scope() as db:
        db.get(Location, ids["loc"]).is_active = False


def test_bola_pages_and_full_bet_flow_over_http():
    from tests.test_web import login
    ids = _web_setup()
    try:
        c, _ = login("admin")
        for path in ["/bola", "/bola/results", "/bola/betlogs", f"/bola/draws/{ids['draw']}"]:
            assert c.get(path, follow_redirects=True).status_code == 200, path
        assert "Bola" in c.get("/dashboard").text                       # nav entry is present

        q = f"/bola/draws/{ids['draw']}/preview?kind=casas3&numbers=12,5,36&tumbok=5&sahod=5"
        pv = c.get(q).json()
        assert pv["ok"] and pv["line_count"] == 3 and pv["stake_cents"] == 3000

        token = c.cookies.get("snx_csrf").split(".")[0]
        r = c.post("/bola/bets/place", data={
            "csrf_token": token, "draw_id": ids["draw"], "kind": "pair",
            "n1": "7", "n2": "9", "tumbok": "7", "sahod": "0", "print": "1"})
        assert r.status_code == 303 and "/bola/tickets/" in r.headers["location"]
        page = c.get(r.headers["location"])
        assert page.status_code == 200 and "7 · 9" in page.text

        token = c.cookies.get("snx_csrf").split(".")[0]
        r = c.post(f"/bola/draws/{ids['draw']}/declare", data={
            "csrf_token": token, "ball_first": "7", "ball_second": "9",
            "reason": "web test, before draw time"})
        assert r.status_code == 303, r.text[:600]
        assert "SETTLED" in c.get(f"/bola/draws/{ids['draw']}").text
        assert c.get("/cashflow").status_code == 200
        assert c.get("/commissions", follow_redirects=True).status_code == 200
    finally:
        _web_teardown(ids)
