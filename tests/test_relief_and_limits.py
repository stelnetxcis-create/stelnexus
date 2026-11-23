"""Minimum-odds relief, bet-size limits, Draw limits, big-bet alert and the one-time settings rollout."""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select, text

from app.core.errors import ValidationError
from app.db.models import AppSetting, Notification, Result, Role, Side, Ticket, TicketStatus, User
from app.db.migrate import _roll_out_payout_limits
from app.services import agents, betting, ledger, players, settlement
from tests.conftest import PASSWORD, load_credit


def put(w, key, value):
    w.store.set(key, str(value), w.admin.id)


def sell(w, game, side, cents, player=None):
    return betting.place_bet(w.db, w.admin, fight_id=game.id, side=side, stake_cents=cents, settings=w.store,
                             player_id=(player or w.player).id)


def declare(w, game, result):
    return settlement.declare(w.db, w.admin, fight_id=game.id, result=result, settings=w.store)


@pytest.fixture()
def pool(world):
    """5% platform cut + 5% tong (9.75% combined), floor 1.30x, no ceiling, pool-share cap off."""
    put(world, "payout.platform_cut_bps", 500)
    put(world, "payout.house_cut_bps", 500)
    put(world, "payout.min_odds_bps", 13000)
    put(world, "payout.min_odds_mode", "relief_all")
    put(world, "payout.max_bet_pool_share_bps", 0)
    return world


def tickets(w, game):
    return list(w.db.scalars(select(Ticket).where(Ticket.fight_id == game.id).order_by(Ticket.id)))


# ------------------------------------------------------------------ relief
def test_no_relief_when_the_normal_multiple_is_already_above_the_floor(pool):
    g = pool.games[0]
    sell(pool, g, Side.MERON, 600_000); sell(pool, g, Side.WALA, 400_000)           # 60/40 -> 1.504x
    t = declare(pool, g, Result.MERON).totals
    assert t["relief"] == 0 and t["auto_cancel"] is False
    assert t["platform_cut"] == 50_000 and t["house_cut"] == 47_500 and t["net_pool"] == 902_500


def test_relief_lowers_the_tong_just_enough_to_pay_the_floor(pool):
    g = pool.games[0]
    sell(pool, g, Side.MERON, 720_000); sell(pool, g, Side.WALA, 280_000)           # Meron holds 72%: 1.25x naturally
    run = declare(pool, g, Result.MERON)
    t = run.totals
    assert t["auto_cancel"] is False
    assert t["net_pool"] == 936_000 and t["relief"] == 33_500                         # exactly 1.30x for the winners
    assert t["platform_cut"] == 50_000 and t["house_cut"] == 14_000                  # tong gave up 33,500; platform untouched
    meron = [x for x in tickets(pool, g) if x.side == Side.MERON][0]
    assert meron.payout_cents == 936_000 and meron.status == TicketStatus.SETTLED
    assert t["house_take"] == 64_000 and t["payout_total"] + t["house_take"] == 1_000_000
    assert ledger.verify_integrity(pool.db)["ok"]


def test_relief_spills_into_the_platform_cut_when_the_tong_is_not_enough(pool):
    g = pool.games[0]
    sell(pool, g, Side.MERON, 740_000); sell(pool, g, Side.WALA, 260_000)           # 74%
    t = declare(pool, g, Result.MERON).totals
    assert t["net_pool"] == 962_000 and t["house_cut"] == 0 and t["platform_cut"] == 38_000
    assert t["payout_total"] == 962_000 and t["house_take"] == 38_000
    assert t["relief"] == 962_000 - 902_500


def test_tong_only_mode_keeps_the_platform_cut_and_refunds_sooner(pool):
    put(pool, "payout.min_odds_mode", "relief_tong")
    g = pool.games[0]
    sell(pool, g, Side.MERON, 740_000); sell(pool, g, Side.WALA, 260_000)           # needs more than the tong can give
    run = declare(pool, g, Result.MERON)
    assert run.totals["auto_cancel"] is True
    assert all(x.status == TicketStatus.REFUNDED for x in tickets(pool, g))


def test_refund_when_even_the_whole_pool_cannot_reach_the_floor(pool):
    g = pool.games[0]
    sell(pool, g, Side.MERON, 800_000); sell(pool, g, Side.WALA, 200_000)           # 80% -> would need 1.04 x the pool
    run = declare(pool, g, Result.MERON)
    assert run.totals["auto_cancel"] is True and run.totals["house_take"] == 0
    assert all(x.status == TicketStatus.REFUNDED and x.payout_cents == x.stake_cents for x in tickets(pool, g))


def test_refund_mode_is_the_old_behaviour(pool):
    put(pool, "payout.min_odds_mode", "refund")
    g = pool.games[0]
    sell(pool, g, Side.MERON, 720_000); sell(pool, g, Side.WALA, 280_000)
    assert declare(pool, g, Result.MERON).totals["auto_cancel"] is True


