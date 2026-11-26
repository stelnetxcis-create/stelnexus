"""The Agent Panel: no Master tier on it, and a Sub Agent's own panel opens only after the Sub Agent allows it and the
Master approves."""
import datetime as dt
import re

import pytest

from app.core.errors import Conflict, Forbidden
from app.db.models import Role
from app.services import agents, panel_access, players
from tests.conftest import PASSWORD
from tests.test_web import login


@pytest.fixture()
def net(world):
    db, admin, store, tag = world.db, world.admin, world.store, world.tag
    mk = lambda actor, role, name: agents.create_agent(    # noqa: E731
        db, actor, username=f"{name}{tag}", password=PASSWORD, role=role, full_name=f"Name {name}", settings=store)
    world.master = mk(admin, Role.MASTER, "pm")
    world.agent = mk(world.master, Role.AGENT, "pa")
    world.sub = mk(world.agent, Role.SUB_AGENT, "ps")
    world.other_master = mk(admin, Role.MASTER, "po")
    for u in (world.master, world.agent, world.sub, world.other_master):
        u.must_change_password = False
    p = players.register_online_player(
        db, world.sub, username=f"pp{tag}", password=PASSWORD, full_name=f"Leafy {tag}",
        birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store, agent_id=world.sub.id)
    world.p = p
    world.rooms = []                                   # rooms a test makes, switched off again so other tests stay clean
    db.commit()
    yield world
    from app.services import locations
    for room_id in world.rooms:
        locations.set_active(db, admin, room_id, False)
    db.commit()


def test_full_chain_ask_allow_approve(net):
    db = net.db
    assert not panel_access.is_open(db, net.agent.id, net.sub.id)
    row = panel_access.ask(db, net.agent, net.sub)
    assert row.status == "asked"
    assert panel_access.ask(db, net.agent, net.sub).id == row.id                      # asking twice changes nothing
    with pytest.raises(Conflict):
        panel_access.approver_answer(db, net.master, row.id, True)                    # the Master cannot skip the Sub Agent
    panel_access.sub_answer(db, net.sub, row.id, allow=True)
    assert row.status == "allowed" and not panel_access.is_open(db, net.agent.id, net.sub.id)   # still shut
    with pytest.raises(Forbidden):
        panel_access.approver_answer(db, net.other_master, row.id, True)              # someone else's Master
    with pytest.raises(Forbidden):
        panel_access.approver_answer(db, net.agent, row.id, True)                     # the Agent cannot approve itself
    panel_access.approver_answer(db, net.master, row.id, True)
    assert panel_access.is_open(db, net.agent.id, net.sub.id)
    panel_access.revoke(db, net.sub, row.id)
    assert not panel_access.is_open(db, net.agent.id, net.sub.id)


def test_master_is_notified_only_after_the_sub_agent_allows(net):
    from sqlalchemy import select
    from app.db.models import Notification
    db = net.db
    row = panel_access.ask(db, net.agent, net.sub)
    db.flush()
    count = lambda u: len(db.scalars(select(Notification).where(Notification.user_id == u.id)).all())   # noqa: E731
    before = count(net.master)
    panel_access.sub_answer(db, net.sub, row.id, allow=True)
    db.flush()
    assert count(net.master) == before + 1


def test_decline_and_reject_close_it_and_it_can_be_asked_again(net):
    db = net.db
    row = panel_access.ask(db, net.agent, net.sub)
    panel_access.sub_answer(db, net.sub, row.id, allow=False)
    assert row.status == "declined"
    panel_access.ask(db, net.agent, net.sub)
    panel_access.sub_answer(db, net.sub, row.id, allow=True)
    panel_access.approver_answer(db, net.master, row.id, False)
    assert row.status == "rejected" and not panel_access.is_open(db, net.agent.id, net.sub.id)


def test_only_your_own_sub_agent(net):
    with pytest.raises(Forbidden):
        panel_access.ask(net.db, net.master, net.sub)
    with pytest.raises(Forbidden):
        panel_access.ask(net.db, net.agent, net.agent)


