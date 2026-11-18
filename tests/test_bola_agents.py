"""Plan sections 4.1 / 5: the Bola cancelled page, per-agent statements, friendly wallet labels."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import Forbidden
from app.db.models import BolaKind, CommissionStatus, DrawStatus, Role
from app.services import accounts, agents, bola, commissions, locations, players, reports, wallet
from tests.conftest import PASSWORD, load_credit
from tests.test_bola import _bet, _declare, bw  # noqa: F401  (bw is a fixture)
from tests.test_web import app, login


# --------------------------------------------------------------- wallet labels (service level)
def test_ledger_kinds_have_friendly_labels():
    assert wallet.label_for("bola.stake") == "Bola bet placed"
    assert wallet.label_for("bola.settle") == "Bola winnings"
    assert wallet.label_for("bola.refund") == "Bola refund"
    assert wallet.label_for("bola.stake.reversal") == "Reversal · Bola bet placed"


def test_a_bola_day_reads_in_plain_words_in_the_ledger(bw):  # noqa: F811
    """Bet, cancel and win each leave a ledger movement whose label is human, not a code."""
    w = bw
    _bet(w, [7, 9], BolaKind.PAIR, 700, 0)
    gone = _bet(w, [1, 2], BolaKind.PAIR, 500, 0)
    bola.cancel_bola_ticket(w.db, w.admin, ticket_id=gone.id, reason="typo at the desk",
                            settings=w.store)
    _declare(w, 7, 9)
    labels = _ledger_labels_for_player(w)
    assert {"Bola bet placed", "Bola winnings", "Reversal · Bola bet placed"} <= labels, labels
    assert not any("." in label for label in labels)               # no raw "bola.stake" left


def _ledger_labels_for_player(w) -> set[str]:
    """Labels of every movement on the player's wallet, as the money history shows them."""
    from app.db.models import LedgerEntry, LedgerTxn
    acct = accounts.wallet_for_player(w.db, w.player)
    kinds = w.db.scalars(select(LedgerTxn.kind).join(LedgerEntry, LedgerTxn.id == LedgerEntry.txn_id)
                         .where(LedgerEntry.account_id == acct.id)).all()
    return {wallet.label_for(k) for k in kinds}


# --------------------------------------------------------------- statement (service level)
def test_agent_statement_counts_sales_cancels_and_commission(bw):  # noqa: F811
    w = bw
    _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)                     # PHP 100, loses
    _bet(w, [5, 6], BolaKind.PAIR, 700, 0)                         # PHP 7, loses
    gone = _bet(w, [3, 4], BolaKind.PAIR, 2500, 0)                 # PHP 25, cancelled
    bola.cancel_bola_ticket(w.db, w.admin, ticket_id=gone.id, reason="typo at the desk",
                            settings=w.store)
    _declare(w, 30, 31)
    day = w.draw.draw_date
    stmt = reports.bola_agent_statement(w.db, w.admin, start=day, end=day)
    by_name = {r["name"]: r for r in stmt["rows"]}
    kub = by_name[w.kub.username]
    assert (kub["tickets"], kub["staked"]) == (2, 10_700)
    assert (kub["canceled"], kub["canceled_cents"]) == (1, 2_500)
    assert (kub["winners"], kub["paid"]) == (0, 0)
    assert kub["commission"] > 0 and kub["unclaimed"] == kub["commission"]
    assert w.cavo.username in by_name and by_name[w.cavo.username]["commission"] > 0
    assert w.master.username not in by_name                        # D4 default: no Master line
    assert stmt["totals"]["staked"] == sum(r["staked"] for r in stmt["rows"])


def test_statement_shows_winnings_paid_and_no_commission_on_a_losing_draw(bw):  # noqa: F811
    """A PHP 7 winner costs the house PHP 5,600, so the draw pays no commission (plan 3.8)."""
    w = bw
    win = _bet(w, [7, 9], BolaKind.PAIR, 700, 0)
    _declare(w, 7, 9)
    w.db.refresh(win)
    assert win.payout_cents == 560_000
    day = w.draw.draw_date
    row = reports.bola_agent_statement(w.db, w.admin, start=day, end=day, agent_id=w.kub.id)["rows"][0]
    assert (row["tickets"], row["winners"], row["paid"]) == (1, 1, 560_000)
    assert row["commission"] == 0


