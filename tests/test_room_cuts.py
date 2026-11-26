"""Per-room commission cuts and the Agent Panel: who may set what, and how a room's cut reaches a settlement."""
import datetime as dt
from types import SimpleNamespace

import pytest

from app.core.errors import Forbidden, ValidationError
from app.core.rbac import ROLE_PERMS, Perm
from app.db.models import Role
from app.services import agents, commissions, locations
from tests.conftest import PASSWORD


@pytest.fixture()
def net(world):
    db, admin, store, tag = world.db, world.admin, world.store, world.tag
    master = agents.create_agent(db, admin, username=f"rm{tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="M", settings=store)
    agent = agents.create_agent(db, master, username=f"ra{tag}", password=PASSWORD, role=Role.AGENT,
                                full_name="A", settings=store)
    sub = agents.create_agent(db, agent, username=f"rs{tag}", password=PASSWORD, role=Role.SUB_AGENT,
                              full_name="S", settings=store)
    room = locations.create_location(db, admin, name=f"Room {tag}")
    world.master, world.agent, world.sub, world.room = master, agent, sub, room
    return world


# ------------------------------------------------------------------ who holds the Agent Panel
def test_agent_panel_is_for_agents_and_the_dev_team_only():
    assert {Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS} <= ROLE_PERMS[Role.AGENT]
    assert {Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS} <= ROLE_PERMS[Role.ADMIN]
    assert {Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS} <= ROLE_PERMS[Role.BISHOP]
    assert {Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS} <= ROLE_PERMS[Role.MASTER] and Perm.CUTS_MANAGE not in ROLE_PERMS[Role.MASTER]
    for role in (Role.SUB_AGENT, Role.TELLER, Role.DECLARATOR, Role.PAWN):
        assert not ({Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS} & ROLE_PERMS[role]), role


# ------------------------------------------------------------------ the order of precedence
def test_precedence_of_the_levels(world):
    store = world.store
    agent = SimpleNamespace(id=1, cut_bps=None, sop_bps=None)
    sub = SimpleNamespace(id=2, cut_bps=None, sop_bps=None)
    game_vol = commissions.effective_agent_bps(agent, store, "bola")
    game_sop = commissions.effective_sub_bps(sub, agent, store, "bola")
    row = lambda **kw: SimpleNamespace(volume_bps=kw.get("v"), sop_bps=kw.get("s"))   # noqa: E731
    room = commissions.RoomRates(default=row(v=2500, s=2000))
    assert commissions.effective_agent_bps(agent, store, "bola", room) == 2500        # room default beats the game's
    assert commissions.effective_sub_bps(sub, agent, store, "bola", room) == 2000
    agent.cut_bps, agent.sop_bps = 4000, 1500
    assert commissions.effective_agent_bps(agent, store, "bola", room) == 4000        # the Agent's own cut beats the room default
    assert commissions.effective_sub_bps(sub, agent, store, "bola", room) == 1500
    sub.cut_bps = 900
    assert commissions.effective_sub_bps(sub, agent, store, "bola", room) == 900      # a Sub Agent's personal cut beats that
    room.by_agent[1] = row(v=3500, s=5000)
    assert commissions.effective_agent_bps(agent, store, "bola", room) == 3500        # the Agent's row for the room beats all
    assert commissions.effective_sub_bps(sub, agent, store, "bola", room) == 5000
    assert commissions.effective_agent_bps(agent, store, "bola") == 4000              # no room: the old behaviour
    assert game_vol and game_sop


# ------------------------------------------------------------------ who may set what
def test_agent_sets_its_own_sop_per_room_and_nothing_else(net):
    w = net
    row = commissions.set_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, agent_id=w.agent.id,
                                   sop="45", settings=w.store)
    assert row.sop_bps == 4500 and row.volume_bps is None
    with pytest.raises(Forbidden):                                   # its own Volume % is the Admin side's
        commissions.set_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, agent_id=w.agent.id,
                                 volume="40", settings=w.store)
    with pytest.raises(Forbidden):                                   # nobody else's room
        commissions.set_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, agent_id=w.master.id,
                                 sop="10", settings=w.store)
    with pytest.raises(Forbidden):                                   # not the room's default
        commissions.set_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, sop="10", settings=w.store)
    with pytest.raises(ValidationError):                             # over the Settings limit
        commissions.set_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, agent_id=w.agent.id,
                                 sop="99", settings=w.store)