def test_panel_has_no_master_tier_and_hides_players_until_approved(net):
    db = net.db
    c = login(f"pa{net.tag}")[0]
    html = c.get("/agent-panel").text
    assert "Master" not in re.sub(r"<style.*?</style>", "", html, flags=re.S).replace("the Master&rsquo;s approval", "").replace("the Master approves", "")
    assert "of 3" in html and "of 4" not in html
    assert f"Leafy {net.tag}" not in html and "Ask to view panel" in html
    row = panel_access.ask(db, net.agent, net.sub)
    panel_access.sub_answer(db, net.sub, row.id, allow=True)
    db.commit()
    html = c.get("/agent-panel").text
    assert "Waiting for the Master" in html and f"Leafy {net.tag}" not in html
    panel_access.approver_answer(db, net.master, row.id, True)
    db.commit()
    html = c.get("/agent-panel").text
    assert f"Leafy {net.tag}" in html and "Panel open" in html


def test_consent_pages_http(net):
    db = net.db
    row = panel_access.ask(db, net.agent, net.sub)
    db.commit()
    sc = login(f"ps{net.tag}")[0]
    r = sc.get("/agent-panel/consents")
    assert r.status_code == 200 and "Name pa" in r.text
    tok = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
    assert sc.post(f"/agent-panel/consents/{row.id}/allow", data={"csrf_token": tok}).status_code == 303
    mc = login(f"pm{net.tag}")[0]
    r = mc.get("/agent-panel/consents")
    assert r.status_code == 200 and "Approve" in r.text
    assert login(f"po{net.tag}")[0].get("/agent-panel/consents").text.count("Approve</button>") == 0
    tok = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
    assert mc.post(f"/agent-panel/consents/{row.id}/approve", data={"csrf_token": tok}).status_code == 303
    db.expire_all()
    assert panel_access.is_open(db, net.agent.id, net.sub.id)


def test_master_sees_the_agent_panel_and_approves_there(net):
    from app.services import locations
    db = net.db
    net.rooms.append(locations.create_location(db, net.admin, name=f"VRoom {net.tag}").id)   # cut fields live under a room
    row = panel_access.ask(db, net.agent, net.sub)
    panel_access.sub_answer(db, net.sub, row.id, allow=True)
    db.commit()
    mc = login(f"pm{net.tag}")[0]
    r = mc.get("/agent-panel")
    assert r.status_code == 200 and "Waiting for your approval" in r.text and "Name pa" in r.text
    assert 'href="/agent-panel/consents"' not in r.text                       # no separate page needed for the Master
    page = mc.get(f"/agent-panel?agent={net.agent.id}")
    assert page.status_code == 200 and 'name="volume"' in page.text          # a Master sets its own Agent's cuts
    tok = re.search(r'name="csrf-token" content="([^"]+)"', mc.get("/agent-panel").text).group(1)
    r = mc.post(f"/agent-panel/consents/{row.id}/approve", data={"csrf_token": tok, "back": "panel"})
    assert r.status_code == 303 and r.headers["location"] == "/agent-panel"
    db.expire_all()
    assert panel_access.is_open(db, net.agent.id, net.sub.id)
    # Masters look across the whole network (existing scope rule), but only the Master above the Sub Agent approves.
    other = login(f"po{net.tag}")[0].get("/agent-panel")
    assert other.status_code == 200 and "Waiting for your approval" not in other.text


def test_master_panel_is_the_agent_view_one_level_up(net):
    db = net.db
    row = panel_access.ask(db, net.agent, net.sub)
    db.commit()
    html = login(f"pm{net.tag}")[0].get("/agent-panel").text
    assert "Branch tree" in html and "gt-crown" in html and "Name pm" in html          # same hero and golden tree
    assert "Name pa" in html and "Name ps" in html                                      # Master -> Agent -> Sub Agent
    assert ">Master</span>" in html                                                     # the ladder tops out at Master
    assert f"Leafy {net.tag}" not in html and "Pick one to open their panel" not in html   # no separate picker page
    panel_access.sub_answer(db, net.sub, row.id, allow=True)
    panel_access.approver_answer(db, net.master, row.id, True)
    db.commit()
    assert f"Leafy {net.tag}" in login(f"pm{net.tag}")[0].get("/agent-panel").text          # approved: the leaf shows
    page = login(f"pm{net.tag}")[0].get(f"/agent-panel?agent={net.agent.id}")
    assert page.status_code == 200 and "My branch" in page.text                         # an Agent's panel, with a way back


