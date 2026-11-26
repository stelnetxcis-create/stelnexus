"""Scatter (the slot): the money side, the settings, the pages. The maths is in test_scatter_engine.py."""
import pytest
from sqlalchemy import func, select

from app import games
from app.core.errors import Conflict, Forbidden, InsufficientFunds, LockedState, ValidationError
from app.db.models import Role, ScatterRound, User
from app.games import scatter_engine as eng
from app.services import accounts, ledger, scatter
from tests.conftest import fund_user
from tests.test_web import login


def _credit(w, user):
    return accounts.wallet_for_user(w.db, user).balance_cents


def _fund(w, cents):
    fund_user(w.db, w.admin, w.admin, cents)
    w.db.flush()


def _rounds(w):
    return int(w.db.scalar(select(func.count(ScatterRound.id))) or 0)


# ---------------------------------------------------------------- the money
def test_a_spin_moves_exactly_the_stake_and_the_win(world):
    w = world
    _fund(w, 500_000)
    wallet0, house0 = _credit(w, w.admin), accounts.scatter_house(w.db).balance_cents
    made = []
    for _ in range(25):
        before = _credit(w, w.admin)
        rnd, result, balance = scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store)
        made.append(rnd)
        assert rnd.stake_cents == 1000 and rnd.win_cents == 1000 * result["win"] // 100
        assert balance == before - 1000 + rnd.win_cents == _credit(w, w.admin)
        assert rnd.win_x100 == result["win"] and rnd.stake_txn_id
        assert bool(rnd.win_txn_id) == (rnd.win_cents > 0)
    paid, staked = sum(r.win_cents for r in made), 25 * 1000
    # the Scatter house account is the other side of every posting: it keeps what the game kept, and may go negative
    assert accounts.scatter_house(w.db).balance_cents - house0 == staked - paid
    assert _credit(w, w.admin) - wallet0 == paid - staked
    assert ledger.verify_integrity(w.db)["ok"]


def test_not_enough_credit_books_nothing(world):
    w = world
    poor = User(username=f"sp{w.tag}", role=Role.AGENT, full_name="Poor", status=w.admin.status,
                password_hash="x", ref_code=f"SP{w.tag}")
    w.db.add(poor)
    w.db.flush()
    accounts.wallet_for_user(w.db, poor)
    fund_user(w.db, w.admin, poor, 500)                       # PHP 5, the cheapest bet is PHP 10
    n = _rounds(w)
    with pytest.raises(InsufficientFunds):
        scatter.spin(w.db, poor, bet_cents=1000, settings=w.store)
    assert _credit(w, poor) == 500 and _rounds(w) == n


def test_an_account_without_a_balance_cannot_play(world):
    w = world
    staff = User(username="decl-scatter", role=Role.DECLARATOR, full_name="D")
    with pytest.raises(Forbidden):
        scatter.spin(w.db, staff, bet_cents=1000, settings=w.store)


def test_a_repeated_request_id_charges_once(world):
    w = world
    _fund(w, 100_000)
    before = _credit(w, w.admin)
    a, ra, _ = scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store, idempotency_key="dup-1")
    mid = _credit(w, w.admin)
    b, rb, _ = scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store, idempotency_key="dup-1")
    assert a.id == b.id and ra["win"] == rb["win"] and _credit(w, w.admin) == mid == before - 1000 + a.win_cents
    with pytest.raises(Conflict):                             # same id, different bet: not the same spin
        scatter.spin(w.db, w.admin, bet_cents=2000, settings=w.store, idempotency_key="dup-1")


def test_only_listed_bets_are_accepted(world):
    w = world
    _fund(w, 100_000)
    for bad in (999, 0, -1000, 1500, "1000", None, True):
        with pytest.raises(ValidationError):
            scatter.spin(w.db, w.admin, bet_cents=bad, settings=w.store)


def test_the_bet_ladder_follows_the_settings(world):
    w = world
    try:
        assert scatter.bet_ladder(w.store) == [1000, 2000, 5000, 10000, 20000, 50000, 100000]
        w.store.set("scatter.bet_ladder", "5, 10,abc,0,20000", w.admin.id)
        assert scatter.bet_ladder(w.store) == [1000]          # 5 is under the minimum, 20000 is over the maximum
        w.store.set("scatter.bet_ladder", "", w.admin.id)
        assert scatter.bet_ladder(w.store) == [1000]          # never empty: falls back to the minimum bet
    finally:
        w.store.set("scatter.bet_ladder", "10,20,50,100,200,500,1000", w.admin.id)


