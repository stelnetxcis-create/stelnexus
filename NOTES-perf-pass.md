# Performance pass: N+1 queries, dashboard cache, pagination, CSS merge, index audit

Unzip over the project root (or `git apply perf-pass.patch`) and restart the app. No database
migration runs automatically and there are no new Python dependencies. `npm run build` is already
done; the built bundles have the same hashes as before.

## 1. N+1 queries
Counted with a SQL counter on a seeded agent tree (one dashboard/list page view each):

| Page | Before | After | How |
| --- | --- | --- | --- |
| /cashflow | 59 | 18 | one grouped query for every account kind; integrity check sums all accounts in one query |
| /commissions | 47 | 21 | `joinedload` of agent / ticket / fight / player |
| /agents | 24 | 11 | `accounts.user_balances()` (one query, was two per row) |
| /clp | 15 | 9 | `accounts.player_wallets()` |
| /wallet | 20 | 14 | `joinedload` on cash requests |
| /history | 1 + 1 per game + 1 per event | fixed | `betting.pool_snapshots()`, one IN query for events |
| /cuts | 1 per agent | 1 | subs loaded together |

`tests/test_query_counts.py` adds rows and fails if a list page issues more queries for them.

## 2. Dashboard cache (`app/core/ttlcache.py`)
House-wide KPIs, top agents and an agent's downline/player counts are cached for
`DASHBOARD_CACHE_SECONDS` (default 10; 0 = off). Credit balances stay live. The cache is per
process and is cleared after commit when a result is declared/corrected/rejected, a cash request is
created or reviewed, an event is created or changes status, or a ticket is cancelled. Bets themselves
do not clear it, so "tickets today" can lag by up to the TTL.

## 3. Pagination (`app/web/paging.py`)
Newer / Older links on bet logs, cancelled tickets, both Bola ticket lists, history, the audit
trail, players, agents and both commission pages. Filters stay in the links. Commission lines go
back at most 2,000 per game; use the date range to go further.

## 4. CSS
`app.css` + `luxe.css` became `css/base.css`; `arena.css` + `royale.css` became `css/theme.css`
(`tools/css-merge.mjs`, already run). Login pages need only base, so it is two layers, not one.
179 + 1 overridden declarations were removed at source. The built `auth` and `site` bundles are
byte-identical to before.

## 5. Indexes
Nothing is dropped automatically. `python scripts/index_audit.py` (read-only) lists 16 single-column
indexes already covered by a wider index, prints query plans, and on PostgreSQL lists unused
indexes. `deploy/index_audit.sql` has the reviewed statements: two indexes to add (wallet history,
game history) and the 16 drops commented out. New model index: `ix_ledger_entries_account_id_id`;
existing databases need the SQL file to get it.

## Changed / new
New: app/core/ttlcache.py, app/web/paging.py, scripts/index_audit.py, deploy/index_audit.sql,
tools/css-merge.mjs, tests/test_query_counts.py, css/base.css, css/theme.css.
Removed: css/app.css, luxe.css, arena.css, royale.css.
Changed: services (accounts, agents, betting, cashdesk, commissions, events, ledger, reports,
settlement), routes (admin, agents, bets, bola, extra, money, players, dashboard), models (one
index), config, _macros.html + 10 list templates, asset_bundles.json, sw.js, offline.html, tests.
