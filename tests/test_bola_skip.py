"""Skip the draw wait: dev-only by default, grantable per account, and the cut-off variable carries to upcoming draws."""
import datetime as dt

import pytest

from app.core.errors import Forbidden, LockedState
from app.core.rbac import Perm, permissions_for, permissions_of
from app.db.models import DrawStatus, Role
from app.services import agents, bola, locations
from tests.conftest import PASSWORD


@pytest.fixture()
def skipw(world):
    db, admin, store = world.db, world.admin, world.store
    store.set("bola.enabled", "true", admin.id)
    loc = locations.create_location(db, admin, name=f"Skip {world.tag}")
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    world.draw = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date.today(), slot=1,
                                  settings=store, scheduled_at=soon, status=DrawStatus.OPEN)
    world.loc = loc
    yield world
    loc.is_active = False
    db.flush()


def test_skip_is_off_for_every_role_but_the_admin():
    assert Perm.BOLA_SKIP in permissions_for(Role.ADMIN)
    for role in (Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.DECLARATOR, Role.PAWN):
        assert Perm.BOLA_SKIP not in permissions_for(role, gated=False)


def test_admin_skips_the_wait(skipw):
    w = skipw
    draw = bola.skip_wait(w.db, w.admin, w.draw.id, reason="test")
    assert draw.status == DrawStatus.LOCKED
    now = dt.datetime.now(dt.timezone.utc)
    assert draw.cutoff_at.replace(tzinfo=draw.cutoff_at.tzinfo or dt.timezone.utc) <= now
    assert draw.scheduled_at.replace(tzinfo=draw.scheduled_at.tzinfo or dt.timezone.utc) <= now
    assert not bola.bettable(draw, settings=w.store)


def test_skip_twice_is_refused(skipw):
    from app.core.errors import ValidationError
    bola.skip_wait(skipw.db, skipw.admin, skipw.draw.id)
    with pytest.raises(ValidationError):
        bola.skip_wait(skipw.db, skipw.admin, skipw.draw.id)


def test_master_cannot_skip_until_the_admin_allows_it(skipw):
    w = skipw
    master = agents.create_agent(w.db, w.admin, username=f"sm{w.tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="M", settings=w.store)
    assert Perm.BOLA_SKIP not in permissions_of(master)
    with pytest.raises(Forbidden):
        bola.skip_wait(w.db, master, w.draw.id)
    agents.set_permission_overrides(w.db, w.admin, master.id, [Perm.BOLA_SKIP.value], [])
    assert Perm.BOLA_SKIP in permissions_of(master)
    assert bola.skip_wait(w.db, master, w.draw.id).status == DrawStatus.LOCKED


def test_settled_draw_cannot_be_skipped(skipw):
    w = skipw
    w.draw.status = DrawStatus.VOID
    w.db.flush()
    with pytest.raises(LockedState):
        bola.skip_wait(w.db, w.admin, w.draw.id)


def test_changing_the_cutoff_variable_moves_upcoming_draws(skipw):
    w = skipw
    w.store.set("bola.cutoff_minutes", "10", w.admin.id)
    bola.resync_cutoffs(w.db, w.admin, w.store)
    before = w.draw.scheduled_at - w.draw.cutoff_at
    assert before == dt.timedelta(minutes=10)
    w.store.set("bola.cutoff_minutes", "5", w.admin.id)
    bola.resync_cutoffs(w.db, w.admin, w.store)
    assert w.draw.scheduled_at - w.draw.cutoff_at == dt.timedelta(minutes=5)
    w.store.set("bola.cutoff_minutes", "10", w.admin.id)


def test_desk_flags_follow_the_rights(skipw):
    """The Declarator Desk shows skip / change time / void to whoever holds the right, and delete to the Admin only."""
    from app.services import declarator
    w = skipw
    master = agents.create_agent(w.db, w.admin, username=f"dm{w.tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="M", settings=w.store)
    agents.set_permission_overrides(w.db, w.admin, master.id, [Perm.BOLA_SKIP.value], [])
    flags = {"bola_skip": Perm.BOLA_SKIP in permissions_of(master),
             "bola_manage": Perm.BOLA_MANAGE in permissions_of(master),
             "dev": master.role == Role.ADMIN}
    board = declarator.board(w.db, master, w.store, can=flags, sabong=False, with_bola=True)
    assert board["can"]["bola_skip"] and board["can"]["bola_manage"] and not board["can"]["dev"]
