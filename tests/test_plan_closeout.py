"""Plan close-out tests: the sabong idempotency race, the 'house-losing draw pays no commission'
ruling, and the pawn-proof refusal on the Bola declare route."""
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BolaCommission, BolaKind, Role, Side, Ticket, User
from app.db.session import session_scope
from app.services import accounts, agents, betting, bola, bola_settlement, ledger
from tests.conftest import PASSWORD
from tests.test_bola import _bet, _declare, _web_setup, _web_teardown, bw  # noqa: F401  (bw is a fixture)
from tests.test_web import login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ---------------------------------------------------------------- sabong idempotency race
def test_sabong_duplicate_key_that_waited_on_the_game_lock_gets_the_first_ticket(world):
    """Two requests with the same key: the second passes the early check (the first had not
    committed yet), then waits on the game lock. After the first commits, the second must hand
    back the first ticket, not trip the unique key or charge again."""
    w = world
    game = w.games[0]
    kw = dict(fight_id=game.id, side=Side.MERON, stake_cents=10_000, settings=w.store,
              player_id=w.player.id, idempotency_key=f"race-{w.tag}")
    first = betting.place_bet(w.db, w.admin, **kw)
    balance = accounts.wallet_for_player(w.db, w.player).balance_cents

    # Simulate the race: the pre-lock lookup sees nothing (first request not committed yet),
    # every later lookup (after the lock) sees the truth.
    real_scalar, seen = w.db.scalar, {"early": False}

    def racing_scalar(stmt, *a, **k):
        if not seen["early"] and "tickets.idempotency_key" in str(stmt):
            seen["early"] = True
            return None
        return real_scalar(stmt, *a, **k)

    w.db.scalar = racing_scalar
    try:
        second = betting.place_bet(w.db, w.admin, **kw)
    finally:
        del w.db.scalar                                   # back to the class method
    assert seen["early"], "the simulated early miss was never hit"
    assert second.id == first.id
    assert accounts.wallet_for_player(w.db, w.player).balance_cents == balance      # charged once
    n = w.db.scalar(select(func.count(Ticket.id)).where(Ticket.idempotency_key == kw["idempotency_key"]))
    assert n == 1
    assert ledger.verify_integrity(w.db)["ok"]


# ---------------------------------------------------------------- ruling (a), pinned
def test_house_losing_draw_pays_no_commission(bw):
    """Plan ruling (a), current behaviour: when the house pays out more than it took in on a
    draw, no commission is written (the cap is applied to the house's gross)."""
    w = bw
    w.store.set("bola.commission.pay_master", "true", w.admin.id)     # even with every tier switched on
    try:
        _bet(w, [7, 9], BolaKind.PAIR, 10_000, 0)                     # PHP 100 on 7-9 -> pays PHP 80,000
        _declare(w, 7, 9)
        rows = list(w.db.scalars(select(BolaCommission).where(BolaCommission.draw_id == w.draw.id)))
        assert sum(r.amount_cents for r in rows) == 0
        assert ledger.verify_integrity(w.db)["ok"]
    finally:
        w.store.set("bola.commission.pay_master", "true", w.admin.id)


# ---------------------------------------------------------------- pawn proof on Bola declare
@pytest.fixture()
def gate_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", False)


def test_pawn_declaring_bola_without_a_picture_is_refused_when_proof_is_required(gate_off):
    ids = _web_setup()
    try:
        with session_scope() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            from app.core.settings_registry import SettingsStore
            store = SettingsStore(db)
            store.set("fight.pawn_proof_required", "true", admin.id)
            agents.create_agent(db, admin, username=f"bpawn{ids['draw']}", password=PASSWORD,
                                role=Role.PAWN, full_name="P", settings=store)
        c, r = login(f"bpawn{ids['draw']}")
        assert r.status_code == 303
        token = c.cookies.get("snx_csrf").split(".")[0]
        url = f"/bola/draws/{ids['draw']}/declare"
        data = {"csrf_token": token, "ball_first": "3", "ball_second": "4"}
        hdr = {"Accept": "application/json"}

        no_pic = c.post(url, data=data, headers=hdr)
        assert no_pic.status_code in (400, 422), no_pic.text[:200]
        assert "picture" in no_pic.text.lower()

        with_pic = c.post(url, data=data, files={"proof": ("r.png", PNG, "image/png")}, headers=hdr)
        # The picture check now passes; the draw time has not arrived, so a Pawn is still stopped,
        # but for that reason and not for the missing picture.
        assert "picture" not in with_pic.text.lower() or with_pic.status_code == 303
        with session_scope() as db:
            from app.db.models import BolaDraw, DrawStatus
            assert db.get(BolaDraw, ids["draw"]).status != DrawStatus.SETTLED
    finally:
        _web_teardown(ids)


# ---------------------------------------------------------------- review fixes (Nov 17)
def test_reused_idempotency_key_on_a_different_bet_is_refused(bw):
    """A key is a double-tap guard, not a ticket lookup: same key + different numbers/stake must
    not hand back the old ticket as if the new bet had been placed."""
    from app.core.errors import Conflict
    w = bw
    base = dict(draw_id=w.draw.id, kind=BolaKind.PAIR, tumbok_cents=1000, sahod_cents=0,
                settings=w.store, idempotency_key=f"reuse-{w.tag}")
    first = bola.place_bola_bet(w.db, w.kub, numbers=[1, 2], **base)
    again = bola.place_bola_bet(w.db, w.kub, numbers=["1", "2"], **base)       # same bet, form strings
    assert again.id == first.id
    for change in ({"numbers": [1, 3]}, {"tumbok_cents": 2000}, {"sahod_cents": 500}):
        with pytest.raises(Conflict):
            bola.place_bola_bet(w.db, w.kub, **{**base, "numbers": [1, 2], **change})
    n = w.db.scalar(select(func.count()).select_from(bola.BolaTicket)
                    .where(bola.BolaTicket.idempotency_key == base["idempotency_key"]))
    assert n == 1


def test_cancel_takes_the_draw_lock_before_the_ticket_lock(bw):
    """Settlement locks draw -> tickets. If cancel locked ticket -> draw, a cancel and a declare on
    the same draw could wait on each other forever (PostgreSQL aborts one with a 500). SQLite
    ignores FOR UPDATE, so look at the statements the ORM is asked to run."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    w = bw
    ticket = _bet(w, [4, 5], BolaKind.PAIR, 1000, 0)
    locked: list[str] = []

    @event.listens_for(Session, "do_orm_execute")
    def spy(state):
        stmt = state.statement
        if getattr(stmt, "_for_update_arg", None) is not None:
            locked.append(state.bind_mapper.class_.__name__ if state.bind_mapper else "?")

    try:
        bola.cancel_bola_ticket(w.db, w.admin, ticket_id=ticket.id, reason="test", settings=w.store)
    finally:
        event.remove(Session, "do_orm_execute", spy)
    assert locked[:2] == ["BolaDraw", "BolaTicket"], locked
