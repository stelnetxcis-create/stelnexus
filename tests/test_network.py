"""Master Agent / Agent / Sub Agent network: roles, invites, the cut formula, hidden admin, migration."""
import datetime as dt
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app.core.errors import Forbidden, ValidationError
from app.core.rbac import Perm, permissions_for
from app.db.models import Commission, Result, Role, Side, Status, User
from app.main import create_app
from app.services import accounts, agents, betting, commissions, players, referrals, reports, settlement
from app.services.commissions import split_pool
from tests.conftest import PASSWORD, load_credit

app = create_app()


# ---------------------------------------------------------------- the formula
@pytest.mark.parametrize("pool", [0, 1, 7, 999, 100_000, 123_457])
@pytest.mark.parametrize("agent,sop", [(3000, 3000), (0, 5000), (10000, 10000), (2750, 3333), (3000, 0)])
def test_split_always_sums_to_pool(pool, agent, sop):
    s = split_pool(pool, agent_bps=agent, sub_bps=sop)
    assert s.paid == pool and min(s.master, s.agent, s.sub_agent) >= 0


def test_default_split_is_70_30_with_sub_agent_30_percent_of_the_agents_30():
    s = split_pool(100_000, agent_bps=3000, sub_bps=3000)
    assert (s.master, s.agent, s.sub_agent, s.agent_gross) == (70_000, 21_000, 9_000, 30_000)


def test_missing_or_suspended_links_roll_up():
    assert split_pool(1000, agent_bps=3000, sub_bps=3000, has_sub=False).sub_agent == 0
    # no Sub Agent: the Agent keeps the whole Agent cut
    assert split_pool(1000, agent_bps=3000, sub_bps=3000, has_sub=False).agent == 300
    only_master = split_pool(1000, agent_bps=3000, sub_bps=3000, has_agent=False, has_sub=False)
    assert only_master.master == 1000
    no_master = split_pool(1000, agent_bps=3000, sub_bps=3000, has_master=False)
    assert no_master.master == 0 and no_master.paid == 300      # house keeps the Master share


