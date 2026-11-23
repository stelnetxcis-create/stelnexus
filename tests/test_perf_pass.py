"""Pagination clamps and pagers, precomputed report snapshots, the streamed Bola CSV, the prod
SQLite guard and the SQLite -> PostgreSQL migration."""
import argparse
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import make_url
from starlette.requests import Request

from app.core.config import Settings, get_settings
from app.core.pagination import MAX_OFFSET, clamp_limit, clamp_offset
from app.db.models import BolaKind, EventStatus, ReportSnapshot, Result, Side
from app.db.session import engine
from app.main import app
from app.services import betting, events, reports, snapshots
from tests.conftest import PASSWORD
from tests.test_bola import _bet, bw  # noqa: F401  (fixture)
from tests.test_features import declare, sell
from tests.test_web import login


def _req(qs: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/x", "query_string": qs.encode(),
                    "headers": [], "server": ("test", 80), "scheme": "http"})


def _api():
    c = TestClient(app)
    tok = c.post("/api/v1/auth/token", json={"username": "admin", "password": PASSWORD}).json()["token"]
    return c, {"Authorization": f"Bearer {tok}"}


# ----------------------------------------------------------------------------- clamps
def test_clamp_limit_is_always_between_one_and_the_maximum():
    assert clamp_limit(-1, 100) == 1 and clamp_limit(0, 100) == 1
    assert clamp_limit(10**9, 100) == 100 and clamp_limit(25, 100) == 25
    assert clamp_limit(None, 100) == 100 and clamp_limit("abc", 100, default=7) == 7


def test_clamp_offset_is_never_negative_or_unbounded():
    assert clamp_offset(-5) == 0 and clamp_offset(None) == 0 and clamp_offset("x") == 0
    assert clamp_offset(10**12) == MAX_OFFSET and clamp_offset(40) == 40


def test_negative_limit_cannot_return_the_whole_table(world):
    """SQLite treats LIMIT -1 as 'no limit'; the clamp turns it into one row."""
    for i in range(3):
        sell(world, world.games[i], Side.MERON, 2000)
    world.db.flush()
    assert len(betting.list_tickets(world.db, world.admin, limit=-1)) == 1
    assert len(betting.list_tickets(world.db, world.admin, limit=0)) == 1


def test_api_negative_limit_is_bounded(world):
    events.create_event(world.db, world.admin, title=f"Second {world.tag}",
                        event_date=world.event.event_date, settings=world.store, fight_count=1)
    world.db.commit()
    c, h = _api()
    for path, key in (("/api/v1/events", "events"), ("/api/v1/tickets", "tickets")):
        r = c.get(path, params={"limit": -1}, headers=h)
        assert r.status_code == 200 and len(r.json()[key]) <= 1, path
    assert len(c.get("/api/v1/events", params={"limit": 1, "offset": 1}, headers=h).json()["events"]) <= 1


# ----------------------------------------------------------------------------- pagers
def test_two_pagers_on_one_page_are_independent():
    from app.web.paging import paginate
    data = list(range(10))
    pager, rows = paginate(_req("tpage=1&lpage=2&q=a"), 2,
                           lambda limit, offset: data[offset:offset + limit], param="tpage")
    assert rows == [2, 3] and pager.has_next
    assert "tpage=2" in pager.next_url and "lpage=2" in pager.next_url and "q=a" in pager.next_url
    assert "tpage" not in pager.prev_url and "lpage=2" in pager.prev_url      # page 0 drops its param


def test_page_past_the_end_still_has_a_way_back():
    from app.web.paging import paginate
    pager, rows = paginate(_req("page=99"), 5, lambda limit, offset: [])
    assert rows == [] and pager.prev_url and not pager.has_next


@pytest.mark.parametrize("path", [
    "/archives", "/bola/results", "/admin/ipbans", "/admin/accounts", "/referrals", "/wallet?t=approvals",
    "/archives?page=3", "/bola/results?page=3", "/admin/ipbans?page=3",
    "/admin/accounts?upage=2&ppage=2", "/referrals?page=3"])
def test_paged_pages_render(path):
    c, _ = login("admin")
    assert c.get(path, follow_redirects=True).status_code == 200, path


def test_player_history_pages_bets_and_ledger_separately(world):
    for i in range(3):
        sell(world, world.games[i], Side.MERON, 2000)
    world.db.commit()
    c, _ = login("admin")
    for qs in ("", "?tpage=1", "?lpage=1", "?tpage=1&lpage=1", "?tpage=50"):
        assert c.get(f"/clp/{world.player.id}/history{qs}").status_code == 200, qs


