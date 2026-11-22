"""Cavo + Kubrador merged into Agent + Sub Agent; one statement/commission view for both games.

Pinned here:
* default split Master 70 / Agent 30 / Sub Agent 30% of the Agent's 30, in BOTH games;
* each game has its own default cuts, and a cut on one account beats them for every game;
* a player directly under an Agent leaves the whole Agent cut with the Agent;
* the merged statement + commission list shows an account and everything below it, never the rest;
* an old database's Cavo / Kubrador rows become Agent / Sub Agent.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select, text

from app.core.errors import ValidationError
from app.db.models import BolaCommission, BolaKind, Commission, DrawStatus, Result, Role, Side
from app.services import (
    agents, betting, bola, bola_settlement, commissions, ledger, locations, players, reports, settlement,
)
from tests.conftest import PASSWORD, fund_user, load_credit


@pytest.fixture()
def tree(world):
    """Master -> Agent -> Sub Agent -> player, plus a player straight under the Agent, plus a rival
    Agent (with its own Sub Agent) under the same Master."""
    db, admin, store, tag = world.db, world.admin, world.store, world.tag
    mk = lambda actor, role, name: agents.create_agent(     # noqa: E731
        db, actor, username=f"{name}{tag}", password=PASSWORD, role=role, full_name=name, settings=store)

    def player_under(owner, name):
        p = players.register_online_player(
            db, owner, username=f"{name}{tag}", password=PASSWORD, full_name=name,
            birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store, agent_id=owner.id)
        p.kyc_verified = True
        load_credit(db, admin, store, p, 1_000_000)
        return p

    master = mk(admin, Role.MASTER, "mm")
    agent = mk(master, Role.AGENT, "ag")
    sub = mk(agent, Role.SUB_AGENT, "sb")
    rival = mk(master, Role.AGENT, "rv")
    rival_sub = mk(rival, Role.SUB_AGENT, "rs")
    for k, v in dict(master=master, agent=agent, sub=sub, rival=rival, rival_sub=rival_sub,
                     p_sub=player_under(sub, "ps"), p_agent=player_under(agent, "pa"),
                     p_rival=player_under(rival_sub, "pr")).items():
        setattr(world, k, v)
    return world


def _tiers(rows):
    out = {}
    for r in rows:
        out[r.tier] = out.get(r.tier, 0) + r.amount_cents
    return out


def _sabong(w, seller, player, result=Result.MERON):
    for side in (Side.MERON, Side.WALA):
        betting.place_bet(w.db, seller, fight_id=w.games[0].id, side=side, stake_cents=10000,
                          settings=w.store, player_id=player.id)
    run = settlement.declare(w.db, w.admin, fight_id=w.games[0].id, result=result, settings=w.store)
    return list(w.db.scalars(select(Commission).where(Commission.run_id == run.id)))


# ---------------------------------------------------------------- the split
def test_sabong_default_is_70_21_9(tree):
    w = tree
    rows = _sabong(w, w.sub, w.p_sub)                   # tong 5% of 20,000 = pool 1,000
    assert _tiers(rows) == {"master": 700, "agent": 210, "sub_agent": 90}
    assert {r.agent_id for r in rows} == {w.master.id, w.agent.id, w.sub.id}
    assert ledger.verify_integrity(w.db)["ok"]


def test_player_straight_under_an_agent_leaves_the_whole_agent_cut_with_the_agent(tree):
    w = tree
    rows = _sabong(w, w.agent, w.p_agent)
    assert _tiers(rows) == {"master": 700, "agent": 300}          # no Sub Agent line


def test_each_game_has_its_own_defaults_and_an_account_cut_beats_both(tree):
    w = tree
    w.store.set("bola.commission.agent_cut_bps", "4000", w.admin.id)
    w.store.set("bola.commission.sub_agent_cut_bps", "5000", w.admin.id)
    try:
        assert commissions.effective_agent_bps(w.agent, w.store, "sabong") == 3000
        assert commissions.effective_agent_bps(w.agent, w.store, "bola") == 4000
        assert commissions.effective_sub_bps(w.sub, w.agent, w.store, "bola") == 5000
        commissions.set_cut(w.db, w.admin, w.agent, volume="35", sop="20", settings=w.store)
        for game in ("sabong", "bola"):
            assert commissions.effective_agent_bps(w.agent, w.store, game) == 3500
            assert commissions.effective_sub_bps(w.sub, w.agent, w.store, game) == 2000
    finally:
        w.store.set("bola.commission.agent_cut_bps", "3000", w.admin.id)
        w.store.set("bola.commission.sub_agent_cut_bps", "3000", w.admin.id)


def test_only_agent_and_sub_agent_have_a_cut(tree):
    w = tree
    with pytest.raises(ValidationError):
        commissions.set_cut(w.db, w.admin, w.master, volume="10", settings=w.store)
    commissions.set_cut(w.db, w.admin, w.sub, sop="40", settings=w.store)
    assert w.sub.cut_bps == 4000


# ---------------------------------------------------------------- Bola
@pytest.fixture()
def bola_tree(tree):
    w = tree
    w.store.set("bola.enabled", "true", w.admin.id)
    w.store.set("bola.commission.pay_master", "true", w.admin.id)
    fund_user(w.db, w.admin, w.sub, 1_000_000)            # the Sub Agent bets from its own credit
    loc = locations.create_location(w.db, w.admin, name=f"Area {w.tag}")
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    w.draw = bola.create_draw(w.db, w.admin, location_id=loc.id, draw_date=dt.date.today(), slot=1,
                              settings=w.store, scheduled_at=soon, status=DrawStatus.OPEN)
    yield w
    w.store.set("bola.commission.pay_master", "false", w.admin.id)
    loc.is_active = False
    w.db.flush()


def _bola_settle(w, bettor):
    bola.place_bola_bet(w.db, bettor, draw_id=w.draw.id, kind=BolaKind.PAIR, numbers=[1, 2],
                        tumbok_cents=5000, sahod_cents=5000, settings=w.store)
    bola_settlement.declare(w.db, w.admin, draw_id=w.draw.id, ball_first=30, ball_second=31,
                            settings=w.store, reason="test: declare before draw time")
    return list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))


def test_bola_uses_the_same_waterfall(bola_tree):
    w = bola_tree
    rows = _bola_settle(w, w.sub)
    pool = 10_000 * int(w.store.get("bola.commission.pool_bps")) // 10_000     # PHP 100 stake
    by = _tiers(rows)
    assert sum(by.values()) == pool
    assert by["master"] == pool - pool * 3000 // 10_000                       # 70% to the Master
    assert "sub_agent" not in by                                              # the bettor earns nothing on its own bet
    assert by["agent"] == pool * 3000 // 10_000                               # so the Agent keeps its whole 30%
    assert ledger.verify_integrity(w.db)["ok"]


# ---------------------------------------------------------------- merged statement + list
def test_merged_statement_and_lines_cover_the_whole_network_and_both_games(bola_tree):
    w = bola_tree
    _bola_settle(w, w.sub)
    day = dt.date.today()
    stmt = reports.agent_statement(w.db, w.agent, start=day, end=day)
    names = {r["name"] for r in stmt["rows"]}
    assert w.agent.username in names and w.sub.username in names           # itself AND below
    assert w.rival.username not in names and w.master.username not in names
    me = next(r for r in stmt["rows"] if r["me"])
    assert me["commission_bola"] > 0 and me["commission"] == me["commission_bola"] + me["commission_sabong"]
    assert stmt["totals"]["commission"] == sum(r["commission"] for r in stmt["rows"])

    lines = commissions.list_scope(w.db, w.agent, start=day, end=day)
    assert {l["earner"].id for l in lines} == {w.agent.id}                 # the bettor (Sub Agent) earns no line
    assert {l["game"] for l in lines} == {"bola"}
    mine = {w.master.id, w.agent.id}                        # the operator tier sees everything visible
    assert mine <= {l["earner"].id for l in commissions.list_scope(w.db, w.master, start=day, end=day)}
    assert commissions.list_scope(w.db, w.rival, start=day, end=day) == []  # nothing of another branch
    only_sabong = commissions.list_scope(w.db, w.master, game="sabong", start=day, end=day)
    assert not {l["earner"].id for l in only_sabong} & mine        # this tree has no sabong lines here


def test_sabong_and_bola_land_in_one_statement(bola_tree):
    w = bola_tree
    _sabong(w, w.sub, w.p_sub)
    # the Bola draw is declared in a second step so both games sit in the same statement
    _bola_settle(w, w.sub)
    day = dt.date.today()
    stmt = reports.agent_statement(w.db, w.master, start=day, end=day)
    row = next(r for r in stmt["rows"] if r["name"] == w.sub.username)
    assert row["commission_sabong"] == 90 and row["commission_bola"] == 0
    assert row["staked_sabong"] == 20_000 and row["staked_bola"] == 10_000
    games = reports.agent_games(w.db, w.master, w.sub.id, start=day, end=day)
    assert {g["game"] for g in games} == {"sabong", "bola"}
    assert reports.agent_statement(w.db, w.master, start=day, end=day, game="bola")["totals"][
        "commission_sabong"] == 0


# ---------------------------------------------------------------- migration
def test_old_cavo_and_kubrador_rows_become_agent_and_sub_agent(tmp_path):
    from app.db import migrate
    eng = create_engine(f"sqlite:///{tmp_path / 'tiers.db'}")
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, uuid VARCHAR(32), created_at DATETIME, updated_at DATETIME, "
            "username VARCHAR(32) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL, "
            "full_name VARCHAR(120) NOT NULL, contact VARCHAR(32), parent_id INTEGER, ref_code VARCHAR(16) NOT NULL UNIQUE, "
            "status VARCHAR(16), CONSTRAINT ck_users_role_valid CHECK (role in "
            "('admin','master','cavo','kubrador','teller','declarator','pawn','agent','sub_agent')))")
        c.exec_driver_sql("CREATE TABLE referral_codes (id INTEGER PRIMARY KEY, code VARCHAR(16), role VARCHAR(16), issuer_id INTEGER)")
        for i, role in enumerate(("master", "cavo", "kubrador"), start=1):
            c.exec_driver_sql(f"INSERT INTO users (id,username,password_hash,role,full_name,ref_code,parent_id) "
                              f"VALUES ({i},'u{i}','x','{role}','n','R{i}',{i - 1 if i > 1 else 'NULL'})")
        c.exec_driver_sql("INSERT INTO referral_codes (id,code,role,issuer_id) VALUES (1,'ABC','kubrador',2)")
    migrate.upgrade(eng)
    with eng.begin() as c:
        assert dict(c.execute(text("SELECT username, role FROM users")).all()) == {
            "u1": "master", "u2": "agent", "u3": "sub_agent"}
        assert c.execute(text("SELECT role FROM referral_codes")).scalar() == "sub_agent"
    assert migrate.upgrade(eng)["tiers"] == []                              # idempotent
