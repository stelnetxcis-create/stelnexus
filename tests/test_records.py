import csv
import datetime as dt
import io
import re

import pytest
from sqlalchemy import select

from app.db.models import Role, Side, Ticket
from app.services import agents, betting, reports, settlement
from tests.conftest import PASSWORD
from tests.test_web import login


@pytest.fixture()
def bet(world):
    w = world
    t = betting.place_bet(w.db, w.admin, fight_id=w.games[0].id, side=Side.MERON, stake_cents=10_000,
                          settings=w.store, player_id=w.player.id, idempotency_key=f"rec-{w.tag}")
    w.db.commit()
    return t


def test_hub_lists_both_games_for_admin():
    c, _ = login("admin")
    html = c.get("/games").text
    assert 'data-game="sabong"' in html


def test_old_and_new_ticket_views_agree(bet):
    c, _ = login("admin")
    old = c.get("/betlogs", follow_redirects=True).text
    new = c.get("/records?tab=tickets&game=sabong").text
    assert bet.code in old and bet.code in new
    assert "100.00" in new


def test_csv_equals_table(bet):
    c, _ = login("admin")
    table = c.get("/records?tab=tickets&game=sabong&q=" + bet.code).text
    body = c.get("/records.csv?tab=tickets&game=sabong&q=" + bet.code).text
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0][:3] == ["Time", "Code", "Game"]
    assert len(rows) - 1 == len(re.findall(r'<td class="mono small"><a href', table)) == 1
    assert rows[1][1] == bet.code and rows[1][5] == "100.00"


def test_date_filter_excludes_other_days(bet):
    c, _ = login("admin")
    html = c.get("/records?tab=tickets&game=sabong&start=2001-01-01&end=2001-01-02").text
    assert bet.code not in html


def test_declarator_sees_no_tickets_or_money(world, bet):
    w = world
    dec = agents.create_agent(w.db, w.admin, username=f"dc{w.tag}", password=PASSWORD,
                              role=Role.DECLARATOR, full_name="Dec")
    w.db.commit()
    c, _ = login(f"dc{w.tag}")
    assert c.get("/records?tab=tickets").status_code in (302, 303, 403)
    assert bet.code not in c.get("/records?tab=history").text
    assert c.get("/records.csv?tab=tickets").status_code in (302, 303, 403)


def test_daily_report_total_equals_archive_rows(world, bet):
    w = world
    settlement.declare(w.db, w.admin, fight_id=w.games[0].id, result=__import__("app.db.models", fromlist=["Result"]).Result.MERON,
                       settings=w.store)
    w.db.commit()
    from app.games import GAMES

    class _Ctx:  # the adapters only need db, store, user and can()
        db, store, user = w.db, w.store, w.admin
        def can(self, _): return True
        def who(self, u, empty="—"): return u.username if u else empty

    rows = GAMES["sabong"].report_daily(_Ctx, w.event.event_date)
    mine = [r for r in rows if r.label == w.event.title]
    summary = reports.event_summary(w.db, w.event.id)
    assert len(mine) == 1
    assert (mine[0].staked, mine[0].paid, mine[0].house, mine[0].net) == (
        summary["staked"], summary["payouts"], summary["house"], summary["net"])
    month = GAMES["sabong"].report_monthly(_Ctx, w.event.event_date.year, w.event.event_date.month)
    assert sum(r.staked for r in month) >= summary["staked"]
    day_rows = GAMES["sabong"].report_daily(_Ctx, w.event.event_date)
    same_day = [r for r in month if r.label == w.event.event_date.isoformat()]
    assert len(same_day) == 1
    assert same_day[0].staked == sum(r.staked for r in day_rows)
    assert same_day[0].net == sum(r.net for r in day_rows)


def test_history_keeps_location_role_and_photo_columns(world, bet):
    from app.db.models import Result
    w = world
    settlement.declare(w.db, w.admin, fight_id=w.games[0].id, result=Result.MERON, settings=w.store)
    w.db.commit()
    c, _ = login("admin")
    html = c.get("/records?tab=history&game=sabong").text
    for head in ("<th>Location</th>", "<th>Llamado / Dehado</th>", "<th>Photo</th>"):
        assert head in html
    assert f"Fight #{w.games[0].number}" in html
