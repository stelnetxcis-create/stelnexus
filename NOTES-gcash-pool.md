# GCash pool + Staff Panel

## What changed
- **Many GCash numbers.** A Teller adds as many as they want (Staff Panel -> Teller). Numbers are
  encrypted at rest, duplicates refused, `+63 917...` formats accepted.
- **Limit tracker.** Per number, per Manila day and month. Defaults: 100,000 / day, 1,000,000 / month
  (Admin Panel -> Variables -> GCash), overridable per number. Usage is summed from requests routed to
  the number (approved + pending), never stored, so it cannot drift. "Log usage" adds outside use.
- **Auto rotation.** New GCash requests go to the first number with room and move on once a request would
  push a number past `gcash.rotate_at_pct` (default 90%) of its day or month limit. A number that cannot
  fit is skipped. If nothing fits, the request is refused with a clear message. Strategy `fill` (list
  order) or `balance` (most room left). Pending requests hold room for `gcash.hold_minutes` (120);
  rejected ones free it. One warning notification when a number crosses the threshold.
- **Teller scoping.** A Teller sees and approves only requests routed to their own numbers (plus bank /
  unrouted ones). Admin and Master see all.
- **Staff Panel** (below Admin Panel): `/staff` overview, `/staff/teller`, `/staff/declarator` (empty).
  Teller section: Teller + Admin (manage) + Pawn with grant `staff:teller` (read-only, numbers masked).
  Declarator section: Declarator + Admin + Pawn with grant `staff:declarator`. Master: no access.
- **Wallet** shows the number to send to; the flash after a load request and the request rows show the
  number that request was routed to; approvals list shows which GCash each request is on.

## Compatibility
- No pool numbers = old behaviour exactly (nothing assigned, nothing limited).
- Startup migration adds `cash_requests.gcash_account_id`, creates `gcash_accounts` / `gcash_adjustments`
  and seeds each active teller's existing number into the pool. Idempotent.
- Creating a Teller still requires one number; it becomes their first pool entry.

## Tests
`tests/test_gcash_pool.py` (17 tests). Existing tests updated: teller permission set, master/admin
permission diff, "teller without GCash" now means "no live number in the pool". `conftest.py` empties the
pool before each test. Two pre-existing failures are unrelated (service-worker / CSS version tests) and
fail identically on the untouched upload.
