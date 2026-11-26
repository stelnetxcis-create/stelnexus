#!/usr/bin/env python3
"""Measure Scatter: return to player, hit rate, bonus frequency, volatility.

    python scripts/scatter_sim.py                 # 1,000,000 rounds
    python scripts/scatter_sim.py -n 5000000 -s 7 # more rounds, fixed seed
    python scripts/scatter_sim.py -n 2000000 -j 4 # four worker processes

The engine has no dependencies, so this runs anywhere Python does. Change the weights or the pay table in
``app/games/scatter_engine.py`` and re-run to see what it did to the numbers. The standard error of the RTP is
printed: a high-volatility game needs millions of rounds before the second decimal means anything.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from multiprocessing import Pool

import importlib.util  # noqa: E402

# Load the engine by path: importing it as ``app.games.scatter_engine`` would run app/games/__init__.py and drag
# in the whole web stack (SQLAlchemy and friends), which a simulation has no use for.
_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "games", "scatter_engine.py")
_spec = importlib.util.spec_from_file_location("scatter_engine", _path)
eng = importlib.util.module_from_spec(_spec)
sys.modules["scatter_engine"] = eng
_spec.loader.exec_module(eng)

EDGES = (1, 2, 5, 10, 20, 50, 100, 500, 1000, 5000)          # x the bet; bucket 0 is "nothing", 1 is "under 1x"


def run(args: tuple[int, int, int]) -> dict:
    n, seed, cap = args
    rng = random.Random(seed)
    out = {"n": n, "paid": 0, "sq": 0, "hits": 0, "bonus": 0, "bonus_paid": 0, "base_paid": 0, "capped": 0,
           "max": 0, "retrig": 0, "tumbles": 0, "buckets": [0] * (len(EDGES) + 2), "loss_dressed_as_win": 0}
    for _ in range(n):
        r = eng.play(rng, max_win_x=cap)
        w = r["win"]
        out["paid"] += w
        out["sq"] += w * w
        out["max"] = max(out["max"], w)
        out["tumbles"] += sum(len(s["tumbles"]) for s in r["spins"])
        if w > 0:
            out["hits"] += 1
        if 0 < w < 100:
            out["loss_dressed_as_win"] += 1
        if r["capped"]:
            out["capped"] += 1
        b = r["bonus"]
        if b:
            out["bonus"] += 1
            out["bonus_paid"] += b["win"] + eng.scatter_pay(r["spins"][0]["scatters"])
            out["retrig"] += b["retriggers"]
        x = w / 100
        i = 0 if w == 0 else 1
        while i - 1 < len(EDGES) and w > 0 and x >= EDGES[i - 1]:
            i += 1
        out["buckets"][i] += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=1_000_000, help="rounds to play")
    ap.add_argument("-s", type=int, default=1, help="seed")
    ap.add_argument("-j", type=int, default=1, help="worker processes")
    ap.add_argument("--cap", type=int, default=eng.DEFAULT_MAX_WIN_X, help="max win, x the bet")
    a = ap.parse_args()
    per = a.n // a.j
    t0 = time.time()
    jobs = [(per, a.s * 1000 + i, a.cap) for i in range(a.j)]
    parts = Pool(a.j).map(run, jobs) if a.j > 1 else [run(jobs[0])]
    tot = {k: sum(p[k] for p in parts) for k in parts[0] if k not in ("buckets", "max")}
    tot["max"] = max(p["max"] for p in parts)
    buckets = [sum(p["buckets"][i] for p in parts) for i in range(len(EDGES) + 2)]
    n = tot["n"]
    mean = tot["paid"] / n / 100
    var = tot["sq"] / n / 10000 - mean * mean
    sd = math.sqrt(max(var, 0))
    se = sd / math.sqrt(n)
    print(f"rounds            {n:,}   ({time.time() - t0:.0f}s, seed {a.s}, cap {a.cap}x)")
    print(f"RTP               {mean * 100:.2f}%   +/- {1.96 * se * 100:.2f} (95% CI)")
    print(f"  base game       {(tot['paid'] - tot['bonus_paid']) / n:.2f}%")
    print(f"  bonus           {tot['bonus_paid'] / n:.2f}%")
    print(f"hit rate          {tot['hits'] / n * 100:.2f}%   (rounds that paid anything)")
    print(f"  paid < bet      {tot['loss_dressed_as_win'] / n * 100:.2f}%   (a 'win' that is still a loss)")
    print(f"bonus every       {n / max(tot['bonus'], 1):.0f} rounds   ({tot['bonus']:,} bonuses, {tot['retrig']:,} retriggers)")
    if tot["bonus"]:
        print(f"avg bonus pays    {tot['bonus_paid'] / tot['bonus'] / 100:.1f}x")
    print(f"tumbles / round   {tot['tumbles'] / n:.2f}")
    print(f"std dev           {sd:.2f}x   max seen {tot['max'] / 100:.0f}x   hit the cap {tot['capped']:,} times")
    print("distribution (x the bet):")
    labels = ["no win", f"under {EDGES[0]}x"] + [f"{EDGES[i]}-{EDGES[i + 1]}x" for i in range(len(EDGES) - 1)] + [f"{EDGES[-1]}x+"]
    for label, count in zip(labels, buckets):
        print(f"  {label:>10}  {count / n * 100:8.3f}%  1 in {n / count:,.0f}" if count else f"  {label:>10}  {0:8.3f}%")


if __name__ == "__main__":
    main()