def test_master_sets_the_cuts_of_its_own_agents_only(net):
    from app.core.errors import Forbidden
    from app.services import commissions, locations
    db = net.db
    room = locations.create_location(db, net.admin, name=f"MRoom {net.tag}")
    net.rooms.append(room.id)
    row = commissions.set_room_cut(db, net.master, game="sabong", location_id=room.id, agent_id=net.agent.id,
                                   volume="20", sop="10", settings=net.store)
    assert (row.volume_bps, row.sop_bps) == (2000, 1000)                       # Volume % and SOP %, both
    commissions.set_sub_room_cut(db, net.master, game="sabong", location_id=room.id, sub_id=net.sub.id,
                                 sop="5", settings=net.store)                  # and one Sub Agent of its Agent
    with pytest.raises(Forbidden):                                              # not another Master's Agent
        commissions.set_room_cut(db, net.other_master, game="sabong", location_id=room.id, agent_id=net.agent.id,
                                 volume="20", settings=net.store)
    with pytest.raises(Forbidden):
        commissions.set_sub_room_cut(db, net.other_master, game="sabong", location_id=room.id, sub_id=net.sub.id,
                                     sop="5", settings=net.store)
    with pytest.raises(Forbidden):                                              # an Agent still cannot raise its own Volume %
        commissions.set_room_cut(db, net.agent, game="sabong", location_id=room.id, agent_id=net.agent.id,
                                 volume="30", settings=net.store)
    with pytest.raises(Forbidden):                                              # and the room default stays with the Admin side
        commissions.set_room_cut(db, net.master, game="sabong", location_id=room.id, volume="20", settings=net.store)


def test_master_saves_a_cut_from_the_panel(net):
    from app.services import locations
    db = net.db
    room = locations.create_location(db, net.admin, name=f"HRoom {net.tag}")
    net.rooms.append(room.id)
    db.commit()
    mc = login(f"pm{net.tag}")[0]
    page = mc.get(f"/agent-panel?agent={net.agent.id}")
    assert 'name="volume"' in page.text and "View only" not in page.text.split("Defaults for all")[1][:6000]
    tok = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    r = mc.post("/agent-panel/rooms", data={"csrf_token": tok, "game": "sabong", "location_id": str(room.id),
                                            "agent_id": str(net.agent.id), "volume": "22", "sop": "11"})
    assert r.status_code == 303
    page = mc.get(f"/agent-panel?agent={net.agent.id}")
    assert 'value="22.0"' in page.text and 'value="11.0"' in page.text
    theirs = login(f"po{net.tag}")[0].get(f"/agent-panel?agent={net.agent.id}")   # another Master: look only
    assert theirs.status_code == 200 and 'name="volume"' not in theirs.text


def test_dev_team_sees_the_same_new_design_rooted_at_the_network(net):
    db = net.db
    row = panel_access.ask(db, net.agent, net.sub)
    db.commit()
    html = login("admin")[0].get("/agent-panel").text
    assert "Branch tree" in html and "gt-crown" in html and "Whole network" in html        # the new design, no picker page
    assert "Pick one to open their panel" not in html and "ap-card" not in html
    assert ">Admin</span>" in html                                                          # the ladder tops out at Admin
    assert "Name pm" in html and "Name pa" in html and "Name ps" in html                    # Master -> Agent -> Sub Agent
    assert f"/agent-panel?agent={net.agent.id}" in html                                     # an Agent opens from the tree
    assert f"Leafy {net.tag}" not in html                                                       # gated for devs too
    page = login("admin")[0].get(f"/agent-panel?agent={net.agent.id}")
    assert page.status_code == 200 and "Whole network" in page.text                         # and a way back


