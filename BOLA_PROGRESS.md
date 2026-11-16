# Bola progress handoff

Full suite: 188 passed (run `python -m pytest -q`). Feature is dark: `bola.enabled=false` by default.

## Done
- Phases 1-3 (earlier): foundations, `services/bola.py`, `services/bola_settlement.py`.
- Phase 4 commissions: `compute_bola_lines`, `persist_bola`, `mark_bola_reversed`, `list_bola_for`;
  `totals_for` / `claim_to_wallet` include Bola. Defaults: D3 SOP out of Cavo's cut, D4 Master remainder is a commission line.
- Commissions page lists Bola rows (Game column) ; `reports.bola_totals` + Bola cards on /cashflow.
- CLI: `bola-generate`, `bola-tick`, `bola-convert-cuts --rates file.csv [--pool 0.30] [--apply]`.
- Web (Phase 5, core): `app/web/routes_bola.py` registered in main.py; templates `bola_lobby/draw/ticket/results/betlogs.html`;
  `static/js/bola.js` (live slip preview); sidebar "Number game" group. Routes: lobby, results, draw page + preview JSON,
  place/cancel, ticket + QR, bet logs, create/generate/open/lock/void draw, declare, correct, approve/reject.
- Tests: `tests/test_bola.py` (34 tests): section 9 cases, CLI converter, web end-to-end over HTTP.

## Findings worth knowing
- Liability-cap boundary: a cap is "maximum allowed" (reaching it is fine). Plan text said 124 passes / 125 fails; code allows 125.
- A draw where the house loses money pays NO commission (cap is applied to house gross). Intended per plan, but confirm.
- Ticket header uses setting `ticket.header`; no "PCSO"/"OFFICIAL" anywhere (plan 0.1).

## Done since session 2
Admin schedules UI, JSON API, dashboard card, `/live` panel, daily/monthly reports + CSV, number grid,
`bola-import-legacy`, expenses UI, result photo on draws, dev-only gate (`BOLA_DEV_ONLY`), systemd timers in
`deploy/`, `bola-preflight` (run by `scripts/security_check.sh`), README section.
Full plan-by-plan status: `STELNEXUS_NUMBER_GAME_PROGRESS.md`.

## TODO (in order)
1. Rulings needed: D4 (Master remainder: commission line today, plan default was house); no commission on a
   losing draw; liability-cap boundary (reaching the cap passes); legacy `cavo3` has SOP but no commission rate.
2. Per-agent Bola report / agent statement.
3. Friendly wallet-history labels for `bola.stake`, `bola.settle`, `bola.refund`.
4. Apply `fight.pawn_proof_required` to Bola declares (only matters once the dev gate is lifted).
5. Confirm whether players should bet for themselves (D7).
6. Load test (500 concurrent bets, Postgres) incl. the "two threads at the cap boundary" test.
7. Staged enablement (play-money first), then the legal gate (plan 0.1) before real money.
