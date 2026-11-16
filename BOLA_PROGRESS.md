# Bola progress handoff (end of session 2)

Full suite: 145 passed. Feature is dark: `bola.enabled=false` by default.

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

## TODO (in order)
1. Admin UI for `BolaSchedule` (draw times per area): `/admin/bola/schedules` (service `bola.upsert_schedule` already exists).
2. JSON API `/api/v1/bola/...` (plan 4.3) in `app/api/v1/routes.py`.
3. Dashboard card + `/live` panel (`live.show_bola`), and daily/monthly Bola reports + CSV (`/bola/draws/{id}/report.csv`).
4. Number-grid tap UI on the slip (currently plain number inputs).
5. `bola-import-legacy` CLI (parse online_bukis.sql, --dry-run / --apply), `BolaExpense` UI.
6. `proofs.attach` generic for BolaDraw (result photo).
7. Deploy: systemd timer / cron for `bola-tick` and `bola-generate --days 2`; `scripts/security_check.sh` check that bola.enabled is deliberate.
8. Load test (500 concurrent bets, Postgres), staged enablement. Confirm legal gate (plan 0.1) before real money.