# ------------------------------------------------------------------ the chain
@pytest.fixture()
def chain(world):
    db, admin, store = world.db, world.admin, world.store
    tag = world.tag
    master = agents.create_agent(db, admin, username=f"m{tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="M", settings=store)
    cavo = agents.create_agent(db, master, username=f"c{tag}", password=PASSWORD, role=Role.AGENT,
                               full_name="C", settings=store)       # w.cavo = the Agent
    kub = agents.create_agent(db, cavo, username=f"k{tag}", password=PASSWORD, role=Role.SUB_AGENT,
                              full_name="K", settings=store)        # w.kub = the Sub Agent
    player = players.register_online_player(
        db, kub, username=f"pk{tag}", password=PASSWORD, full_name="P", birth_date=dt.date(1990, 1, 1),
        gcash_number="09171234567", settings=store, agent_id=kub.id)
    player.kyc_verified = True
    load_credit(db, admin, store, player, 100_000)
    for k, v in dict(master=master, cavo=cavo, kub=kub, player=player).items():
        setattr(world, k, v)
    return world


def _bet(w, side, cents):
    return betting.place_bet(w.db, w.kub, fight_id=w.games[0].id, side=side, stake_cents=cents,
                             settings=w.store, player_id=w.player.id)


def test_end_to_end_commission_split_and_audit(chain):
    w = chain
    _bet(w, Side.MERON, 10000); _bet(w, Side.WALA, 10000)
    t = w.db.scalar(select(betting.Ticket).where(betting.Ticket.agent_id == w.kub.id))
    assert (t.kubrador, t.cavo) == (w.kub.username, w.cavo.username)      # Betting List: Sub Agent / Agent
    run = settlement.declare(w.db, w.admin, fight_id=w.games[0].id, result=Result.MERON, settings=w.store)
    rows = list(w.db.scalars(select(Commission).where(Commission.run_id == run.id)))
    by = {}
    for r in rows:
        by[r.tier] = by.get(r.tier, 0) + r.amount_cents
    pool = run.totals["house_take"]                       # 5% tong of 20,000 = 1,000
    assert pool == 1000 and sum(by.values()) == pool
    assert by["master"] == 700 and by["agent"] == 210 and by["sub_agent"] == 90
    assert all(r.calc and r.calc["volume_bps"] == 3000 for r in rows)
    assert {r.agent_id for r in rows} == {w.master.id, w.cavo.id, w.kub.id}
    assert reports.integrity(w.db)["ok"]


def _pawn(w, grants):
    p = agents.create_agent(w.db, w.admin, username="pw" + w.tag, password=PASSWORD, role=Role.PAWN,
                            full_name="Pawn", settings=w.store)
    agents.set_pawn_grants(w.db, w.admin, p.id, grants)
    return p


def test_master_cannot_change_cut_pawn_with_grant_is_limited_admin_is_not(chain):
    w = chain
    with pytest.raises(Forbidden):
        commissions.set_cut(w.db, w.master, w.cavo, volume="40", settings=w.store)   # Master: no cuts any more
    pawn = _pawn(w, ["cuts:edit"])
    commissions.set_cut(w.db, pawn, w.cavo, volume="40", sop="25", settings=w.store)
    assert (w.cavo.cut_bps, w.cavo.sop_bps) == (4000, 2500)
    with pytest.raises(ValidationError):
        commissions.set_cut(w.db, pawn, w.cavo, volume="80", settings=w.store)       # over the guard rail
    commissions.set_cut(w.db, w.admin, w.cavo, volume="80", settings=w.store)        # Admin is not limited
    assert w.cavo.cut_bps == 8000
    commissions.set_cut(w.db, w.admin, w.cavo, volume="", sop="", settings=w.store)  # reset to default
    assert w.cavo.cut_bps is None and w.cavo.sop_bps is None
    with pytest.raises(Forbidden):
        commissions.set_cut(w.db, w.cavo, w.kub, sop="10", settings=w.store)          # Agent cannot
    no_grant = _pawn_without(w)
    with pytest.raises(Forbidden):
        commissions.set_cut(w.db, no_grant, w.cavo, volume="10", settings=w.store)
    from app.db.models import AuditLog
    assert w.db.scalar(select(AuditLog.id).where(AuditLog.action == "cut.update"))


def _pawn_without(w):
    return agents.create_agent(w.db, w.admin, username="pn" + w.tag, password=PASSWORD, role=Role.PAWN,
                               full_name="Pawn", settings=w.store)


def test_per_kubrador_override_beats_cavo_sop(chain):
    w = chain
    commissions.set_cut(w.db, w.admin, w.cavo, sop="10", settings=w.store)
    assert commissions.effective_sub_bps(w.kub, w.cavo, w.store) == 1000
    commissions.set_cut(w.db, w.admin, w.kub, sop="30", settings=w.store)
    assert commissions.effective_sub_bps(w.kub, w.cavo, w.store) == 3000


# ---------------------------------------------------------- invites / roles
def test_invite_role_is_fixed_by_issuer(chain):
    w = chain
    assert referrals.create_code(w.db, w.master, ttl_minutes=60).role == "agent"
    assert referrals.create_code(w.db, w.cavo, ttl_minutes=60).role == "sub_agent"     # default; "player" also allowed
    assert referrals.create_code(w.db, w.cavo, ttl_minutes=60, role="player").role == "player"
    assert referrals.create_code(w.db, w.kub, ttl_minutes=60).role == "player"
    with pytest.raises(Forbidden):                         # Cavo / Kubrador no longer exist as codes
        referrals.create_code(w.db, w.master, ttl_minutes=60, role="cavo")


def test_agent_code_creates_sub_agent_and_sub_agent_code_creates_player(chain):
    w = chain
    code = referrals.create_code(w.db, w.cavo, ttl_minutes=60)
    new = referrals.redeem(w.db, code.code, username=f"nk{w.tag}", password=PASSWORD, full_name="NK",
                           contact=None, settings=w.store)
    assert new.role == Role.SUB_AGENT and new.parent_id == w.cavo.id
    code = referrals.create_code(w.db, w.kub, ttl_minutes=60)
    p = referrals.redeem(w.db, code.code, username=f"pl{w.tag}", password=PASSWORD, full_name="Pl",
                         contact=None, settings=w.store, birth_date=dt.date(1995, 5, 5),
                         gcash_number="09171234567")
    assert p.agent_id == w.kub.id
    with pytest.raises(ValidationError):                      # single use
        referrals.redeem(w.db, code.code, username="x" + w.tag, password=PASSWORD, full_name="X",
                         contact=None, settings=w.store, birth_date=dt.date(1995, 5, 5),
                         gcash_number="09171234567")


def test_role_creation_rules(chain):
    w = chain
    with pytest.raises(Forbidden):
        agents.create_agent(w.db, w.master, username="a" + w.tag, password=PASSWORD, role=Role.ADMIN,
                            full_name="A", settings=w.store)
    with pytest.raises(Forbidden):                      # a Master Agent has no second Master Agent
        agents.create_agent(w.db, w.master, username="b" + w.tag, password=PASSWORD, role=Role.MASTER,
                            full_name="B", settings=w.store)
    with pytest.raises(Forbidden):                      # no Sub Agent straight under a Master Agent
        agents.create_agent(w.db, w.master, username="d" + w.tag, password=PASSWORD, role=Role.SUB_AGENT,
                            full_name="D", settings=w.store)
    with pytest.raises(Forbidden):                      # an Agent recruits Sub Agents only, not Agents
        agents.create_agent(w.db, w.cavo, username="c" + w.tag, password=PASSWORD, role=Role.AGENT,
                            full_name="C", settings=w.store)
    with pytest.raises(Forbidden):                      # the retired tiers cannot be created at all
        agents.create_agent(w.db, w.admin, username="e" + w.tag, password=PASSWORD, role=Role.CAVO,
                            full_name="E", settings=w.store)


# ------------------------------------------------------------- hidden layer
def test_permissions_master_is_admin_minus_admin_panel(monkeypatch):
    # The number game is dev-only by default (Master holds no Bola perm at all); lift that gate
    # here so this test checks the role matrix, not the gate. The gate has its own tests.
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", False)
    admin_only = {Perm.SETTINGS_VIEW, Perm.SETTINGS_MANAGE, Perm.AUDIT_VIEW, Perm.HIDDEN_VIEW,
                  Perm.CUTS_VIEW, Perm.CUTS_MANAGE, Perm.PLAYERS_VIEW, Perm.PLAYERS_CREATE,
                  Perm.PLAYERS_EDIT, Perm.ONLINE_VIEW, Perm.PANEL_VIEW, Perm.PAWNS_MANAGE,
                  Perm.LOCATIONS_VIEW, Perm.LOCATIONS_MANAGE, Perm.TEAMS_VIEW, Perm.TEAMS_MANAGE,
                  # house money is dev-only
                  Perm.CASHFLOW_VIEW, Perm.REPORTS_VIEW,
                  # livestream chat: Admin, and Pawns it was granted to
                  Perm.CHAT_USE,
                  # account tools, IP bans
                  Perm.ACCOUNTS_VIEW, Perm.ACCOUNTS_EDIT, Perm.ACCOUNTS_REMOVE, Perm.IPBANS_MANAGE,
                  Perm.CANCEL_LOG,
                  # Staff Panel: each section opens for its own role, the Admin and granted Pawns
                  Perm.STAFF_VIEW, Perm.STAFF_TELLER, Perm.STAFF_DECLARATOR,
                  # Master never enters or changes a result
                  Perm.FIGHTS_DECLARE, Perm.FIGHTS_REDECLARE,
                  Perm.BOLA_DECLARE, Perm.BOLA_REDECLARE,
                  # approving / rejecting belongs to designated staff (Teller, Admin), never Master
                  Perm.REQUESTS_REVIEW, Perm.FIGHTS_APPROVE, Perm.BOLA_APPROVE}
    assert permissions_for(Role.ADMIN) - permissions_for(Role.MASTER) == admin_only
    assert Perm.EVENTS_CREATE in permissions_for(Role.MASTER)
    for role in (Role.AGENT, Role.SUB_AGENT, Role.DECLARATOR, Role.PAWN):
        assert Perm.CUTS_VIEW not in permissions_for(role)
    for role in (Role.DECLARATOR, Role.PAWN):
        assert Perm.EVENTS_CREATE not in permissions_for(role)
    for role in (Role.AGENT, Role.SUB_AGENT):      # agents host events but never run or approve them
        assert Perm.EVENTS_CREATE in permissions_for(role) and Perm.EVENTS_MANAGE not in permissions_for(role)
        assert Perm.AGENTS_CREATE not in permissions_for(role) and Perm.REQUESTS_REVIEW not in permissions_for(role)
    for role in (Role.AGENT, Role.SUB_AGENT):
        assert Perm.AGENTS_VIEW not in permissions_for(role) and Perm.PLAYERS_VIEW not in permissions_for(role)
        assert Perm.WALLET_TRANSFER in permissions_for(role)
    for role in (Role.DECLARATOR, Role.PAWN):
        assert not {Perm.WALLET_VIEW, Perm.WALLET_TRANSFER, Perm.REQUESTS_CREATE, Perm.BETS_PLACE} & permissions_for(role)


def test_admin_is_invisible_to_master(chain):
    w = chain
    listed = agents.list_agents(w.db, w.master)
    assert all(u.role != Role.ADMIN for u in listed)
    with pytest.raises(Forbidden):
        agents.get_agent(w.db, w.master, w.admin.id)
    with pytest.raises(Forbidden):
        agents.reset_password(w.db, w.master, w.admin.id, "Another-Passw0rd-2026!")
    assert any(u.role == Role.MASTER for u in agents.list_agents(w.db, w.master))      # sanity: list not empty
    assert agents.get_agent(w.db, w.admin, w.master.id)                                # admin sees master
    tree_ids = {n["user"].id for n in agents.tree(w.db, w.master)}
    assert w.admin.id not in tree_ids


def _login(username):
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    c.get("/login")
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token})
    assert r.status_code == 303
    return c


