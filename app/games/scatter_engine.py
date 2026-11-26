"""Scatter: the game engine. Pure Python, no imports from the rest of the app.

Keeping the maths in one dependency-free file means it can be simulated, audited and replayed on its own
(``scripts/scatter_sim.py``), and the web layer can never "adjust" an outcome: it asks for a round and pays
exactly what the round says.

The game
--------
A 6 x 5 grid. A symbol pays **anywhere** on the grid once 8 or more of it are showing (no lines, no
adjacency). Every winning symbol is removed, the survivors fall, new symbols drop in from the top, and the
grid is checked again: a *tumble*. A spin ends when a tumble finds no win.

    pay symbols   4 gems (low) + moon, comet, sun, crown (high)         pay tiers: 8-9 / 10-11 / 12+
    SCATTER       pays on its own and starts the bonus (see below)       never removed by a tumble
    MULTIPLIER    an orb with a value; never part of a cluster           never removed by a tumble

    base game     when a spin ends with a win, the orbs on the grid are ADDED UP and multiply that spin's win
                  (no orbs = x1).
    4+ scatters   at the end of a spin: scatter pay (4 = 3x, 5 = 5x, 6+ = 100x the bet) and 10 free spins.
    free spins    orbs are collected into a multiplier that STAYS for the rest of the bonus and multiplies every
                  later win. 3+ scatters during free spins add 5 more spins.

Money is integer. A win is stored as ``x100`` -- hundredths of the bet -- so ``win_cents = bet * x100 // 100``.
One round (the base spin plus any free spins it triggered) is resolved in one go and returned as plain JSON:
the browser only *plays back* the sequence, it never decides anything.

Cells are ints: ``0..7`` pay symbols, ``8`` scatter, ``>= 100`` a multiplier orb worth ``cell - 100``.
"""
from __future__ import annotations

import random
from bisect import bisect_left
from itertools import accumulate

ENGINE_VERSION = 1

# Measured with scripts/scatter_sim.py (3.5M base rounds + 90k direct bonus sessions): return to player 96.5%
# (base game 59.2% + bonus 37.3%, +/- 0.6 at 95%), a win on 19.8% of rounds, a bonus once in ~259 rounds
# averaging ~96x the bet, standard deviation ~13x. Re-run the script after touching ANY number below.
MEASURED_RTP = 96.5

COLS, ROWS = 6, 5
CELLS = COLS * ROWS
MIN_CLUSTER = 8

SCATTER = 8
ORB = 100                      # cell = ORB + value
SYMBOL_NAMES = ("ruby", "emerald", "sapphire", "amethyst", "moon", "comet", "sun", "crown", "scatter")
PAY_SYMBOLS = 8

# Pay table, x100 of the TOTAL bet, by cluster size: (8-9, 10-11, 12+).
PAYTABLE: dict[int, tuple[int, int, int]] = {
    0: (20, 50, 150),        # ruby      low
    1: (30, 75, 175),        # emerald
    2: (40, 90, 220),        # sapphire
    3: (60, 120, 290),       # amethyst
    4: (75, 180, 540),       # moon      high
    5: (110, 360, 1100),     # comet
    6: (180, 720, 2200),     # sun
    7: (700, 1800, 7200),    # crown
}
SCATTER_PAY = {4: 300, 5: 500, 6: 10000}      # x100 of the bet for 4, 5, 6+ scatters
FREE_SPINS_AWARD = 10
FREE_SPINS_RETRIGGER = 5
FREE_SPINS_RETRIGGER_AT = 3
FREE_SPINS_MAX = 40            # total spins in one bonus, retriggers included
SCATTERS_TO_TRIGGER = 4
MAX_TUMBLES = 60               # a hard stop; real rounds end after a handful