# ----------------------------------------------------------------------------- reports
def test_event_summaries_match_the_old_per_event_numbers(world):
    from app.db.models import Commission, EventExpense, Fight, SettlementRun, Ticket
    from app.db.models import CommissionStatus, RunStatus, TicketStatus
    sell(world, world.games[0], Side.MERON, 10000)
    sell(world, world.games[0], Side.WALA, 10000)
    declare(world, world.games[0], Result.MERON)
    events.add_expense(world.db, world.admin, world.event.id, label="Fee", amount_cents=300)
    other = events.create_event(world.db, world.admin, title=f"Other {world.tag}",
                                event_date=world.event.event_date, settings=world.store, fight_count=1)
    world.db.flush()
    got = reports.event_summaries(world.db, [world.event.id, other.id, world.event.id])
    db, eid = world.db, world.event.id
    live = [TicketStatus.ACTIVE, TicketStatus.SETTLED, TicketStatus.REFUNDED]
    staked = db.scalar(select(func.coalesce(func.sum(Ticket.stake_cents), 0))
                       .where(Ticket.event_id == eid, Ticket.status.in_(live)))
    house = sum(int((r.totals or {}).get("house_take", 0)) for r in db.scalars(
        select(SettlementRun).join(Fight, Fight.id == SettlementRun.fight_id)
        .where(Fight.event_id == eid, SettlementRun.status == RunStatus.APPLIED)))
    commission = db.scalar(select(func.coalesce(func.sum(Commission.amount_cents), 0))
                           .join(Fight, Fight.id == Commission.fight_id)
                           .where(Fight.event_id == eid, Commission.status != CommissionStatus.REVERSED))
    expenses = db.scalar(select(func.coalesce(func.sum(EventExpense.amount_cents), 0))
                         .where(EventExpense.event_id == eid))
    row = got[eid]
    assert (row["staked"], row["house"], row["commission"], row["expenses"]) == (
        staked, house, commission, expenses) and staked == 20000 and expenses == 300
    assert row["net"] == house - commission - expenses
    assert got[other.id] == {"staked": 0, "payouts": 0, "house": 0, "commission": 0, "expenses": 0, "net": 0}
    assert reports.event_summary(db, eid) == row


def test_event_summaries_use_a_constant_number_of_queries(world):
    extra = [events.create_event(world.db, world.admin, title=f"E{i} {world.tag}",
                                 event_date=world.event.event_date, settings=world.store, fight_count=1).id
             for i in range(5)]
    world.db.flush()
    seen = []
    listener = lambda *a, **k: seen.append(1)          # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        reports.event_summaries(world.db, extra + [world.event.id])
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert len(seen) <= 4


def test_cashflow_with_precomputed_flows_equals_live(world):
    sell(world, world.games[0], Side.MERON, 5000)
    declare(world, world.games[0], Result.MERON)
    flows = reports.cashflow_flows(world.db)
    assert reports.cashflow(world.db, flows) == reports.cashflow(world.db)
    assert reports.dashboard(world.db, world.admin, flows=flows)["cashflow"] == reports.cashflow(world.db)


# ----------------------------------------------------------------------------- snapshots
def test_snapshot_write_read_roundtrip_and_single_row(db):
    from app.db.models import Role
    scope = f"t{os.urandom(4).hex()}"
    snapshots.write(db, "test.kind", {"a": 1, "role": Role.ADMIN}, scope=scope)
    snapshots.write(db, "test.kind", {"a": 2}, scope=scope)
    assert snapshots.read(db, "test.kind", scope).payload == {"a": 2}
    assert db.scalar(select(func.count()).select_from(ReportSnapshot).where(
        ReportSnapshot.kind == "test.kind", ReportSnapshot.scope == scope)) == 1


def test_snapshot_losing_the_first_insert_race_updates_instead(db):
    scope = f"r{os.urandom(4).hex()}"
    snapshots.write(db, "test.kind", {"v": 1}, scope=scope)
    real, calls = db.scalar, {"n": 0}

    def blind_first_lookup(*a, **k):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*a, **k)

    db.scalar = blind_first_lookup
    try:
        snapshots.write(db, "test.kind", {"v": 2}, scope=scope)     # sees "no row", insert collides
    finally:
        del db.scalar
    assert snapshots.read(db, "test.kind", scope).payload == {"v": 2}


def test_fresh_snapshot_is_served_and_a_stale_one_is_recomputed(db):
    scope = f"s{os.urandom(4).hex()}"
    calls = []
    compute = lambda: calls.append(1) or {"n": len(calls)}      # noqa: E731
    first = snapshots.get(db, "test.kind", compute, scope=scope, max_age_seconds=3600)
    assert first.inline and calls == [1]                        # nothing stored yet: computed inline
    again = snapshots.get(db, "test.kind", compute, scope=scope, max_age_seconds=3600)
    assert not again.inline and calls == [1] and again.payload == {"n": 1}     # served from storage
    import datetime as dt
    row = db.scalar(select(ReportSnapshot).where(ReportSnapshot.kind == "test.kind",
                                                 ReportSnapshot.scope == scope))
    row.computed_at = row.computed_at - dt.timedelta(hours=2)
    db.flush()
    stale = snapshots.get(db, "test.kind", compute, scope=scope, max_age_seconds=60)
    assert stale.inline and calls == [1, 1] and stale.payload == {"n": 2}      # dead timer self-heals