def test_switched_off_refuses_every_spin(world):
    w = world
    _fund(w, 100_000)
    try:
        w.store.set("scatter.enabled", "false", w.admin.id)
        n = _rounds(w)
        with pytest.raises(LockedState):
            scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store)
        assert _rounds(w) == n
        assert not games.GAMES["scatter"].visible(_Ctx(w))
    finally:
        w.store.set("scatter.enabled", "true", w.admin.id)
    assert games.GAMES["scatter"].visible(_Ctx(w))


class _Ctx:
    def __init__(self, w):
        self.store, self.user = w.store, w.admin

    def can(self, perm):
        return True


def test_the_win_cap_is_read_from_the_settings(world):
    w = world
    _fund(w, 100_000)
    try:
        w.store.set("scatter.max_win_x", "100", w.admin.id)
        for _ in range(40):
            rnd, result, _b = scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store)
            assert rnd.max_win_x == 100 and rnd.win_cents <= 1000 * 100
    finally:
        w.store.set("scatter.max_win_x", "5000", w.admin.id)


def test_every_round_can_be_replayed_from_its_seed(world):
    w = world
    _fund(w, 100_000)
    for _ in range(10):
        rnd, result, _b = scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store)
        assert scatter.verify(rnd)
        assert eng.play_seeded(rnd.seed, max_win_x=rnd.max_win_x)["win"] == rnd.win_x100
    rnd.win_x100 += 1
    assert not scatter.verify(rnd)                            # a tampered figure no longer matches its seed
    w.db.rollback()


def test_the_daily_limit_counts_scatter_stakes(world):
    from app.services import players
    w = world
    user = User(username=f"sd{w.tag}", role=Role.AGENT, full_name="Lim", status=w.admin.status,
                password_hash="x", ref_code=f"SD{w.tag}")
    w.db.add(user)
    w.db.flush()
    accounts.wallet_for_user(w.db, user)
    fund_user(w.db, w.admin, user, 1_000_000)
    try:
        w.store.set("account.daily_bet_limit_cents", "3000", w.admin.id)
        scatter.spin(w.db, user, bet_cents=2000, settings=w.store)
        assert players.account_staked_today(w.db, user) == 2000
        with pytest.raises(Forbidden):
            scatter.spin(w.db, user, bet_cents=2000, settings=w.store)
    finally:
        w.store.set("account.daily_bet_limit_cents", "0", w.admin.id)


# ---------------------------------------------------------------- the pages
def test_the_page_the_spin_and_the_records_over_http(world):
    w = world
    _fund(w, 500_000)
    w.db.commit()                                             # release SQLite's single writer for the app's own connection
    c, _ = login("admin")
    page = c.get("/scatter")
    assert page.status_code == 200 and 'id="sx"' in page.text and "Rules &amp; paytable" in page.text
    token = c.cookies.get("snx_csrf").split(".")[0]
    asjson = {"Accept": "application/json"}

    r = c.post("/scatter/spin", data={"bet": "1000", "key": "http-1", "csrf_token": token}, headers=asjson)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["bet"] == 1000 and body["result"]["spins"] and "seed" not in r.text
    assert body["win"] == 1000 * body["result"]["win"] // 100
    again = c.post("/scatter/spin", data={"bet": "1000", "key": "http-1", "csrf_token": token}, headers=asjson).json()
    assert again["code"] == body["code"]                      # a retry is the same round, not a second charge

    bad = c.post("/scatter/spin", data={"bet": "777", "csrf_token": token}, headers=asjson)
    assert bad.status_code in (400, 422) and bad.json()["error"]
    assert c.post("/scatter/spin", data={"bet": "1000"}, headers=asjson).status_code == 403     # no CSRF token

    assert body["code"] in c.get("/records?tab=tickets&game=scatter").text
    assert 'data-game="scatter"' in c.get("/games").text
    anon = login("nobody-here")[0]
    assert anon.post("/scatter/spin", data={"bet": "1000"}, headers=asjson).status_code in (401, 403)


def test_the_ledger_stays_balanced_after_many_spins(world):
    w = world
    _fund(w, 2_000_000)
    for _ in range(150):
        scatter.spin(w.db, w.admin, bet_cents=1000, settings=w.store)
    assert ledger.verify_integrity(w.db)["ok"]