def test_statement_follows_the_claim_status_and_the_date_range(bw):  # noqa: F811
    w = bw
    _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    _declare(w, 30, 31)
    day = w.draw.draw_date
    assert commissions.claim_to_wallet(w.db, w.cavo) is not None
    row = {r["name"]: r for r in reports.bola_agent_statement(
        w.db, w.admin, start=day, end=day)["rows"]}[w.cavo.username]
    assert row["claimed"] > 0 and row["unclaimed"] == 0
    assert row["claimed"] + row["unclaimed"] == row["commission"]
    later = day + dt.timedelta(days=3000)
    assert reports.bola_agent_statement(w.db, w.admin, start=later, end=later)["rows"] == []


def test_statement_scope_each_agent_sees_only_their_own_network(bw):  # noqa: F811
    w = bw
    rival = agents.create_agent(w.db, w.master, username=f"br{w.tag}", password=PASSWORD,
                                role=Role.AGENT, full_name="Rival", settings=w.store)
    _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    _declare(w, 30, 31)
    day = w.draw.draw_date

    def names(actor):
        return {r["name"] for r in reports.bola_agent_statement(w.db, actor, start=day, end=day)["rows"]}

    assert names(w.kub) == {w.kub.username}                         # a Kubrador sees only itself
    assert names(w.cavo) == {w.cavo.username, w.kub.username}       # a Cavo sees its downline
    assert names(rival) == set()                                    # a sibling sees none of it
    assert {w.kub.username, w.cavo.username} <= names(w.admin)
    with pytest.raises(Forbidden):                                  # nor can it ask for one by id
        reports.bola_agent_draws(w.db, rival, w.kub.id, start=day, end=day)
    rows = reports.bola_agent_draws(w.db, w.cavo, w.kub.id, start=day, end=day)
    assert len(rows) == 1 and rows[0]["draw"].id == w.draw.id and rows[0]["staked"] == 10_000


def test_legacy_tickets_are_not_in_statements(bw):  # noqa: F811
    w = bw
    t = _bet(w, [1, 2], BolaKind.PAIR, 5000, 5000)
    t.legacy = True
    w.db.flush()
    day = w.draw.draw_date
    assert reports.bola_agent_statement(w.db, w.admin, start=day, end=day,
                                        agent_id=w.kub.id)["rows"] == []


# --------------------------------------------------------------- pages (HTTP, gate lifted)
@pytest.fixture()
def net(monkeypatch):
    """Committed network: Master > Cavo > Kubrador (+ a rival Cavo and a Declarator), one draw
    with a lost ticket and a cancelled ticket. Roles other than Admin need the dev gate lifted."""
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", False)
    from app.core.settings_registry import SettingsStore
    from app.db.models import User
    from app.db.session import session_scope
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        store.set("bola.enabled", "true", admin.id)
        mk = lambda parent, name, role: agents.create_agent(            # noqa: E731
            db, parent, username=f"{name}{tag}", password=PASSWORD, role=role, full_name=name,
            settings=store)
        master = mk(admin, "nm", Role.MASTER)
        cavo = mk(master, "nc", Role.AGENT)
        rival = mk(master, "nr", Role.AGENT)
        kub = mk(cavo, "nk", Role.SUB_AGENT)
        decl = mk(admin, "nd", Role.DECLARATOR)
        for u in (master, cavo, rival, kub, decl):
            u.must_change_password = False
        player = players.register_online_player(
            db, kub, username=f"np{tag}", password=PASSWORD, full_name="Net Player",
            birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store, agent_id=kub.id)
        player.kyc_verified = True
        load_credit(db, admin, store, player, 1_000_000)
        loc = locations.create_location(db, admin, name=f"Net Area {tag}")
        draw = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date.today(), slot=1,
                                settings=store, status=DrawStatus.OPEN,
                                scheduled_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2))
        place = lambda n, t: bola.place_bola_bet(                       # noqa: E731
            db, kub, draw_id=draw.id, kind=BolaKind.PAIR, numbers=n, tumbok_cents=t, sahod_cents=0,
            player_id=player.id, settings=store)
        place([1, 2], 5000)
        gone = place([3, 4], 2500)
        bola.cancel_bola_ticket(db, admin, ticket_id=gone.id, reason="typo at the desk", settings=store)
        ids = dict(tag=tag, loc=loc.id, draw=draw.id, kub=kub.id, cavo=cavo.id, gone=gone.code,
                   names={"master": master.username, "cavo": cavo.username, "rival": rival.username,
                          "kub": kub.username, "decl": decl.username})
    yield ids
    with session_scope() as db:
        from app.db.models import Location
        db.get(Location, ids["loc"]).is_active = False


