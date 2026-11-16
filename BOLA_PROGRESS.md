# Bola progress handoff

Full suite: 208 passed, 1 skipped (`python -m pytest -q`; the skip is the Postgres run, see below).
Feature is dark: `bola.enabled=false` by default, and `BOLA_DEV_ONLY=true` hides it from every role but the Admin.
This file supersedes `STELNEXUS_NUMBER_GAME_PROGRESS.md` (written earlier; it still lists items that are now done).

## Done
- Plan phases 1-6 (foundations, draws and bets, settlement, commissions, web + API, scheduling and ops):
  deploy timers, `bola-preflight` in `scripts/security_check.sh`, README section.
- This session:
  - Livestream chat (experimental) rename finished (leftover "staff room" wording and the grant section).
  - `/bola/cancelled` and `/bola/agents` (+ `.csv`, `/bola/agents/{id}`): the sidebar linked to them before they existed.
    Scoped to the viewer's network; Declarator/Pawn without totals are refused; hidden Admin shows as "System".
  - Friendly ledger labels (`bola.stake` / `bola.settle` / `bola.refund`; cancels read "Reversal · Bola bet placed"),
    also in the staff player history.
  - D4 is now a setting: `bola.commission.pay_master`, default OFF = plan default (Master share stays with the house).
    The earlier code paid the Master a commission line; turn the setting on to get that back.
  - D7 closed: players are not logins, so agents bet for players, same as sabong.
  - `scripts/bola_loadtest.py` (+ `tests/test_bola_postgres.py`): ran on PostgreSQL 16. 500 concurrent bets: exposure equals
    the live lines, ledger balanced; two-thread cap and per-line boundaries: exactly one winner in 20/20 rounds each;
    cancels racing bets stay consistent. Disabling the draw lock makes both boundary checks fail, so the tests do bite.
  - Bug found and fixed by that run: concurrent requests with the same idempotency key got a raw IntegrityError
    (money was safe: one ticket, one charge). `place_bola_bet` now re-checks the key after taking the draw lock and
    returns the first ticket.
- Plan close-out (`tests/test_plan_closeout.py`):
  - Sabong `betting.place_bet` now re-checks the idempotency key after taking the game lock (same fix as Bola).
    A duplicate that waited on the lock gets the first ticket instead of a raw IntegrityError. Test removed-fix = fails.
  - Pawn proof (`fight.pawn_proof_required`) on the Bola declare route is now tested (refused without a picture).
  - Ruling (a) is pinned by a test: a house-losing draw pays no commission, even with `pay_master` on.
    Ruling (b) was already pinned by `test_liability_cap_boundary`.

## Still open
1. Rulings: (a) and (b) are decided in code and pinned by tests; change them only on purpose.
   (c) legacy `cavo3` has a SOP rate but no commission rate: needs a number from the operator.
2. Independent review of the money paths before real money (a self-review is not that).
3. Play-money period, then the legal gate (plan 0.1). Not code tasks.
4. Run `scripts/bola_loadtest.py` against your own PostgreSQL / hardware before launch (numbers here came from a sandbox:
   about 48 bets/s with 32 workers on one draw, all of them serialised by the draw lock by design).
5. Open design questions: per-location money caps (none exist today), more game types (only Tumbok-Sahod exists).
