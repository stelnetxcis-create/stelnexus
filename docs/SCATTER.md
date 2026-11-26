# Scatter (the slot)

A 6 x 5 scatter-pays tumble slot with free spins and multiplier orbs, in the Games hub next to Sabong and Bola.

## How it plays
* 8 or more of the same symbol **anywhere** pays (tiers 8-9 / 10-11 / 12+). Winners burst, the rest fall, new symbols drop in, repeat.
* **Scatter** pays on its own (4 = 3x, 5 = 5x, 6+ = 100x the bet) and 4+ start **10 free spins**; 3+ inside the bonus add 5 (40 spins max).
* **Multiplier orbs** (x2 to x100): when a spin ends with a win the orbs on the board are added up and multiply that spin. In free spins they are banked for the rest of the bonus.
* One round (spin + any free spins) is resolved in ONE request and never pays more than `scatter.max_win_x` (default 5000x).

## Where things live
| Piece | File |
|---|---|
| Maths (pure Python, no imports from the app) | `app/games/scatter_engine.py` |
| Simulator: RTP, hit rate, bonus frequency, volatility | `scripts/scatter_sim.py` |
| Money: validation, ledger postings, round record | `app/services/scatter.py` |
| Hub / Records adapter | `app/games/scatter.py` |
| Routes (`GET /scatter`, `POST /scatter/spin`) | `app/web/routes_scatter.py` |
| Page, art, animation, sound | `templates/scatter.html`, `static/js/scatter-art.js`, `scatter.js`, `css/scatter.css` |
| Table | `ScatterRound` in `app/db/models.py` (created by the normal schema upgrade) |

## Money
Both postings and the round row are one transaction:

    spin   wallet +stake | sys:scatter -stake    kind scatter.stake
    win    sys:scatter +win | wallet -win        kind scatter.win     (only if the round paid)

`sys:scatter` is its own house account (HOUSE_REVENUE, may go negative) created on first use, so it rolls into the
Cashflow "house revenue" and the game's result is a single balance. The account daily bet limit counts Scatter stakes.

## Fairness and audit
The browser never decides anything: it receives the finished round and plays it back. Every round stores a random
`seed`; `scatter.verify(round)` (or `scatter_engine.play_seeded(seed, max_win_x=...)`) re-creates it exactly. The
full replay JSON is kept for rounds that bonus or pay 10x+; every round keeps its seed.

## Return to player
Measured by `scripts/scatter_sim.py` (3.5M base rounds + 90k direct bonus sessions): **96.5% +/- 0.6** (base game
59.2%, bonus 37.3%), a win on 19.8% of rounds, a bonus about once in 259 rounds averaging ~96x, std dev ~13x.
**Re-run the simulator after changing any weight or pay in the engine**, and update `MEASURED_RTP`. A win smaller than
the bet is shown quietly ("Returned"), never celebrated: it is not a win.

## Settings (Admin Panel > Variables > Scatter)
`scatter.enabled` (master switch, default ON), `scatter.bet_ladder`, `scatter.min_bet_cents`, `scatter.max_bet_cents`,
`scatter.max_win_x`, `scatter.min_seconds_between_spins`. Rights: `scatter.view`, `scatter.play`, `scatter.report`
(Agents, Sub Agents and Players get view + play by default; Admin/Master get all).

## Deploying
Run `npm run build` (the build-freshness test fails until you do). Confirm you are licensed to offer slot games
before enabling it for real money.