# Draw weights per cell. The three dicts below are what the RTP is tuned with (see scripts/scatter_sim.py).
SYMBOL_WEIGHTS = {
    "base": {0: 1000, 1: 980, 2: 960, 3: 940, 4: 900, 5: 860, 6: 820, 7: 780},
    # the bonus is "richer": fewer low gems and more of the high symbols, so big clusters land more often
    "free": {0: 766, 1: 751, 2: 735, 3: 720, 4: 1134, 5: 1084, 6: 1033, 7: 983},
}
SCATTER_WEIGHT = {"base": 150, "free": 150}
ORB_WEIGHT = {"base": 70, "free": 280}
ORB_VALUES = ((2, 3600), (3, 2600), (4, 1700), (5, 1000), (6, 600), (8, 330), (10, 240),
              (12, 120), (15, 60), (20, 30), (25, 14), (50, 4), (100, 1))

DEFAULT_MAX_WIN_X = 5000       # x the bet; the operator can lower or raise it (settings: scatter.max_win_x)


def _table(mode: str) -> tuple[list[int], list[int]]:
    """(cumulative weights, cell for each slot) for ``mode`` = base | free."""
    cells: list[int] = []
    weights: list[int] = []
    for sym, w in SYMBOL_WEIGHTS[mode].items():
        cells.append(sym)
        weights.append(w)
    cells.append(SCATTER)
    weights.append(SCATTER_WEIGHT[mode])
    orb_total = ORB_WEIGHT[mode]
    value_sum = sum(w for _, w in ORB_VALUES)
    for value, w in ORB_VALUES:
        cells.append(ORB + value)
        weights.append(orb_total * w / value_sum)
    return list(accumulate(weights)), cells


_TABLES = {mode: _table(mode) for mode in ("base", "free")}


def _draw(rng, mode: str) -> int:
    cum, cells = _TABLES[mode]
    return cells[bisect_left(cum, rng.random() * cum[-1])]


def _tier(count: int) -> int:
    return 0 if count < 10 else (1 if count < 12 else 2)


def pay_for(symbol: int, count: int) -> int:
    """x100 of the bet that ``count`` of ``symbol`` pays (0 below the minimum cluster)."""
    return PAYTABLE[symbol][_tier(count)] if count >= MIN_CLUSTER else 0


def scatter_pay(count: int) -> int:
    if count < SCATTERS_TO_TRIGGER:
        return 0
    return SCATTER_PAY[min(count, 6)]


def new_seed() -> str:
    import secrets
    return secrets.token_hex(16)


def _spin(rng, mode: str, mult_in: int) -> tuple[dict, int]:
    """One spin: the opening grid, every tumble, and what is left standing. Returns (spin, multiplier after)."""
    grid = [[_draw(rng, mode) for _ in range(ROWS)] for _ in range(COLS)]      # grid[col][row], row 0 = top
    start = [col[:] for col in grid]
    tumbles: list[dict] = []
    raw = 0                                                                    # x100, before any multiplier

    while len(tumbles) < MAX_TUMBLES:
        counts: dict[int, list[tuple[int, int]]] = {}
        for c in range(COLS):
            for r in range(ROWS):
                cell = grid[c][r]
                if cell < PAY_SYMBOLS:
                    counts.setdefault(cell, []).append((c, r))
        wins = []
        for sym, cells in counts.items():
            if len(cells) >= MIN_CLUSTER:
                wins.append({"sym": sym, "count": len(cells), "pay": pay_for(sym, len(cells)),
                             "cells": [[c, r] for c, r in cells]})
        if not wins:
            break
        gone = {(c, r) for w in wins for c, r in w["cells"]}
        step_win = sum(w["pay"] for w in wins)
        raw += step_win
        fill: list[list[int]] = []
        for c in range(COLS):
            keep = [grid[c][r] for r in range(ROWS) if (c, r) not in gone]
            fresh = [_draw(rng, mode) for _ in range(ROWS - len(keep))]
            fill.append(fresh)                                                 # top to bottom
            grid[c] = fresh + keep
        tumbles.append({"wins": wins, "win": step_win, "fill": fill})

    scatters = sum(1 for c in range(COLS) for r in range(ROWS) if grid[c][r] == SCATTER)
    orbs = [{"c": c, "r": r, "v": grid[c][r] - ORB}
            for c in range(COLS) for r in range(ROWS) if grid[c][r] >= ORB]
    orb_sum = sum(o["v"] for o in orbs)

    mult = mult_in
    if raw > 0 and orbs:
        # base game: the orbs on the grid are the multiplier for this spin; free spins: they are banked for good
        mult = mult_in + orb_sum if mode == "free" else orb_sum
    factor = max(1, mult) if (mode == "free" or raw > 0) else 1
    win = raw * factor
    if mode == "base":
        mult = mult_in                                      # base spins never carry a multiplier forward
    spin = {"mode": mode, "grid": start, "tumbles": tumbles, "scatters": scatters, "orbs": orbs,
            "orb_sum": orb_sum if raw > 0 else 0, "raw": raw, "factor": factor if raw > 0 else 1,
            "win": win, "collected": bool(raw > 0 and orbs)}
    return spin, mult


