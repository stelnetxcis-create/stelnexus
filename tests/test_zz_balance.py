"""The pool must pay for itself: whatever the split, the cuts, the caps and the minimum odds, the winners are paid
out of the players' own stakes. The house never pays out of pocket, and the books always balance."""
import random

import pytest

from app.db.models import Result, Side, Ticket
from tests.test_features import declare, sell, tickets


@pytest.mark.parametrize("seed", range(6))
def test_pool_always_pays_for_itself(world, seed):
    rnd = random.Random(seed)
    w = world
    w.store.set("payout.platform_cut_bps", "500", w.admin.id)
    w.store.set("payout.house_cut_bps", "500", w.admin.id)
    w.store.set("payout.min_odds_bps", "15000", w.admin.id)
    w.store.set("payout.max_llamado_bps", "25000", w.admin.id)
    w.store.set("payout.max_dehado_bps", "25000", w.admin.id)
    for g in w.games:
        meron = wala = 0
        for _ in range(rnd.randint(2, 7)):
            side = rnd.choice([Side.MERON, Side.WALA])
            amt = rnd.randint(2000, 90000)
            sell(w, g, side, amt)
            meron += amt if side == Side.MERON else 0
            wala += amt if side == Side.WALA else 0
        if not meron or not wala:                       # a side nobody bet: a different rule, covered elsewhere
            continue
        draw = rnd.choice([0, 0, 5000])
        if draw:
            sell(w, g, Side.DRAW, draw)
        res = rnd.choice([Result.MERON, Result.WALA])
        t = declare(w, g, res).totals
        stakes = sum(x.stake_cents for x in tickets(w, g))
        assert stakes == meron + wala + draw
        # money in = money out + what the house kept, to the centavo
        assert t["payout_total"] + t["refund_total"] + t["house_take"] == stakes
        if t["auto_cancel"]:
            assert t["refund_total"] == stakes and t["payout_total"] == 0 and t["house_take"] == 0
            continue
        # the winners are paid from the players' own stakes: never more than the net pool, never a loss to the house
        assert t["payout_total"] <= t["net_pool"] <= meron + wala
        assert t["house_take"] >= 0
        # both cuts are taken from the pool, and what the house kept is at least those cuts (plus lost Draw stakes)
        assert t["house_cut"] + t["platform_cut"] + t["net_pool"] == meron + wala
        # every winner got between 1.50x and 2.50x of the stake (the cap rounds down to the centavo)
        win = meron if res == Result.MERON else wala
        mult = t["payout_total"] / win
        assert 1.5 - 1e-3 <= mult <= 2.5 + 1e-3, (meron, wala, res, mult)