def test_sub_agent_cannot_set_room_cuts_and_master_only_for_its_own_agents(net):
    w = net
    with pytest.raises(Forbidden):
        commissions.set_room_cut(w.db, w.sub, game="sabong", location_id=w.room.id, agent_id=w.agent.id,
                                 sop="10", settings=w.store)
    stranger = agents.create_agent(w.db, w.admin, username=f"rx{w.tag}", password=PASSWORD, role=Role.MASTER,
                                   full_name="X", settings=w.store)
    with pytest.raises(Forbidden):                                   # a Master that did not invite this Agent
        commissions.set_room_cut(w.db, stranger, game="sabong", location_id=w.room.id, agent_id=w.agent.id,
                                 sop="10", settings=w.store)
    assert commissions.set_room_cut(w.db, w.master, game="sabong", location_id=w.room.id, agent_id=w.agent.id,
                                    volume="25", sop="10", settings=w.store).volume_bps == 2500   # the one that did


def test_admin_side_sets_room_default_and_agent_volume_and_clearing_removes_the_row(net):
    w = net
    commissions.set_room_cut(w.db, w.admin, game="sabong", location_id=w.room.id, volume="25", sop="20", settings=w.store)
    rows = {(r["game"], r["location"].id): r for r in commissions.room_rows(w.db, w.store, agent=w.agent)}
    r = rows[("sabong", w.room.id)]
    assert (r["volume_bps"], r["sop_bps"]) == (2500, 2000)
    assert r["with_sub"].paid == r["with_sub"].pool                  # the split still adds up to the pool
    commissions.set_room_cut(w.db, w.admin, game="sabong", location_id=w.room.id, agent_id=w.agent.id,
                             volume="35", settings=w.store)
    r = {(x["game"], x["location"].id): x for x in commissions.room_rows(w.db, w.store, agent=w.agent)}[("sabong", w.room.id)]
    assert r["volume_bps"] == 3500
    commissions.set_room_cut(w.db, w.admin, game="sabong", location_id=w.room.id, agent_id=w.agent.id,
                             volume="", settings=w.store)
    assert not [o for o in commissions.override_rows(w.db) if o.agent_id == w.agent.id and o.game == "sabong"]


# ------------------------------------------------------------------ it reaches the money
def test_a_rooms_cut_changes_the_commission_lines(net):
    w = net
    ticket = SimpleNamespace(id=1, status="settled", agent_id=w.sub.id, stake_cents=100_000, player_id=None, user_id=None)

    def sub_line(location_id):
        lines = commissions.compute_lines(w.db, [ticket], basis="turnover", house_gross=1_000_000, total_staked=100_000,
                                          settings=w.store, game="bola", location_id=location_id,
                                          pool_key="bola.commission.pool_bps")
        return {l.tier: l.amount_cents for l in lines}

    before = sub_line(w.room.id)
    commissions.set_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, agent_id=w.agent.id, sop="50", settings=w.store)
    after = sub_line(w.room.id)
    assert after["sub_agent"] > before["sub_agent"]                      # the Sub Agent earns more in that room
    assert after["agent"] < before["agent"]                              # out of the Agent's own cut
    assert after["master"] == before["master"]                           # the Master's remainder never moves
    assert sub_line(None) == before                                      # another room (or none): untouched


