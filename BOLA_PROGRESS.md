# Bola progress handoff

Full suite: 237 passed, 1 skipped (`python -m pytest -q`; the skip is the Postgres run, see below).
Feature is OPEN (Nov 17): `bola.enabled=true` and `BOLA_DEV_ONLY=false` by default, so every role with Bola permissions sees it. `BOLA_DEV_ONLY=true` still pulls it back behind the Admin.
No liability cap is set by default (0 = none): set both before real money. `bola-preflight` now fails until caps + `BOLA_LIVE_ACK=1` are in place.
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

## Review pass (Nov 17) - read of `bola.py`, `bola_settlement.py`, `ledger.py`
A second read of the money paths by a different reviewer than the author. It is NOT the independent
pre-launch review (item 2 below): it never ran on PostgreSQL and no human signed it off.
Checked and found sound: ledger sign maths of stake/settle/refund/reversal (each txn sums to zero), exposure vs
settlement agree on what an outcome owes, per-ticket payout cap only lowers payouts, void/redeclare roll back
before re-settling, run status re-checked under the draw lock, wallet floor enforced under the account lock.
Fixed:
- `cancel_bola_ticket` locked ticket -> draw while settlement locks draw -> tickets: a cancel racing a declare could
  deadlock (PostgreSQL aborts one with a 500, no money lost). Now draw -> ticket, same as bet and settlement.
  Visibility is checked before any lock so an outsider cannot stall a draw. Test: `test_cancel_takes_the_draw_lock_before_the_ticket_lock`.
- Idempotency replay returned the first ticket for ANY request carrying the key. A reused key on a different bet
  (other numbers/stake/draw/player) looked like success but placed nothing. Now Conflict unless it is the same bet.
  Test: `test_reused_idempotency_key_on_a_different_bet_is_refused`.
- `test_dashboard_games` was stale since the UI upgrade (Esports tile also says "Coming soon"); it now checks the
  number-game tile itself. The dev-only gate was verified not to leak.
Noted, not changed:
- Sabong `betting.place_bet` has the same replay-without-comparison behaviour as Bola had.
- `bola.allow_duplicate_balls=true` is a trap: tickets can never hold (n,n), so a doubled draw always favours the
  house, and the shown RTP (n*n outcomes) overstates what players can win. Leave it off.

## Open launch + admin tools (Nov 17)
- Bola open to all roles; the dashboard "Coming soon" tile is gone (tile is a link when on, absent when off).
- Seller Desk (`/sell`) is experimental: new `sell.desk` permission, Admin only, no Pawn grant.
- Admin Panel: Accounts (edit details / remove = close) and IP bans, both grantable to a Pawn. See README.
- Fixed: sabong idempotency replay now checks it is the same bet; `bola.allow_duplicate_balls` fails the preflight.
- New table `ip_bans` (created on start by the existing schema upgrade; no data migration).

## Still open
1. Rulings: (a) and (b) are decided in code and pinned by tests; change them only on purpose.
   (c) legacy `cavo3` has a SOP rate but no commission rate: needs a number from the operator.
2. Independent review of the money paths before real money (a self-review is not that).
3. Play-money period, then the legal gate (plan 0.1). Not code tasks.
4. Run `scripts/bola_loadtest.py` against your own PostgreSQL / hardware before launch (numbers here came from a sandbox:
   about 48 bets/s with 32 workers on one draw, all of them serialised by the draw lock by design).
5. Open design questions: per-location money caps (none exist today), more game types (only Tumbok-Sahod exists).