def run_bonus(rng, room: int, first_index: int) -> tuple[list[dict], dict, int, bool]:
    """The free spins of one bonus. ``room`` is how much more (x100) the round may still win before its cap.
    Returns (spins, bonus facts, win x100, hit the cap)."""
    spins: list[dict] = []
    bonus = {"awarded": FREE_SPINS_AWARD, "retriggers": 0, "spins": 0, "win": 0, "mult": 0}
    left, mult, won = FREE_SPINS_AWARD, 0, 0
    capped = False
    while left > 0 and not capped:
        left -= 1
        spin, mult = _spin(rng, "free", mult)
        spin["index"] = first_index + len(spins)
        spin["mult"] = mult
        bonus["spins"] += 1
        if spin["scatters"] >= FREE_SPINS_RETRIGGER_AT and bonus["awarded"] + FREE_SPINS_RETRIGGER <= FREE_SPINS_MAX:
            left += FREE_SPINS_RETRIGGER
            bonus["awarded"] += FREE_SPINS_RETRIGGER
            bonus["retriggers"] += 1
            spin["retrigger"] = FREE_SPINS_RETRIGGER
        if won + spin["win"] >= room:
            spin["win"] = room - won
            spin["capped"] = capped = True
        won += spin["win"]
        bonus["win"] = won
        bonus["mult"] = mult
        spins.append(spin)
    return spins, bonus, won, capped


def play(rng, *, max_win_x: int = DEFAULT_MAX_WIN_X) -> dict:
    """Resolve one whole round. ``rng`` needs only ``.random()``. ``max_win_x`` caps the round at x the bet."""
    cap = int(max_win_x) * 100
    base, _ = _spin(rng, "base", 0)
    base["index"] = 0
    spins = [base]
    total = base["win"]
    bonus = None
    capped = False
    if base["scatters"] >= SCATTERS_TO_TRIGGER:
        base["scatter_pay"] = scatter_pay(base["scatters"])
        total += base["scatter_pay"]
    if total >= cap:
        total, capped = cap, True
    elif base["scatters"] >= SCATTERS_TO_TRIGGER:
        more, bonus, won, capped = run_bonus(rng, cap - total, 1)
        spins += more
        total += won
    return {"v": ENGINE_VERSION, "spins": spins, "bonus": bonus, "win": total, "capped": capped}


def play_seeded(seed: str, *, max_win_x: int = DEFAULT_MAX_WIN_X) -> dict:
    """The same round again from its stored seed: how an operator checks a disputed round."""
    return play(random.Random(seed), max_win_x=max_win_x)


def summary(result: dict) -> dict:
    """The small facts a records page needs, without the whole replay."""
    spins = result["spins"]
    tumbles = sum(len(s["tumbles"]) for s in spins)
    top = max((s["factor"] for s in spins), default=1)
    return {"spins": len(spins), "free_spins": (result.get("bonus") or {}).get("spins", 0),
            "tumbles": tumbles, "top_multiplier": top, "win_x100": result["win"],
            "capped": bool(result.get("capped"))}


def theoretical_note() -> str:
    return "Return to player is measured by simulation (scripts/scatter_sim.py), not derived by hand."
