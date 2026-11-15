import pytest
from sqlalchemy import select

from app.core.errors import Conflict, LockedState, ValidationError
from app.db.models import Result, Role, Side, Ticket, TicketStatus
from app.services import agents, betting, events, players, reports, settlement
from tests.conftest import PASSWORD


def sell(w, game, side, cents):
    return betting.place_bet(w.db, w.teller, fight_id=game.id, side=side, stake_cents=cents,
                             settings=w.store, station=w.station, funded_by="cash")


def declare(w, game, result):
    return settlement.declare(w.db, w.admin, fight_id=game.id, result=result, settings=w.store)


def tickets(w, game):
    return list(w.db.scalars(select(Ticket).where(Ticket.fight_id == game.id).order_by(Ticket.id)))


def test_draw_loses_when_meron_wins(world):
    g = world.games[0]
    sell(world, g, Side.MERON, 10000); sell(world, g, Side.WALA, 10000); sell(world, g, Side.DRAW, 2000)
    run = declare(world, g, Result.MERON)
    t = run.totals
    assert t["house_cut"] == 1000 and t["payout_total"] == 19000
    assert t["house_take"] == 3000          # tong 1,000 + lost draw stake 2,000
    assert {x.side.value: x.payout_cents for x in tickets(world, g)}["draw"] == 0


def test_draw_result_pays_draw_and_refunds_sides(world):
    g = world.games[0]
    sell(world, g, Side.MERON, 10000); sell(world, g, Side.WALA, 10000); sell(world, g, Side.DRAW, 2000)
    declare(world, g, Result.DRAW)
    by = {x.side.value: (x.status, x.payout_cents) for x in tickets(world, g)}
    assert by["meron"] == (TicketStatus.REFUNDED, 10000)
    assert by["wala"] == (TicketStatus.REFUNDED, 10000)
    assert by["draw"] == (TicketStatus.SETTLED, 16000)      # 8.00x default


def test_minimum_odds_cancels_and_refunds(world):
    g = world.games[0]
    sell(world, g, Side.MERON, 100000); sell(world, g, Side.WALA, 5000)
    run = declare(world, g, Result.MERON)
    assert run.totals["auto_cancel"] is True
    assert all(x.status == TicketStatus.REFUNDED for x in tickets(world, g))
    assert run.totals["house_take"] == 0


def test_minimum_odds_can_be_switched_off(world):
    world.store.set("payout.min_odds_bps", "0", world.admin.id)
    try:
        g = world.games[0]
        sell(world, g, Side.MERON, 100000); sell(world, g, Side.WALA, 5000)
        assert declare(world, g, Result.MERON).totals["auto_cancel"] is False
    finally:
        world.store.set("payout.min_odds_bps", "14000", world.admin.id)


def test_force_bet_side(world):
    g = world.games[0]
    events.set_bet_mode(world.db, world.admin, g.id, "meron")
    with pytest.raises(LockedState):
        sell(world, g, Side.WALA, 2000)
    with pytest.raises(LockedState):
        sell(world, g, Side.DRAW, 2000)
    sell(world, g, Side.MERON, 2000)
    events.set_bet_mode(world.db, world.admin, g.id, "all")
    sell(world, g, Side.WALA, 2000)


def test_llamado_dehado(world):
    g = world.games[0]
    with pytest.raises(ValidationError):
        events.resolve_result(g, "llamado")
    events.set_llamado(world.db, world.admin, g.id, "wala")
    assert events.resolve_result(g, "llamado") == Result.WALA
    assert events.resolve_result(g, "dehado") == Result.MERON
    assert events.role_of(g, "wala") == "Llamado" and events.role_of(g, "meron") == "Dehado"


def test_claim_once_and_unclaimed_figure(world):
    g = world.games[0]
    winner = sell(world, g, Side.MERON, 10000); sell(world, g, Side.WALA, 10000)
    declare(world, g, Result.MERON)
    assert betting.claim_status(winner) == "pending"
    assert reports.fight_figures(world.db, g)["unclaimed"] == winner.payout_cents
    betting.claim_ticket(world.db, world.teller, ticket_id=winner.id)
    assert reports.fight_figures(world.db, g)["unclaimed"] == 0
    with pytest.raises(Conflict):
        betting.claim_ticket(world.db, world.teller, ticket_id=winner.id)


