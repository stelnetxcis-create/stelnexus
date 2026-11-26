"""Scatter's maths, on its own: deterministic, self-consistent, and inside the return-to-player it advertises."""
import random


from app.games import scatter_engine as eng


def _final_grid(spin):
    """Replay a spin the way the browser does (remove winners, drop survivors, fill from the top)."""
    grid = [col[:] for col in spin["grid"]]
    for t in spin["tumbles"]:
        gone = {(c, r) for w in t["wins"] for c, r in w["cells"]}
        for c in range(eng.COLS):
            keep = [grid[c][r] for r in range(eng.ROWS) if (c, r) not in gone]
            assert len(t["fill"][c]) == eng.ROWS - len(keep), "fill must replace exactly what was removed"
            grid[c] = t["fill"][c] + keep
    return grid


def test_same_seed_same_round_different_seed_different_round():
    assert eng.play_seeded("abc") == eng.play_seeded("abc")
    assert eng.play_seeded("abc") != eng.play_seeded("abd")


def test_every_round_is_internally_consistent():
    rng = random.Random(2026)
    for _ in range(4000):
        r = eng.play(rng)
        spins = r["spins"]
        assert 0 <= r["win"] <= eng.DEFAULT_MAX_WIN_X * 100
        total = sum(s["win"] for s in spins) + (spins[0].get("scatter_pay") or 0)
        assert r["win"] == (eng.DEFAULT_MAX_WIN_X * 100 if r["capped"] else total)
        assert (r["bonus"] is None) == (len(spins) == 1)
        for s in spins:
            assert len(s["grid"]) == eng.COLS and all(len(c) == eng.ROWS for c in s["grid"])
            assert s["raw"] == sum(t["win"] for t in s["tumbles"])
            for t in s["tumbles"]:
                for w in t["wins"]:
                    assert w["count"] == len(w["cells"]) >= eng.MIN_CLUSTER
                    assert w["pay"] == eng.pay_for(w["sym"], w["count"]) > 0
            final = _final_grid(s)
            for sym in range(eng.PAY_SYMBOLS):                       # a spin only ends when nothing pays
                assert sum(col.count(sym) for col in final) < eng.MIN_CLUSTER
            assert s["scatters"] == sum(col.count(eng.SCATTER) for col in final)
        if r["bonus"]:
            assert spins[0]["scatters"] >= eng.SCATTERS_TO_TRIGGER
            assert r["bonus"]["spins"] == len(spins) - 1 <= eng.FREE_SPINS_MAX


def test_multiplier_rules():
    rng = random.Random(7)
    seen_base = seen_free = 0
    for _ in range(30000):
        r = eng.play(rng)
        for s in r["spins"]:
            if s["raw"] == 0:
                assert s["win"] == 0 and not s["collected"]
            elif s["mode"] == "base":
                assert s["factor"] == max(1, s["orb_sum"]) and s["win"] == s["raw"] * s["factor"]
                seen_base += s["factor"] > 1
            else:
                assert s["factor"] == max(1, s["mult"]) and s["win"] == s["raw"] * s["factor"]
                seen_free += s["factor"] > 1
    assert seen_base and seen_free


def test_the_cap_is_a_hard_ceiling():
    rng = random.Random(11)
    for _ in range(20000):
        assert eng.play(rng, max_win_x=3)["win"] <= 300


def test_pay_tables_only_go_up():
    for sym, (a, b, c) in eng.PAYTABLE.items():
        assert 0 < a < b < c, sym
    assert eng.pay_for(0, 7) == 0 and eng.pay_for(0, 8) == eng.PAYTABLE[0][0] and eng.pay_for(0, 12) == eng.PAYTABLE[0][2]
    assert [eng.scatter_pay(n) for n in (3, 4, 5, 6, 9)] == [0, 300, 500, 10000, 10000]


def test_return_to_player_is_where_it_says_it_is():
    """Stratified (base spins + bonus sessions) with fixed seeds, so it is deterministic and quick. The bounds are
    loose on purpose: this catches a broken edit to the weights or pay table, not a 0.3% drift (use
    scripts/scatter_sim.py for that)."""
    rng = random.Random(99)
    n = 90_000
    base_paid = trig = scat = 0
    for _ in range(n):
        s, _m = eng._spin(rng, "base", 0)
        base_paid += s["win"]
        if s["scatters"] >= eng.SCATTERS_TO_TRIGGER:
            trig += 1
            scat += eng.scatter_pay(s["scatters"])
    sessions = 2500
    bonus = sum(eng.run_bonus(rng, eng.DEFAULT_MAX_WIN_X * 100, 1)[2] for _ in range(sessions)) / sessions
    rtp = base_paid / n + (trig / n) * (bonus + scat / max(trig, 1))          # all x100 -> percent
    assert 88 <= rtp <= 105, rtp
    assert abs(eng.MEASURED_RTP - 96.5) < 1e-9 or 88 <= eng.MEASURED_RTP <= 105
    assert 150 <= n / max(trig, 1) <= 400, "the bonus should land about once in 260 rounds"
