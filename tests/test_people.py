"""Staff Panel -> People: one directory for the team, the players and who is online, and who sees what in it."""
import datetime as dt

from app.core.rbac import Perm, permissions_for
from app.db.models import Role
from app.services import agents, presence
from tests.conftest import PASSWORD
from tests.test_network import chain  # noqa: F401  (fixture)
from tests.test_teller_declarator import _mk
from tests.test_web import login

TELLER_GCASH = "09171230000"


def _page(c, path):
    r = c.get(path)
    return r.status_code, r.text.split("<main")[1].split("</main>")[0] if "<main" in r.text else r.text


def test_teller_holds_the_online_right_and_nothing_wider():
    perms = permissions_for(Role.TELLER)
    assert Perm.ONLINE_VIEW in perms and Perm.AGENTS_VIEW in perms
    assert Perm.PLAYERS_VIEW not in perms and Perm.ACCOUNTS_VIEW not in perms and Perm.ACCOUNTS_EDIT not in perms


def test_teller_sees_who_is_online_with_gcash_and_no_device_or_ip(world):
    db, admin, store = world.db, world.admin, world.store
    teller = _mk(db, admin, store, Role.TELLER, "tp", gcash_number=TELLER_GCASH)
    other = _mk(db, admin, store, Role.TELLER, "to", gcash_number="09175550000")
    db.commit()
    login(other.username)                                   # somebody else is signed in
    c, r = login(teller.username)
    assert r.status_code == 303
    for path in ("/staff/people", "/clp/online", "/agents"):    # the old addresses land on the one tab a Teller has
        code, body = _page(c, path)
        assert code == 200, path
        assert other.username in body and "09175550000" in body, path
        assert "Device" not in body and "Sessions" not in body and "Create user" not in body, path
    assert c.get("/clp").status_code in (403, 404)               # no player list
    assert c.get("/staff/people?tab=team").status_code == 200    # asking for a tab you lack opens your own


def test_admin_sees_every_tab_and_the_device_columns(world):
    db, admin, store = world.db, world.admin, world.store
    teller = _mk(db, admin, store, Role.TELLER, "ta", gcash_number=TELLER_GCASH)
    db.commit()
    login(teller.username)
    c, _ = login("admin")
    code, body = _page(c, "/staff/people?tab=online")
    assert code == 200 and "Device" in body and TELLER_GCASH in body
    for tab in ("team", "players"):
        assert c.get(f"/staff/people?tab={tab}").status_code == 200
    staff_home = c.get("/staff").text
    assert "/staff/people" in staff_home                     # People is a tab of the Staff Panel
    nav = c.get("/dashboard").text.split('<nav class="nav">')[1].split("</nav>")[0]
    assert 'href="/agents"' not in nav and 'href="/clp"' not in nav     # the old Network links are gone


def test_master_gets_the_team_only_and_no_sidebar_staff_panel(chain):
    w = chain
    w.master.must_change_password = False
    w.db.flush()
    w.db.commit()
    c, _ = login(w.master.username)
    code, body = _page(c, "/staff/people")
    assert code == 200 and w.cavo.username in body
    assert c.get("/clp").status_code == 403 and c.get("/clp/online").status_code == 403
    nav = c.get("/dashboard").text.split('<nav class="nav">')[1].split("</nav>")[0]
    assert 'href="/staff/people"' in nav and "Staff Panel" not in nav


def test_an_agent_has_no_people_page(chain):
    w = chain
    w.cavo.must_change_password = False
    w.db.flush()
    w.db.commit()
    c, _ = login(w.cavo.username)
    assert c.get("/staff/people").status_code == 403


def test_presence_still_refuses_a_master(chain):
    from app.core.errors import Forbidden
    import pytest
    with pytest.raises(Forbidden):
        presence.online_accounts(chain.db, chain.master, chain.store)


def test_accounts_old_address_opens_people_and_tools_live_in_the_row(world):
    c, _ = login("admin")
    code, body = _page(c, "/admin/accounts")
    assert code == 200 and "pp-list" in body
    code, body = _page(c, "/admin/accounts?kind=players")
    assert code == 200
    panel = c.get("/admin").text
    assert 'href="/admin/accounts"' not in panel                     # the Admin Panel tab is gone


def test_branches_and_tiers():
    from app.core.branches import USERS, STAFF, DEV, branch_of, commission_eligible, describe, tier_of
    assert [branch_of(r) for r in ("player", "master", "declarator", "teller", "pawn", "bishop", "admin")] == \
        [USERS, USERS, STAFF, STAFF, DEV, DEV, DEV]
    assert tier_of("player") == 1 and tier_of("sub_agent") == 2 and tier_of("agent") == 3 and tier_of("master") == 4
    assert commission_eligible("sub_agent") and not commission_eligible("player") and not commission_eligible("teller")
    assert describe(Role.AGENT)["branch_label"] == "Users"


def test_people_pages_show_the_branch(world):
    c, _ = login("admin")
    for tab in ("team", "players", "online"):
        code, body = _page(c, f"/staff/people?tab={tab}")
        assert code == 200
    assert "Users" in _page(c, "/staff/people?tab=team")[1]
