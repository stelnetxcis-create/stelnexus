"""Query-count guard rails (N+1 detection).

Seeds a small agent tree, several players, tickets and a settled fight, then counts the SQL
statements a page issues. The point is not an exact number but that the count does not grow with
the number of rows shown: a regression to per-row lazy loading shows up as a large jump.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db.models import Result, Role, Side
from app.db.session import engine
from app.main import create_app
from app.services import agents, betting, players, settlement
from tests.conftest import PASSWORD, load_credit

app = create_app()


class QueryCounter:
    def __init__(self):
        self.statements: list[str] = []

    def _on(self, conn, cursor, statement, params, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._on)

    @property
    def count(self) -> int:
        return len(self.statements)


def _login(username):
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    c.get("/login")
    token = c.cookies.get("snx_csrf").split(".")[0]
    assert c.post("/login", data={"username": username, "password": PASSWORD,
                                  "csrf_token": token}).status_code == 303
    return c


@pytest.fixture()
def busy(world):
    """A master with 3 agents, each with a sub agent and a funded player who bets; one fight settled."""
    db, admin, store, tag = world.db, world.admin, world.store, world.tag
    master = agents.create_agent(db, admin, username=f"qm{tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="QM", settings=store)
    master.must_change_password = False
    world.master = master
    world.tickets = []
    for i in range(3):
        ag = agents.create_agent(db, master, username=f"qa{i}{tag}", password=PASSWORD, role=Role.AGENT,
                                 full_name=f"QA{i}", settings=store)
        sub = agents.create_agent(db, ag, username=f"qs{i}{tag}", password=PASSWORD, role=Role.SUB_AGENT,
                                  full_name=f"QS{i}", settings=store)
        for j in range(2):
            p = players.register_online_player(
                db, sub, username=f"qp{i}{j}{tag}", password=PASSWORD, full_name="P",
                birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store, agent_id=sub.id)
            p.kyc_verified = True
            load_credit(db, admin, store, p, 500_000)
            for side in (Side.MERON, Side.WALA):
                world.tickets.append(betting.place_bet(
                    db, sub, fight_id=world.games[0].id, side=side, stake_cents=10_000,
                    settings=store, player_id=p.id))
    settlement.declare(db, admin, fight_id=world.games[0].id, result=Result.MERON, settings=store)
    db.commit()
    return world


def _count(client, path):
    with QueryCounter() as q:
        r = client.get(path)
    return q.count if r.status_code == 200 else f"{q.count} (HTTP {r.status_code})"


# Pages whose query count must NOT grow with the number of rows they list (N+1 guard).
LISTS = ["/agents", "/commissions", "/clp", "/wallet", "/cashflow", "/betlogs", "/events", "/history"]


def _grow(world, n):
    """Add ``n`` more agents/players/tickets so every list has more rows to render."""
    db, admin, store = world.db, world.admin, world.store
    for i in range(n):
        tag = f"{world.tag}g{i}{int(dt.datetime.now().timestamp() * 1000) % 100000}"
        ag = agents.create_agent(db, world.master, username=f"ga{tag}", password=PASSWORD, role=Role.AGENT,
                                 full_name="GA", settings=store)
        p = players.register_online_player(
            db, ag, username=f"gp{tag}", password=PASSWORD, full_name="GP", birth_date=dt.date(1990, 1, 1),
            gcash_number="09171234567", settings=store, agent_id=ag.id)
        p.kyc_verified = True
        load_credit(db, admin, store, p, 200_000)
        betting.place_bet(db, ag, fight_id=world.games[1].id, side=Side.MERON, stake_cents=5_000,
                          settings=store, player_id=p.id)
    db.commit()


def test_list_pages_do_not_issue_a_query_per_row(busy):
    c = _login("admin")
    before = {path: _count(c, path) for path in LISTS}
    _grow(busy, 6)
    after = {path: _count(c, path) for path in LISTS}
    for path in LISTS:
        assert isinstance(before[path], int) and isinstance(after[path], int), (path, before[path], after[path])
        # 6 more rows each: an N+1 would add 6 or more queries; allow 1 for incidental differences
        assert after[path] <= before[path] + 1, f"{path}: {before[path]} -> {after[path]} queries (N+1?)"


def test_batch_balance_helpers_match_single_row_lookups(busy):
    from app.services import accounts
    db = busy.db
    users = [busy.master] + [u for u in db.query(agents.User).filter(agents.User.parent_id == busy.master.id)]
    got = accounts.user_balances(db, users)
    for u in users:
        assert got[u.id] == (agents.wallet_balance(db, u), agents.commission_balance(db, u))
    pl = [t.player_id for t in busy.tickets]
    wallets = accounts.player_wallets(db, pl)
    for pid in set(pl):
        assert wallets[pid] == accounts.wallet_for_player(db, db.get(players.Player, pid)).balance_cents


def test_integrity_check_is_two_queries_regardless_of_accounts(busy):
    from app.services import ledger
    with QueryCounter() as q:
        report = ledger.verify_integrity(busy.db)
    assert report["ok"]
    assert q.count <= 4, q.count          # unbalanced txns, accounts, grouped nets (+ slack)


def test_dashboard_cache_serves_then_clears_on_commit(busy, monkeypatch):
    from app.core import ttlcache
    from app.core.config import get_settings
    from app.services import events
    monkeypatch.setattr(get_settings(), "DASHBOARD_CACHE_SECONDS", 30)
    ttlcache.clear()
    c = _login("admin")
    cold = _count(c, "/dashboard")
    warm = _count(c, "/dashboard")
    assert warm < cold - 10, (cold, warm)                  # KPI queries skipped on the second view
    # a staff action that commits clears the cache, so the next view is recomputed
    events.create_event(busy.db, busy.admin, title="Cache probe", event_date=dt.date.today(),
                        settings=busy.store, fight_count=1)
    busy.db.commit()
    assert _count(c, "/dashboard") >= cold - 3


def test_ttlcache_returns_independent_copies_and_expires(monkeypatch):
    from app.core import ttlcache
    ttlcache.clear()
    calls = []

    def compute():
        calls.append(1)
        return {"n": [1, 2, 3]}

    a = ttlcache.cached(("t", 1), 60, compute)
    a["n"].append(99)                                      # caller mutates its copy
    b = ttlcache.cached(("t", 1), 60, compute)
    assert b == {"n": [1, 2, 3]} and len(calls) == 1
    assert ttlcache.cached(("t", 2), 0, compute) and len(calls) == 2   # ttl 0 = never cached
    ttlcache.clear("t")
    ttlcache.cached(("t", 1), 60, compute)
    assert len(calls) == 3


# ----------------------------------------------------------------------------- pagination
def test_pager_links_keep_filters_and_clamp_page(busy):
    from starlette.requests import Request
    from app.web.paging import MAX_PAGE, make_pager, page_number

    def req(q):
        return Request({"type": "http", "method": "GET", "path": "/betlogs", "query_string": q.encode(),
                        "headers": [], "scheme": "http", "server": ("x", 80)})

    assert page_number(req("page=abc")) == 0 and page_number(req("page=-4")) == 0
    assert page_number(req("page=999999999")) == MAX_PAGE
    r = req("status=active&q=ab&page=2")
    p = make_pager(r, 300, 2, 300, True)
    assert p.prev_url == "/betlogs?status=active&q=ab&page=1"
    assert p.next_url == "/betlogs?status=active&q=ab&page=3"
    assert make_pager(r, 300, 1, 10, False).prev_url == "/betlogs?status=active&q=ab"   # page 0 drops ?page
    assert (p.first, p.last) == (601, 900)


def test_paginate_uses_lookahead_row():
    from starlette.requests import Request
    from app.web.paging import paginate
    r = Request({"type": "http", "method": "GET", "path": "/x", "query_string": b"page=1", "headers": [],
                 "scheme": "http", "server": ("x", 80)})
    data = list(range(25))
    seen = {}

    def fetch(limit, offset):
        seen.update(limit=limit, offset=offset)
        return data[offset:offset + limit]

    pager, rows = paginate(r, 10, fetch)
    assert seen == {"limit": 11, "offset": 10} and rows == list(range(10, 20)) and pager.has_next
    pager, rows = paginate(Request({**r.scope, "query_string": b"page=2"}), 10, fetch)
    assert rows == list(range(20, 25)) and not pager.has_next and pager.next_url is None


def test_older_audit_rows_are_reachable(busy):
    """The audit trail used to stop at the newest 300 rows. Page 1 must now show the older ones."""
    from app.db.models import AuditLog
    db = busy.db
    marker = f"pg{busy.tag}"
    db.add_all(AuditLog(action=f"{marker}.{i:04d}", entity_type=marker) for i in range(330))
    db.commit()
    c = _login("admin")
    first = c.get(f"/admin/audit?entity={marker}").text
    second = c.get(f"/admin/audit?entity={marker}&page=1").text
    # newest first: ids are inserted in order, so page 0 holds .0329 down to .0030, page 1 holds the rest
    assert f"{marker}.0329" in first and f"{marker}.0029" not in first
    assert f"{marker}.0029" in second and f"{marker}.0000" in second and f"{marker}.0329" not in second
    assert "Older" in first and "page=1" in first
    assert "Newer" in second
    assert "Older" not in second            # 30 rows left: no further page


def test_commission_lines_paginate_across_both_games(busy):
    from app.services import commissions
    db = busy.db
    everything = commissions.list_scope(db, busy.admin, limit=1000)
    assert len(everything) >= 4
    first = commissions.list_scope(db, busy.admin, limit=2, offset=0)
    second = commissions.list_scope(db, busy.admin, limit=2, offset=2)
    assert first + second == everything[:4]                 # slices tile the merged, newest-first list
    assert commissions.list_scope(db, busy.admin, limit=2, offset=10_000) == []
    c = _login("admin")
    assert c.get("/commissions?page=1").status_code == 200
