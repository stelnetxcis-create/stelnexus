"""Declarator Desk: the emergency side blocker and the auto-close timer."""
import datetime as dt

import pytest

from app.core.errors import LockedState, ValidationError
from app.db.base import utcnow
from app.db.models import FightStatus, Side
from app.services import betting, events
from tests.test_declarator_desk import JSON, _desk, _mine


def _bet(w, game, side):
    return betting.place_bet(w.db, w.admin, fight_id=game.id, side=side, stake_cents=10_000,
                             settings=w.store, player_id=w.player.id)


# ----------------------------------------------------------------------------- side blocker

def test_declarator_blocks_one_side_and_only_the_other_takes_bets(world):
    c, h = _desk(world)
    g = world.games[0]
    r = c.post(f"/staff/declarator/fights/{g.id}/block", data={"side": "meron"}, headers=h).json()
    assert r["ok"] and "MERON is blocked" in r["message"]
    world.db.expire_all()
    assert events.get_fight(world.db, g.id).bet_mode == "wala"
    with pytest.raises(LockedState):
        _bet(world, g, Side.MERON)
    assert _bet(world, g, Side.WALA).side == Side.WALA                 # the unblocked side still works
    board = _mine(c.get("/_live/declarator", headers=JSON).json(), world.event.id)
    assert board["games"][0]["bm"] == "wala"


def test_blocking_can_be_undone_and_is_audited(world):
    from sqlalchemy import select
    from app.db.models import AuditLog
    c, h = _desk(world)
    g = world.games[0]
    c.post(f"/staff/declarator/fights/{g.id}/block", data={"side": "wala"}, headers=h)
    assert c.post(f"/staff/declarator/fights/{g.id}/block", data={"side": "none"}, headers=h).json()["ok"]
    world.db.expire_all()
    assert events.get_fight(world.db, g.id).bet_mode == "all"
    assert _bet(world, g, Side.MERON).side == Side.MERON
    n = len(world.db.scalars(select(AuditLog).where(AuditLog.action == "fight.bet_mode", AuditLog.entity_id == str(g.id))).all())
    assert n >= 2


def test_block_needs_a_valid_side_and_the_right_role(world):
    c, h = _desk(world)
    g = world.games[0]
    assert c.post(f"/staff/declarator/fights/{g.id}/block", data={"side": "both"}, headers=h).status_code in (400, 422)
    from tests.test_web import login
    p, _ = login(world.player.username)
    assert p.post(f"/staff/declarator/fights/{g.id}/block", data={"side": "meron"},
                  headers={"X-CSRF-Token": (p.cookies.get("snx_csrf") or "x").split(".")[0], **JSON}).status_code in (302, 303, 401, 403)


# ----------------------------------------------------------------------------- auto-close timer

def test_timer_closes_betting_when_it_runs_out_and_a_late_bet_is_refused(world):
    c, h = _desk(world)
    g = world.games[0]
    r = c.post(f"/staff/declarator/fights/{g.id}/timer", data={"seconds": "60"}, headers=h).json()
    assert r["ok"] and "60 seconds" in r["message"]
    world.db.expire_all()
    fight = events.get_fight(world.db, g.id)
    assert fight.auto_close_at is not None and fight.status == FightStatus.OPEN
    assert _bet(world, g, Side.MERON)                                      # still open before the deadline
    assert events.close_due_fights(world.db) == 0
    fight.auto_close_at = utcnow() - dt.timedelta(seconds=1)               # the deadline passes
    world.db.flush()
    with pytest.raises(LockedState):                                       # refused even before the sweeper has run
        _bet(world, g, Side.WALA)
    assert events.close_due_fights(world.db) == 1
    assert fight.status == FightStatus.LOCKED and fight.auto_close_at is None and fight.locked_at is not None


def test_live_poll_closes_an_overdue_game_and_reports_the_deadline(world):
    c, h = _desk(world)
    g = world.games[0]
    c.post(f"/staff/declarator/fights/{g.id}/timer", data={"seconds": "90"}, headers=h)
    body = c.get(f"/_live/event/{world.event.id}", headers=JSON).json()
    row = body["fights"][0]
    assert row["st"] == "open" and row["ac"] > 0 and body["event"]["sn"] > 0
    world.db.expire_all()
    events.get_fight(world.db, g.id).auto_close_at = utcnow() - dt.timedelta(seconds=2)
    world.db.commit()
    body = c.get(f"/_live/event/{world.event.id}", headers=JSON).json()
    assert body["fights"][0]["st"] == "locked" and body["fights"][0]["ac"] == 0


def test_timer_can_be_cancelled_and_manual_close_clears_it(world):
    c, h = _desk(world)
    g = world.games[0]
    c.post(f"/staff/declarator/fights/{g.id}/timer", data={"seconds": "30"}, headers=h)
    assert c.post(f"/staff/declarator/fights/{g.id}/timer", data={"seconds": "0"}, headers=h).json()["ok"]
    world.db.expire_all()
    assert events.get_fight(world.db, g.id).auto_close_at is None
    c.post(f"/staff/declarator/fights/{g.id}/timer", data={"seconds": "30"}, headers=h)
    c.post(f"/staff/declarator/fights/{g.id}/lock", data={"locked": "1"}, headers=h)    # the declarator closes it by hand
    world.db.expire_all()
    f = events.get_fight(world.db, g.id)
    assert f.status == FightStatus.LOCKED and f.auto_close_at is None


def test_timer_limits_and_state_rules(world):
    g = world.games[0]
    with pytest.raises(ValidationError):
        events.set_auto_close(world.db, world.admin, g.id, 2)
    with pytest.raises(ValidationError):
        events.set_auto_close(world.db, world.admin, g.id, 99999)
    events.lock_fight(world.db, world.admin, g.id, True)
    with pytest.raises(LockedState):
        events.set_auto_close(world.db, world.admin, g.id, 30)
    assert events.close_reason(g) is None                                   # nothing to close once it is locked