def test_relief_only_applies_when_the_heavy_side_wins(pool):
    g = pool.games[0]
    sell(pool, g, Side.MERON, 720_000); sell(pool, g, Side.WALA, 280_000)
    t = declare(pool, g, Result.WALA).totals                                          # the light side wins: normal payout
    assert t["relief"] == 0 and t["auto_cancel"] is False and t["net_pool"] == 902_500
    wala = [x for x in tickets(pool, g) if x.side == Side.WALA][0]
    assert wala.payout_cents == 902_500


def test_live_multiple_and_estimate_match_what_settlement_pays(pool):
    g = pool.games[0]
    sell(pool, g, Side.MERON, 720_000); sell(pool, g, Side.WALA, 280_000)
    snap = betting.pool_snapshot(pool.db, g)
    mult = betting.side_multipliers(snap, 975, pool.store, None, house_bps=500)
    assert mult["meron"]["bps"] == 13000 and mult["meron"]["relief"] is True and mult["meron"]["below_min"] is False
    assert mult["wala"]["relief"] is False
    assert declare(pool, g, Result.MERON).totals["net_pool"] * 10000 // 720_000 == 13000


def test_corrected_result_applies_relief_again(pool):
    g = pool.games[0]
    sell(pool, g, Side.MERON, 720_000); sell(pool, g, Side.WALA, 280_000)
    declare(pool, g, Result.WALA)
    master = agents.create_agent(pool.db, pool.admin, username=f"rm{pool.tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="M", settings=pool.store)
    run = settlement.request_correction(pool.db, pool.admin, fight_id=g.id, result=Result.MERON,
                                        reason="result was wrong", settings=pool.store)
    run = settlement.approve(pool.db, master, run_id=run.id, settings=pool.store)
    assert run.totals["relief"] == 33_500 and run.totals["net_pool"] == 936_000
    assert ledger.verify_integrity(pool.db)["ok"]


# ------------------------------------------------------------------ bet size limits
def test_bet_cannot_be_a_big_share_of_the_pool_but_small_bets_always_pass(world):
    put(world, "payout.max_bet_pool_share_bps", 1000)                                 # 10%
    put(world, "payout.max_bet_pool_floor_cents", 200_000)                            # always allowed up to 2,000
    g = world.games[0]
    sell(world, g, Side.MERON, 200_000)                                               # empty pool, at the floor: fine
    with pytest.raises(ValidationError, match="too big"):
        sell(world, g, Side.WALA, 200_001)                                            # over the floor and over 10% of 2,000
    put(world, "payout.max_bet_pool_share_bps", 0)
    for _ in range(4):
        sell(world, g, Side.WALA, 1_000_000)                                          # build a 42,000 pool
    put(world, "payout.max_bet_pool_share_bps", 1000)
    sell(world, g, Side.MERON, 420_000)                                               # exactly 10% of 4,200,000: allowed
    with pytest.raises(ValidationError, match="The most you can bet"):
        sell(world, g, Side.MERON, 500_000)


def test_pool_share_rule_can_be_switched_off(world):
    put(world, "payout.max_bet_pool_share_bps", 0)
    sell(world, world.games[0], Side.MERON, 900_000)                                  # 100% of an empty pool: allowed


def test_default_maximum_bet_is_10000_pesos(world):
    from app.core.settings_registry import BY_KEY
    assert BY_KEY["payout.max_stake_cents"].default == 1_000_000
    put(world, "payout.max_bet_pool_share_bps", 0)
    with pytest.raises(ValidationError):
        sell(world, world.games[0], Side.MERON, 1_000_001)


# ------------------------------------------------------------------ Draw limits
def test_draw_ticket_and_draw_total_are_limited(world):
    put(world, "payout.draw_max_stake_cents", 100_000)                                # 1,000 per ticket
    put(world, "payout.draw_max_exposure_cents", 500_000)                             # 5,000 per fight
    g = world.games[0]
    with pytest.raises(ValidationError, match="at most"):
        sell(world, g, Side.DRAW, 100_001)
    for _ in range(5):
        sell(world, g, Side.DRAW, 100_000)
    with pytest.raises(ValidationError, match="Draw is full"):
        sell(world, g, Side.DRAW, 100_000)
    sell(world, world.games[1], Side.DRAW, 100_000)                                   # another fight has its own room


def test_draw_limits_can_be_switched_off_and_draw_still_pays_8x(world):
    put(world, "payout.draw_max_stake_cents", 0)
    put(world, "payout.draw_max_exposure_cents", 0)
    put(world, "payout.max_bet_pool_share_bps", 0)
    g = world.games[0]
    sell(world, g, Side.MERON, 100_000); sell(world, g, Side.WALA, 100_000); sell(world, g, Side.DRAW, 500_000)
    declare(world, g, Result.DRAW)
    by = {x.side.value: x.payout_cents for x in tickets(world, g)}
    assert by == {"meron": 100_000, "wala": 100_000, "draw": 4_000_000}


def test_worst_case_draw_loss_is_bounded_by_the_limits(world):
    put(world, "payout.draw_max_stake_cents", 100_000)
    put(world, "payout.draw_max_exposure_cents", 500_000)
    put(world, "payout.max_bet_pool_share_bps", 0)
    g = world.games[0]
    sell(world, g, Side.MERON, 100_000); sell(world, g, Side.WALA, 100_000)
    for _ in range(5):
        sell(world, g, Side.DRAW, 100_000)
    t = declare(world, g, Result.DRAW).totals
    assert t["house_take"] == -3_500_000                                              # 7 x 5,000: the most the house can lose


# ------------------------------------------------------------------ big-bet alert
def test_big_bet_alert_notifies_the_master_and_is_logged_but_never_blocks(world):
    put(world, "payout.max_bet_pool_share_bps", 0)
    master = agents.create_agent(world.db, world.admin, username=f"bm{world.tag}", password=PASSWORD, role=Role.MASTER,
                                 full_name="M", settings=world.store)
    p = players.register_online_player(world.db, world.admin, username=f"bp{world.tag}", password=PASSWORD,
                                       full_name="Big Bettor", birth_date=dt.date(1990, 1, 1),
                                       gcash_number="09171234568", settings=world.store, agent_id=master.id)
    p.kyc_verified = True
    load_credit(world.db, world.admin, world.store, p, 5_000_000)
    g = world.games[0]
    def big_notes():
        return world.db.scalars(select(Notification).where(Notification.user_id == master.id,
                                                           Notification.message.like("Big bet:%"))).all()
    small = sell(world, g, Side.MERON, 499_999, player=p)
    assert big_notes() == []                                                          # just under the threshold
    big = sell(world, g, Side.WALA, 500_000, player=p)                                # at the threshold
    notes = big_notes()
    assert len(notes) == 1 and "WALA" in notes[0].message
    assert big.status == TicketStatus.ACTIVE and small.status == TicketStatus.ACTIVE
    from app.db.models import AuditLog
    logged = world.db.scalars(select(AuditLog).where(AuditLog.action == "bet.big")).all()
    assert any(a.entity_id == str(big.id) for a in logged) and not any(a.entity_id == str(small.id) for a in logged)


def test_big_bet_alert_can_be_switched_off(world):
    put(world, "payout.max_bet_pool_share_bps", 0)
    put(world, "payout.big_bet_alert_cents", 0)
    before = world.db.scalar(select(__import__("sqlalchemy").func.count()).select_from(Notification))
    sell(world, world.games[0], Side.MERON, 900_000)
    assert world.db.scalar(select(__import__("sqlalchemy").func.count()).select_from(Notification)) == before


# ------------------------------------------------------------------ one-time rollout of the new defaults
def _settings_db(rows):
    engine = create_engine("sqlite://")
    AppSetting.__table__.create(engine)
    with engine.begin() as conn:
        for k, v in rows:
            conn.execute(text("INSERT INTO app_settings (key, value, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)"),
                         {"k": k, "v": v})
    return engine


def _value(engine, key):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).first()
    return row[0] if row else None