# ------------------------------------------------------------------ one Sub Agent in one room
def test_sub_room_row_beats_the_agents_room_setting(world):
    store = world.store
    agent = SimpleNamespace(id=1, cut_bps=None, sop_bps=None)
    sub = SimpleNamespace(id=2, cut_bps=900, sop_bps=None)
    other = SimpleNamespace(id=3, cut_bps=None, sop_bps=None)
    row = SimpleNamespace(volume_bps=None, sop_bps=5000)
    room = commissions.RoomRates(by_agent={1: row}, by_sub={2: SimpleNamespace(sop_bps=1200)})
    assert commissions.sub_bps_source(sub, agent, store, "bola", room) == (1200, "sub_room")     # this Sub Agent, this room
    assert commissions.sub_bps_source(other, agent, store, "bola", room) == (5000, "agent_room")  # its siblings follow the Agent's room
    assert commissions.sub_bps_source(sub, agent, store, "bola") == (900, "personal")             # no room: as before


def test_agent_controls_only_its_own_sub_agents(net):
    w = net
    row = commissions.set_sub_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, sub_id=w.sub.id,
                                       sop="40", settings=w.store)
    assert row.sop_bps == 4000
    with pytest.raises(ValidationError):                                         # over the Settings limit
        commissions.set_sub_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, sub_id=w.sub.id,
                                     sop="99", settings=w.store)
    other_agent = agents.create_agent(w.db, w.master, username=f"ro{w.tag}", password=PASSWORD, role=Role.AGENT,
                                      full_name="O", settings=w.store)
    with pytest.raises(Forbidden):                                               # someone else's Sub Agent
        commissions.set_sub_room_cut(w.db, other_agent, game="bola", location_id=w.room.id, sub_id=w.sub.id,
                                     sop="10", settings=w.store)
    with pytest.raises(Forbidden):                                               # a Sub Agent has no one below it
        commissions.set_sub_room_cut(w.db, w.sub, game="bola", location_id=w.room.id, sub_id=w.sub.id,
                                     sop="10", settings=w.store)
    assert commissions.set_sub_room_cut(w.db, w.master, game="bola", location_id=w.room.id, sub_id=w.sub.id,
                                        sop="10", settings=w.store).sop_bps == 1000   # its Master may (own Agent's Sub Agent)
    assert commissions.set_sub_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, sub_id=w.sub.id,
                                        sop="", settings=w.store) is None        # blank clears it
    assert commissions.set_sub_room_cut(w.db, w.admin, game="bola", location_id=w.room.id, sub_id=w.sub.id,
                                        sop="60", settings=w.store).sop_bps == 6000   # the Admin is not limited


def test_ongoing_games_list_the_open_event_and_feed_the_sub_panel(net):
    w = net
    ongoing = commissions.ongoing_games(w.db)
    assert any(g["game"] == "sabong" and w.event.title in g["what"] for g in ongoing)
    rows = commissions.sub_ongoing_rows(w.db, w.store, w.agent, w.sub)
    mine = next(r for r in rows if r["game"] == "sabong" and w.event.title in r["what"])
    loc_id = mine["location"].id
    commissions.set_sub_room_cut(w.db, w.agent, game="sabong", location_id=loc_id, sub_id=w.sub.id, sop="35", settings=w.store)
    again = next(r for r in commissions.sub_ongoing_rows(w.db, w.store, w.agent, w.sub)
                 if r["game"] == "sabong" and r["location"].id == loc_id)
    assert (again["sop_bps"], again["source"], again["own_bps"]) == (3500, "sub_room", 3500)
    assert again["split"].paid == again["split"].pool


def test_a_sub_agents_own_room_cut_reaches_the_commission_lines(net):
    w = net
    ticket = SimpleNamespace(id=1, status="settled", agent_id=w.sub.id, stake_cents=100_000, player_id=None, user_id=None)

    def lines():
        got = commissions.compute_lines(w.db, [ticket], basis="turnover", house_gross=1_000_000, total_staked=100_000,
                                        settings=w.store, game="bola", location_id=w.room.id,
                                        pool_key="bola.commission.pool_bps")
        return {l.tier: l.amount_cents for l in got}

    before = lines()
    commissions.set_sub_room_cut(w.db, w.agent, game="bola", location_id=w.room.id, sub_id=w.sub.id, sop="50", settings=w.store)
    after = lines()
    assert after["sub_agent"] > before["sub_agent"] and after["agent"] < before["agent"]
    assert after["master"] == before["master"]