def test_who_may_set_the_overall_cut(net):
    from app.core.errors import Forbidden, ValidationError
    from app.services import commissions
    db, store = net.db, net.store
    # Master -> its own Agent (Volume % and SOP %) and that Agent's Sub Agent (SOP %)
    commissions.set_cut(db, net.master, net.agent, volume="35", sop="25", settings=store)
    assert (net.agent.cut_bps, net.agent.sop_bps) == (3500, 2500)
    commissions.set_cut(db, net.master, net.sub, sop="15", settings=store)
    assert net.sub.cut_bps == 1500
    with pytest.raises(ValidationError):                                  # held to the guard rail (50% by default)
        commissions.set_cut(db, net.master, net.agent, volume="90", settings=store)
    with pytest.raises(Forbidden):                                        # another Master's branch
        commissions.set_cut(db, net.other_master, net.agent, volume="35", settings=store)
    with pytest.raises(Forbidden):
        commissions.set_cut(db, net.other_master, net.sub, sop="15", settings=store)
    # Agent -> its own Sub Agent's SOP %, never its own Volume %
    commissions.set_cut(db, net.agent, net.sub, sop="20", settings=store)
    assert net.sub.cut_bps == 2000
    with pytest.raises(Forbidden):
        commissions.set_cut(db, net.agent, net.agent, volume="40", settings=store)
    with pytest.raises(Forbidden):
        commissions.set_cut(db, net.agent, net.agent, sop="40", settings=store)
    # a Sub Agent changes nothing
    with pytest.raises(Forbidden):
        commissions.set_cut(db, net.sub, net.sub, sop="40", settings=store)


def test_master_and_agent_save_the_overall_cut_from_the_panel(net):
    db = net.db
    mc = login(f"pm{net.tag}")[0]
    page = mc.get(f"/agent-panel?agent={net.agent.id}")
    assert "cut, all rooms" in page.text and 'action="/agent-panel/cut"' in page.text
    tok = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    r = mc.post("/agent-panel/cut", data={"csrf_token": tok, "agent_id": str(net.agent.id),
                                          "user_id": str(net.agent.id), "volume": "33", "sop": "22"})
    assert r.status_code == 303
    db.expire_all()
    assert (net.agent.cut_bps, net.agent.sop_bps) == (3300, 2200)
    sub = mc.get(f"/agent-panel?agent={net.agent.id}&sub={net.sub.id}")
    assert "SOP %, all rooms" in sub.text
    tok = re.search(r'name="csrf-token" content="([^"]+)"', sub.text).group(1)
    assert mc.post("/agent-panel/cut", data={"csrf_token": tok, "agent_id": str(net.agent.id),
                                             "user_id": str(net.sub.id), "sop": "12"}).status_code == 303
    db.expire_all()
    assert net.sub.cut_bps == 1200
    # the Agent: no Volume box for itself, but it can save its Sub Agent's SOP %
    ac = login(f"pa{net.tag}")[0]
    apage = ac.get("/agent-panel")
    assert "cut, all rooms" not in apage.text
    asub = ac.get(f"/agent-panel?sub={net.sub.id}")
    assert "SOP %, all rooms" in asub.text
    tok = re.search(r'name="csrf-token" content="([^"]+)"', asub.text).group(1)
    assert ac.post("/agent-panel/cut", data={"csrf_token": tok, "user_id": str(net.sub.id), "sop": "18"}).status_code == 303
    db.expire_all()
    assert net.sub.cut_bps == 1800
    tok = re.search(r'name="csrf-token" content="([^"]+)"', ac.get("/agent-panel").text).group(1)
    bad = ac.post("/agent-panel/cut", data={"csrf_token": tok, "user_id": str(net.agent.id), "volume": "40"})
    assert bad.status_code in (302, 303, 403) and not (bad.status_code == 303 and "saved" in bad.headers.get("location", ""))
    db.expire_all()
    assert net.agent.cut_bps == 3300                                       # the Agent could not raise its own Volume %
