"""Declarator Desk (/staff/declarator): the live board, its poll, and the in-place actions."""
import datetime as dt
import json

from app.db.models import DrawStatus, EventStatus, FightStatus, Role
from app.services import agents, bola, events, locations
from tests.conftest import PASSWORD
from tests.test_web import login

JSON = {"Accept": "application/json"}


def _mk(db, admin, store, role, name):
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-8:]
    u = agents.create_agent(db, admin, username=f"{name}{tag}", password=PASSWORD, role=role,
                            full_name=name, settings=store)
    u.must_change_password = False
    db.flush()
    return u


def _desk(world, role=Role.DECLARATOR, name="dk"):
    boss = world.admin
    if role in (Role.AGENT, Role.SUB_AGENT):         # the network roles need an upline
        boss = _mk(world.db, world.admin, world.store, Role.MASTER, "bm")
    u = agents.create_agent(world.db, boss, username=f"{name}{world.tag}", password=PASSWORD, role=role,
                            full_name=name, settings=world.store)
    u.must_change_password = False
    world.db.flush()
    world.db.commit()
    c, r = login(u.username)
    assert r.status_code == 303
    return c, {"X-CSRF-Token": c.cookies.get("snx_csrf").split(".")[0], **JSON}


def _mine(board, event_id):
    return next(e for e in board["events"] if e["id"] == event_id)


def test_page_and_board_for_a_declarator_carry_no_money(world):
    c, _ = _desk(world)
    page = c.get("/staff/declarator")
    assert page.status_code == 200 and 'id="dk-boot"' in page.text and "declarator" in page.text
    board = c.get("/_live/declarator", headers=JSON).json()
    ev = _mine(board, world.event.id)
    assert ev["cur"] == world.games[0].id and ev["lane"] == "open" and len(ev["games"]) == 6
    blob = json.dumps(board).lower()
    for word in ("stake", "pool", "payout", "cents", "balance", "ticket", "house"):
        assert word not in blob, word


def test_lanes_follow_the_game_and_poll_uses_etag(world):
    c, h = _desk(world)
    first = c.get("/_live/declarator", headers=JSON)
    assert c.get("/_live/declarator", headers={**JSON, "If-None-Match": first.headers["ETag"]}).status_code == 304
    g = world.games[0]
    assert c.post(f"/staff/declarator/events/{world.event.id}/stream",
                  data={"action": "start", "fight_id": g.id}, headers=h).json()["ok"]
    assert _mine(c.get("/_live/declarator", headers=JSON).json(), world.event.id)["lane"] == "air"
    c.post(f"/staff/declarator/events/{world.event.id}/stream", data={"action": "reset", "fight_id": g.id}, headers=h)
    assert c.post(f"/staff/declarator/fights/{g.id}/lock", data={"locked": "1"}, headers=h).json()["ok"]
    assert _mine(c.get("/_live/declarator", headers=JSON).json(), world.event.id)["lane"] == "due"


def test_declare_in_place_advances_the_event_and_a_repeat_is_refused(world):
    c, h = _desk(world)
    g1, g2 = world.games[0], world.games[1]
    done = c.post(f"/staff/declarator/fights/{g1.id}/declare", data={"result": "meron", "reason": "ok"}, headers=h)
    assert done.status_code == 200 and "MERON" in done.json()["message"]
    world.db.expire_all()
    assert events.get_fight(world.db, g1.id).status == FightStatus.SETTLED
    assert _mine(c.get("/_live/declarator", headers=JSON).json(), world.event.id)["cur"] == g2.id
    again = c.post(f"/staff/declarator/fights/{g1.id}/declare", data={"result": "wala"}, headers=h)
    assert again.status_code >= 400 and "already settled" in again.json()["detail"]
    recent = c.get("/_live/declarator", headers=JSON).json()["recent"]
    assert any(r["id"] == g1.id and r["res"] == "meron" for r in recent)


def test_actions_need_the_staff_permission_and_a_csrf_token(world):
    c, h = _desk(world, Role.AGENT, "ag")
    g = world.games[0]
    assert c.get("/staff/declarator").status_code == 403
    assert c.get("/_live/declarator", headers=JSON).status_code == 403
    assert c.post(f"/staff/declarator/fights/{g.id}/declare", data={"result": "meron"}, headers=h).status_code == 403
    d, _ = _desk(world, Role.DECLARATOR, "dn")
    assert d.post(f"/staff/declarator/fights/{g.id}/declare", data={"result": "meron"}, headers=JSON).status_code == 403
    world.db.expire_all()
    assert events.get_fight(world.db, g.id).status == FightStatus.OPEN


def test_a_declarator_can_open_a_draft_event_from_the_desk(world):
    c, h = _desk(world)
    events.set_event_status(world.db, world.admin, world.event.id, EventStatus.DRAFT)
    world.db.commit()
    ev = _mine(c.get("/_live/declarator", headers=JSON).json(), world.event.id)
    assert ev["st"] == "draft" and ev["lane"] == "next" and ev["games"] == []
    assert c.post(f"/staff/declarator/events/{world.event.id}/open", headers=h).json()["ok"]
    world.db.expire_all()
    assert events.get_event(world.db, world.event.id).status == EventStatus.OPEN


def test_bola_draw_waits_for_its_time_then_declares_in_place(world):
    db, admin, store = world.db, world.admin, world.store
    store.set("bola.enabled", "true", admin.id)
    loc = locations.create_location(db, admin, name=f"Area {world.tag}")
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    later = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date.today(), slot=1, settings=store,
                             scheduled_at=soon, status=DrawStatus.OPEN)
    past = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date.today(), slot=2, settings=store,
                            scheduled_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
                            status=DrawStatus.LOCKED)
    db.commit()
    c, h = _desk(world)
    board = c.get("/_live/declarator", headers=JSON).json()
    lanes = {d["id"]: d["lane"] for d in board["draws"]}
    assert lanes[later.id] == "next" and lanes[past.id] == "due" and board["early_ok"] is False
    early = c.post(f"/staff/declarator/bola/{later.id}/declare", data={"ball_first": 3, "ball_second": 9}, headers=h)
    assert early.status_code >= 400
    bad = c.post(f"/staff/declarator/bola/{past.id}/declare", data={"ball_first": 5, "ball_second": 5}, headers=h)
    assert bad.status_code == 422 or bad.status_code == 400
    ok = c.post(f"/staff/declarator/bola/{past.id}/declare", data={"ball_first": 12, "ball_second": 24}, headers=h)
    assert ok.status_code == 200 and "12 / 24" in ok.json()["message"]
    assert any(r["kind"] == "bola" and r["res"] == "12 / 24"
               for r in c.get("/_live/declarator", headers=JSON).json()["recent"])
    loc.is_active = False        # an active area makes later sabong fixtures demand a location
    db.commit()


def test_staff_home_tile_counts_games_waiting(world):
    import re
    c, h = _desk(world)

    def waiting():
        text = re.sub(r"\s+", " ", c.get("/staff").text)
        found = re.search(r"Declarator</span> <strong class=\"stat-value\">(?:(\d+) waiting|All clear)", text)
        return int(found.group(1) or 0)

    before = waiting()                       # other tests may have left games waiting in the shared database
    h["X-CSRF-Token"] = c.cookies.get("snx_csrf").split(".")[0]     # a page view re-issues the token
    c.post(f"/staff/declarator/fights/{world.games[0].id}/lock", data={"locked": "1"}, headers=h)
    assert waiting() == before + 1