def test_refresh_all_stores_the_three_figures(world):
    sell(world, world.games[0], Side.MERON, 5000)
    done = snapshots.refresh_all(world.db, ["flows", "integrity"])
    assert set(done) == {"flows", "integrity"}
    assert snapshots.read(world.db, snapshots.FLOWS).payload == reports.cashflow_flows(world.db)
    assert snapshots.read(world.db, snapshots.INTEGRITY).payload["ok"] is True
    assert set(snapshots.refresh_all(world.db)) == {"flows", "top", "integrity"}


def test_refresh_reports_cli_rejects_unknown_names():
    from app import cli
    with pytest.raises(SystemExit) as exc:
        cli.refresh_reports(argparse.Namespace(only="flows,bogus"))
    assert "bogus" in str(exc.value)
    cli.refresh_reports(argparse.Namespace(only="flows"))


def test_cashflow_page_and_refresh_button_and_api_dashboard(world):
    world.db.commit()
    c, _ = login("admin")
    html = c.get("/cashflow").text
    assert "as of" in html and "/cashflow/refresh" in html
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post("/cashflow/refresh", data={"csrf_token": token})
    assert r.status_code == 303
    assert c.get("/dashboard").status_code == 200
    api, h = _api()
    assert api.get("/api/v1/dashboard", headers=h).status_code == 200


# ----------------------------------------------------------------------------- streamed CSV
def test_bola_draw_csv_streams_every_ticket_in_batches(bw, monkeypatch):    # noqa: F811
    import app.web.routes_bola as rb
    monkeypatch.setattr(rb, "CSV_BATCH", 2)                       # 5 tickets -> 3 batches
    codes = [_bet(bw, [1, n], BolaKind.PAIR, 100, 0).code for n in range(2, 7)]
    bw.db.commit()
    c, _ = login("admin")
    r = c.get(f"/bola/draws/{bw.draw.id}/report.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0].startswith("Draw,") and any(l.startswith("Code,Type") for l in lines)
    for code in codes:
        assert sum(1 for l in lines if l.startswith(code)) == 1, code
    assert any(l.startswith("Tickets,") for l in lines) and any(l.startswith("Net income") for l in lines)


# ----------------------------------------------------------------------------- PostgreSQL
def test_prod_refuses_sqlite():
    with pytest.raises(Exception, match="SQLite"):
        Settings(_env_file=None, ENV="prod", DATABASE_URL="sqlite:///./x.db", SECRET_KEY="k" * 48)


def test_migration_orders_required_parents_first():
    import app.db.models  # noqa: F401
    from app.db.base import Base
    from app.db.sqlite_to_postgres import _deferred_columns, _ordered_tables
    order = _ordered_tables(Base.metadata)
    pos = {t.name: i for i, t in enumerate(order)}
    assert len(order) == len(Base.metadata.tables)
    for t in order:
        for col in t.columns:
            for fk in col.foreign_keys:
                parent = fk.column.table.name
                if parent != t.name and not col.nullable:
                    assert pos[parent] < pos[t.name], f"{t.name}.{col.name} -> {parent}"
        for col in _deferred_columns(t, pos):
            assert col.nullable, f"{t.name}.{col.name} is deferred but NOT NULL"


def _sqlite_source() -> str:
    url = get_settings().DATABASE_URL
    if not url.startswith("sqlite"):
        pytest.skip("source must be the SQLite test database")
    return f"sqlite:///{Path(make_url(url).database).resolve()}"


def test_migration_dry_run_counts_and_writes_nothing():
    from app.db.sqlite_to_postgres import migrate
    lines: list[str] = []
    rep = migrate(_sqlite_source(), "postgresql+psycopg://u:p@127.0.0.1:1/none", log=lines.append)
    assert not rep.applied and rep.total > 0 and rep.counts["users"][0] >= 1
    assert any("Dry run" in l for l in lines)


def test_migration_rejects_wrong_engines():
    from app.db.sqlite_to_postgres import MigrationError, migrate
    with pytest.raises(MigrationError):
        migrate("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d")
    with pytest.raises(MigrationError):
        migrate(_sqlite_source(), "sqlite:///other.db")


@pytest.mark.skipif(not os.environ.get("MIGRATION_TEST_PG_URL"),
                    reason="set MIGRATION_TEST_PG_URL to an EMPTY, disposable PostgreSQL database")
def test_migration_copies_verifies_and_refuses_a_non_empty_target():
    from sqlalchemy import create_engine
    from app.db.base import Base
    from app.db.sqlite_to_postgres import MigrationError, migrate
    target = os.environ["MIGRATION_TEST_PG_URL"]
    source = _sqlite_source()
    try:
        rep = migrate(source, target, apply=True, log=lambda _m: None)
        assert rep.applied and rep.integrity_ok is not False
        assert all(s == t for s, t in rep.counts.values())
        with pytest.raises(MigrationError, match="already has rows"):
            migrate(source, target, apply=True, log=lambda _m: None)
    finally:
        eng = create_engine(target)
        Base.metadata.drop_all(eng)
        eng.dispose()