def test_web_master_sees_ops_not_hidden_pages(chain):
    w = chain
    w.master.must_change_password = False
    w.db.flush(); w.db.commit()
    c = _login(w.master.username)
    for path in ["/dashboard", "/agents", "/referrals", "/commissions", "/change_name", "/wallet",
                 "/wallet?t=approvals", "/events"]:
        assert c.get(path).status_code == 200, path
    # house money (cashflow, report archives) is dev-only now
    for path in ["/cashflow", "/archives", "/settings", "/audit", "/integrity", "/cuts", "/admin", "/admin/variables", "/admin/locations",
                 "/admin/cuts", "/admin/pawns", "/admin/audit", "/clp", "/clp/online"]:
        assert c.get(path).status_code == 403, path
    main = c.get("/agents").text.split("<main")[1].split("</main>")[0]
    assert '<td class="mono">admin</td>' not in main and w.cavo.username in main
    assert c.post(f"/agents/{w.admin.id}/update", data={"full_name": "x"}).status_code == 403
    assert c.get("/chat").status_code == 403            # livestream chat is not for Master
    assert 'href="/chat"' not in c.get("/dashboard").text


def test_web_admin_sees_everything_and_dev_badge():
    c = _login("admin")
    for path in ["/admin", "/admin/variables", "/admin/locations", "/admin/cuts", "/admin/pawns", "/admin/audit",
                 "/admin/integrity", "/commissions", "/clp", "/clp/online", "/wallet?t=approvals"]:
        assert c.get(path).status_code == 200, path
    for old in ["/settings", "/audit", "/integrity", "/cuts", "/cashin_out"]:
        assert c.get(old).status_code == 303, old
    assert ">DEV<" in c.get("/dashboard").text