def test_rollout_moves_old_defaults_once_and_leaves_chosen_values_alone():
    engine = _settings_db([("payout.min_odds_bps", "15000"), ("payout.max_stake_cents", "5000000")])
    done = _roll_out_payout_limits(engine)
    assert len(done) == 2
    assert _value(engine, "payout.min_odds_bps") == "13000" and _value(engine, "payout.max_stake_cents") == "1000000"
    assert _value(engine, "payout.min_odds_mode") == "relief_all"
    # an admin puts the old numbers back later: the rollout must never touch them again
    with engine.begin() as conn:
        conn.execute(text("UPDATE app_settings SET value = '15000' WHERE key = 'payout.min_odds_bps'"))
    assert _roll_out_payout_limits(engine) == []
    assert _value(engine, "payout.min_odds_bps") == "15000"

    chosen = _settings_db([("payout.min_odds_bps", "14000"), ("payout.max_stake_cents", "2500000")])
    assert _roll_out_payout_limits(chosen) == []                                      # not the old defaults: kept
    assert _value(chosen, "payout.min_odds_bps") == "14000" and _value(chosen, "payout.max_stake_cents") == "2500000"


def test_rollout_on_an_empty_database_is_harmless():
    engine = _settings_db([])
    assert _roll_out_payout_limits(engine) == []
    assert _roll_out_payout_limits(engine) == []
