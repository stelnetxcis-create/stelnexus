"""Number game: the logged-in account always bets from its OWN credit (like the fight game).

There is no player to pick. Stake, cancel refund and winnings all use the bettor's own wallet."""
import pytest
from sqlalchemy import select

from app.core.errors import Conflict, Forbidden, InsufficientFunds
from app.db.models import BolaKind, BolaTicket, Role, TicketStatus, User
from app.services import accounts, bola, bola_settlement, ledger
from tests.conftest import fund_user
from tests.test_bola import _declare, bw  # noqa: F401  (bw is a fixture)


def _fund(w, user, cents):
    fund_user(w.db, w.admin, user, cents)


def _credit(w, user):
    return accounts.wallet_for_user(w.db, user).balance_cents


def _self_bet(w, user, numbers=(7, 9), tumbok=1000, sahod=0, **extra):
    return bola.place_bola_bet(w.db, user, draw_id=w.draw.id, kind=BolaKind.PAIR, numbers=list(numbers),
                               tumbok_cents=tumbok, sahod_cents=sahod, settings=w.store, **extra)


def test_bet_uses_the_accounts_own_credit(bw):
    w = bw                                         # the fixture already loaded the Sub Agent's own wallet
    before = _credit(w, w.kub)
    t = _self_bet(w, w.kub)
    assert t.user_id == w.kub.id and t.player_id is None and t.agent_id == w.kub.id
    assert t.stake_cents == 1000
    assert _credit(w, w.kub) == before - 1000
    assert ledger.verify_integrity(w.db)["ok"]


def test_not_enough_credit_is_refused_and_nothing_is_booked(bw):
    w = bw
    _fund(w, w.cavo, 500)                          # an Agent with only PHP 5 of its own
    before = w.db.scalar(select(BolaTicket.id).order_by(BolaTicket.id.desc()).limit(1))
    with pytest.raises(InsufficientFunds):
        _self_bet(w, w.cavo, tumbok=1000)
    assert _credit(w, w.cavo) == 500
    assert w.db.scalar(select(BolaTicket.id).order_by(BolaTicket.id.desc()).limit(1)) == before


def test_account_without_a_balance_cannot_bet(bw):
    w = bw
    staff = User(username="decl-no-wallet", role=Role.DECLARATOR, full_name="D")
    with pytest.raises(Forbidden):
        _self_bet(w, staff)


def test_winnings_land_in_the_bettors_own_wallet(bw):
    w = bw
    before = _credit(w, w.kub)
    t = _self_bet(w, w.kub, numbers=(7, 9), tumbok=1000)
    _declare(w, 7, 9)
    w.db.refresh(t)
    assert t.status == TicketStatus.SETTLED and t.payout_cents > 0
    assert _credit(w, w.kub) == before - 1000 + t.payout_cents
    assert bola_settlement.beneficiary_account(w.db, t).id == accounts.wallet_for_user(w.db, w.kub).id
    assert ledger.verify_integrity(w.db)["ok"]


def test_cancel_is_admin_only_and_refunds_the_bettors_own_wallet(bw):
    w = bw
    before = _credit(w, w.kub)
    t = _self_bet(w, w.kub)
    with pytest.raises(Forbidden):                       # a placed bet is final: its own bettor cannot undo it
        bola.cancel_bola_ticket(w.db, w.kub, ticket_id=t.id, reason="changed my mind", settings=w.store)
    bola.cancel_bola_ticket(w.db, w.admin, ticket_id=t.id, reason="admin undo", settings=w.store)
    assert _credit(w, w.kub) == before                   # the Admin's cancel refunds the bettor's own wallet
    assert ledger.verify_integrity(w.db)["ok"]


def test_idempotency_key_never_bets_twice_and_rejects_a_different_bet(bw):
    w = bw
    before = _credit(w, w.kub)
    a = _self_bet(w, w.kub, idempotency_key="self-bola-1")
    b = _self_bet(w, w.kub, idempotency_key="self-bola-1")
    assert a.id == b.id and _credit(w, w.kub) == before - 1000
    with pytest.raises(Conflict):
        _self_bet(w, w.kub, numbers=(1, 2), idempotency_key="self-bola-1")


def test_slip_has_no_required_player_and_bets_over_http():
    from tests.test_bola import _web_setup, _web_teardown
    from tests.test_web import login
    from app.db.session import session_scope
    ids = _web_setup()
    try:
        with session_scope() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            ledger.post(db, kind="request.in",
                        entries=[(accounts.gcash_clearing(db), +100_000), (accounts.wallet_for_user(db, admin), -100_000)],
                        actor=admin, memo="test load")
            admin_id = admin.id
        c, _ = login("admin")
        html = c.get(f"/bola/draws/{ids['draw']}").text
        assert "Your credit" in html
        slip = html.split("Bet slip", 1)[1]               # the header has an unrelated "Open a player" box
        assert 'name="player_id"' not in slip and "Bet for a player" not in html
        token = c.cookies.get("snx_csrf").split(".")[0]
        r = c.post("/bola/bets/place", data={"csrf_token": token, "draw_id": ids["draw"], "kind": "pair",
                                             "n1": 3, "n2": 4, "tumbok": "5", "sahod": "0", "print": "1"})
        assert r.status_code == 303, r.text[:300]
        tid = int(r.headers["location"].rsplit("/", 1)[1])
        with session_scope() as db:
            t = db.get(BolaTicket, tid)
            assert t.user_id == admin_id and t.player_id is None
        assert c.get(r.headers["location"]).status_code == 200            # the ticket page renders without a player
        assert c.get("/bola/betlogs", follow_redirects=True).status_code == 200
    finally:
        _web_teardown(ids)