def test_web_agent_and_sub_agent_cannot_reach_cuts_or_hidden(chain):
    w = chain
    for u in (w.cavo, w.kub):
        u.must_change_password = False
    w.db.flush(); w.db.commit()
    for u in (w.cavo, w.kub):
        c = _login(u.username)
        for path in ["/cuts", "/admin/cuts", "/settings", "/admin", "/audit", "/agents", "/clp", "/clp/online"]:
            assert c.get(path).status_code == 403, (u.username, path)
        assert c.get("/wallet").status_code == 200
        assert c.post("/cuts/%d" % w.kub.id, data={"sop": "99"}).status_code in (403, 400)
    assert c.get("/referrals").status_code == 200


def test_register_page_asks_player_fields_only_for_player_codes(chain):
    w = chain
    code = referrals.create_code(w.db, w.kub, ttl_minutes=60); w.db.commit()
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    c.get("/register")
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post("/register/code", data={"code": code.code, "csrf_token": token})
    assert "gcash_number" in r.text and "Player" in r.text


# ---------------------------------------------------------------- migration
LEGACY = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY, uuid VARCHAR(32), created_at DATETIME, updated_at DATETIME,
  username VARCHAR(32) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL,
  full_name VARCHAR(120) NOT NULL, contact VARCHAR(32), parent_id INTEGER, ref_code VARCHAR(16) NOT NULL UNIQUE,
  status VARCHAR(16), must_change_password BOOLEAN, failed_attempts INTEGER, locked_until DATETIME,
  password_changed_at DATETIME, last_login_at DATETIME, last_login_ip VARCHAR(64), created_by_id INTEGER,
  CONSTRAINT ck_users_role_valid CHECK (role in ('admin','master','gold','sub','teller','declarator')),
  CONSTRAINT ck_users_no_self_parent CHECK (id <> parent_id));