def test_claim_rejects_unsettled_and_losing(world):
    g = world.games[0]
    open_t = sell(world, g, Side.MERON, 10000); loser = sell(world, g, Side.WALA, 10000)
    with pytest.raises(LockedState):
        betting.claim_ticket(world.db, world.teller, ticket_id=open_t.id)
    declare(world, g, Result.MERON)
    with pytest.raises(LockedState):
        betting.claim_ticket(world.db, world.teller, ticket_id=loser.id)


def test_scan_accepts_code_or_link(world):
    g = world.games[0]
    t = sell(world, g, Side.MERON, 2000)
    assert betting.find_by_scan(world.db, t.code.lower()).id == t.id
    assert betting.find_by_scan(world.db, f"https://x.test/tickets/{t.id}").id == t.id


def test_cancel_event_refunds_and_blocks_reopen(world):
    open_game, empty_game, done = world.games[0], world.games[1], world.games[2]
    t = sell(world, open_game, Side.MERON, 3000)
    sell(world, done, Side.MERON, 10000); sell(world, done, Side.WALA, 10000)
    declare(world, done, Result.MERON)
    result = events.cancel_event(world.db, world.admin, world.event.id, reason="rain stopped play",
                                 settings=world.store)
    assert result["refunded"] == 1 and result["skipped"] == 1 and result["voided"] == 4
    world.db.refresh(t)
    assert t.status == TicketStatus.REFUNDED and t.payout_cents == 3000
    with pytest.raises(Conflict):
        events.cancel_event(world.db, world.admin, world.event.id, reason="again again",
                            settings=world.store)
    with pytest.raises(LockedState):
        events.set_event_status(world.db, world.admin, world.event.id, "open")


def test_event_expenses_feed_net_income(world):
    g = world.games[0]
    sell(world, g, Side.MERON, 10000); sell(world, g, Side.WALA, 10000)
    declare(world, g, Result.MERON)
    before = reports.event_summary(world.db, world.event.id)
    events.add_expense(world.db, world.admin, world.event.id, label="System fee", amount_cents=500)
    after = reports.event_summary(world.db, world.event.id)
    assert after["expenses"] == 500 and after["net"] == before["net"] - 500
    with pytest.raises(ValidationError):
        events.add_expense(world.db, world.admin, world.event.id, label="x", amount_cents=0)


def test_withdrawal_switch_blocks_cash_out(world):
    import datetime as dt
    p = players.register_card_player(world.db, world.admin, card_number=f"CARD{world.tag}",
                                     full_name="Player", birth_date=dt.date(1990, 1, 1),
                                     settings=world.store, agent_id=None)
    world.store.set("wallet.withdrawals_enabled", "false", world.admin.id)
    try:
        with pytest.raises(LockedState):
            players.counter_cash(world.db, world.admin, player_id=p.id, amount_cents=5000,
                                 direction="out", settings=world.store)
    finally:
        world.store.set("wallet.withdrawals_enabled", "true", world.admin.id)


def test_fixed_odds_event_works(world):
    from app.db.models import EventStatus, PayoutModel
    import datetime as dt
    ev = events.create_event(world.db, world.admin, title=f"Fixed {world.tag}", event_date=dt.date.today(),
                             settings=world.store, fight_count=1, payout_model=PayoutModel.FIXED)
    events.set_event_status(world.db, world.admin, ev.id, EventStatus.OPEN)
    g = events.fights_of(world.db, ev.id)[0]
    t = sell(world, g, Side.MERON, 10000)
    assert t.est_payout_cents == 19000
    run = declare(world, g, Result.MERON)
    assert run.totals["payout_total"] == 19000


def test_ledger_stays_balanced(world):
    g = world.games[0]
    sell(world, g, Side.MERON, 10000); sell(world, g, Side.WALA, 10000); sell(world, g, Side.DRAW, 2000)
    declare(world, g, Result.DRAW)
    report = reports.integrity(world.db)
    assert report["ok"], report