def test_cancelled_page_lists_the_cancel_with_who_and_why(net):
    c, _ = login("admin")
    page = c.get("/bola/cancelled")
    assert page.status_code == 200
    assert net["gone"] in page.text and "typo at the desk" in page.text
    row = page.text.split(net["gone"], 1)[1].split("</tr>", 1)[0]
    assert "System" in row and "admin" not in row.lower()          # the hidden Admin is masked
    assert net["gone"] in c.get("/bola/cancelled?q=" + net["gone"]).text
    assert net["gone"] not in c.get("/bola/cancelled?q=zzzz-no-such").text


def test_cancelled_page_is_dev_only(net):
    """Bola Cancelled (and Cancel Ticket) are for the Admin (dev) account only."""
    for who in ("kub", "cavo", "master", "rival"):
        c, r = login(net["names"][who])
        assert r.status_code == 303
        assert c.get("/bola/cancelled").status_code == 403, who
        assert c.get("/cancelT").status_code == 403, who
        assert 'href="/bola/cancelled"' not in c.get("/dashboard").text, who


def test_agent_statement_pages_scope_and_csv(net):
    day = dt.date.today().isoformat()
    q = f"?start={day}&end={day}&game=bola"
    kub, cavo, rival = net["names"]["kub"], net["names"]["cavo"], net["names"]["rival"]   # Sub Agent, Agent, rival

    a, _ = login("admin")
    page = a.get("/commissions" + q)
    assert page.status_code == 200 and kub in page.text and cavo in page.text
    csv_resp = a.get("/commissions.csv" + q)
    assert csv_resp.status_code == 200 and csv_resp.headers["content-type"].startswith("text/csv")
    assert kub in csv_resp.text and "Total" in csv_resp.text
    assert a.get(f"/commissions/{net['kub']}" + q).status_code == 200
    # the old Bola-only paths now redirect to the merged pages
    old = a.get("/bola/agents" + q)
    assert old.status_code == 303 and old.headers["location"].startswith("/commissions?")
    assert a.get(f"/bola/agents/{net['kub']}" + q).headers["location"].startswith(f"/commissions/{net['kub']}?")
    assert a.get("/bola/agents.csv" + q).headers["location"].startswith("/commissions.csv?")

    k, _ = login(kub)
    page = k.get("/commissions" + q).text
    assert kub in page
    assert f'/commissions/{net["cavo"]}?' not in page      # the Agent above is only named as upline, never a row
    assert k.get(f"/commissions/{net['cavo']}" + q).status_code == 403   # not below a Sub Agent

    r, _ = login(rival)
    assert kub not in r.get("/commissions" + q).text
    assert r.get(f"/commissions/{net['kub']}" + q).status_code == 403

    c, _ = login(cavo)
    assert c.get(f"/commissions/{net['kub']}" + q).status_code == 200
    assert kub in c.get("/commissions" + q).text      # an Agent sees the Sub Agent's commission lines too


def test_declarator_cannot_see_statements_or_cancelled_tickets(net):
    d, r = login(net["names"]["decl"])
    assert r.status_code == 303
    for path in ("/bola/agents", "/bola/agents.csv", "/commissions", "/commissions.csv", "/bola/cancelled", "/bola/betlogs"):
        assert d.get(path).status_code == 403, path


def test_bad_dates_fall_back_to_today_and_ranges_are_capped(net):
    a, _ = login("admin")
    assert a.get("/commissions?start=not-a-date&end=also-bad").status_code == 200
    swapped = a.get("/commissions?start=2030-02-01&end=2030-01-01")
    assert swapped.status_code == 200 and "2030-01-01" in swapped.text
    wide = a.get("/commissions?start=1990-01-01&end=2030-01-01")
    assert wide.status_code == 200 and "1990-01-01" not in wide.text


def test_sidebar_links_resolve(net):
    """The nav entries for these two pages shipped before the pages did; keep them honest."""
    a, _ = login("admin")
    html = a.get("/dashboard").text
    for href in ("/bola/cancelled", "/commissions"):
        assert f'href="{href}"' in html
        assert a.get(href).status_code == 200