CREATE TABLE referral_codes (id INTEGER PRIMARY KEY, code VARCHAR(16), role VARCHAR(16), issuer_id INTEGER,
  note VARCHAR(120), created_at DATETIME, expires_at DATETIME, used_at DATETIME, used_by_id INTEGER, revoked_at DATETIME);
"""


def test_old_database_is_upgraded(tmp_path):
    """A database shaped like the previous release (gold/sub roles, old CHECK, no cut columns)."""
    from app.db import migrate
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        for stmt in LEGACY.split(";"):
            if stmt.strip():
                c.exec_driver_sql(stmt)
        for i, role in enumerate(("admin", "master", "gold", "sub", "teller"), start=1):
            c.exec_driver_sql(f"INSERT INTO users (id,username,password_hash,role,full_name,ref_code) "
                              f"VALUES ({i},'u{i}','x','{role}','n','R{i}')")
        c.exec_driver_sql("INSERT INTO referral_codes (id,code,role,issuer_id) VALUES (1,'ABC','gold',1)")
    migrate.upgrade(eng)
    with eng.begin() as c:
        assert dict(c.execute(text("SELECT username, role FROM users")).all()) == {
            "u1": "admin", "u2": "master", "u3": "agent", "u4": "sub_agent", "u5": "teller"}
        assert c.execute(text("SELECT status FROM users WHERE username='u5'")).scalar() == "closed"   # teller retired
        assert c.execute(text("SELECT status FROM users WHERE username='u2'")).scalar() != "closed"
        assert c.execute(text("SELECT role FROM referral_codes")).scalar() == "agent"
        assert {"cut_bps", "sop_bps"} <= {r[1] for r in c.execute(text("PRAGMA table_info(users)"))}
        c.exec_driver_sql("UPDATE users SET role='sub_agent' WHERE id=3")     # new CHECK accepts it
        with pytest.raises(Exception):
            c.exec_driver_sql("UPDATE users SET role='gold' WHERE id=3")      # old value now refused
    migrate.upgrade(eng)                                                      # idempotent


def test_admin_name_is_masked_in_chat_and_who(chain):
    from app.db.models import ChatMessage
    w = chain
    w.db.add(ChatMessage(user_id=w.admin.id, body="hello from the hidden one")); w.db.commit()
    pawn = agents.create_agent(w.db, w.admin, username=f"cp{w.tag}", password=PASSWORD, role=Role.PAWN,
                               full_name="CP", settings=w.store)
    agents.set_pawn_grants(w.db, w.admin, pawn.id, ["chat"])
    pawn.must_change_password = False; w.db.commit()
    c = _login(pawn.username)                            # a Pawn with the chat grant sees the Admin as System
    msgs = c.get("/chat/messages?after=0", headers={"Accept": "application/json"}).json()["messages"]
    mine = [m for m in msgs if m["body"] == "hello from the hidden one"][0]
    assert mine["user"] == "System" and mine["role"] == "Management"
    a = _login("admin")
    msgs = a.get("/chat/messages?after=0", headers={"Accept": "application/json"}).json()["messages"]
    assert [m for m in msgs if m["body"] == "hello from the hidden one"][0]["user"] == "admin"
