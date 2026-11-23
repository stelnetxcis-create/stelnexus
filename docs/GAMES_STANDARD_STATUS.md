# Games Standard: Status (done / to do)

Companion to `GAMES_STANDARD_PLAN.md`. Code is in `stelnexus_games_standard.tar.gz`.

## Test status (read first)
- Full suite: **555 passed, 1 skipped** (last full run had 3 menu-link test failures, fixed and re-run: 40 passed) (one file, `test_bola_agents.py`, was re-run alone after the last fix and passes; `test_bola_postgres.py` was not run: needs Postgres).
- ~25 old tests were updated to follow the new redirects (`follow_redirects=True`) or the new sidebar; `test_query_counts` now also covers the Records tabs.
- Parity restored during testing: cancelled-by shows `System` for the hidden Admin; History has a Location column.

## DONE

### Phase 1: Registry + hub + nav
- `app/games/base.py`: `GameAdapter`, normalized rows (`TicketRow`, `RoundRow`, `ReportRow`), `Filters`, six rights, `can_see_money`.
- `app/games/sabong.py`, `app/games/bola.py`, `app/games/__init__.py` (`GAMES`, `visible_games`).
- `app/web/routes_games.py`: `/games` hub, `/games/{key}/{tab}` redirects to existing pages, game cookie.
- Templates: `games_hub.html`, `_gamebar.html`, `_nav_games.html`.
- Sidebar replaced: All games, one entry per game, Records.
- Tests updated for the new sidebar (`test_player_preview`, `test_bola_ui`, `test_dev_tools`, `test_zz_chat_store`).

### Phase 2: Records (Tickets, Cancelled)
- `app/web/routes_records.py`, `templates/records/*`: `/records` and `/records.csv`; filters (date, status, search, session, round); KPI strip; money hidden for roles that hide totals.
- Added `since`/`until` to `betting.list_tickets` and `bola.list_tickets`.

### Phase 3: History + Commissions tabs
- History via adapters; Commissions page shown inside the Records layout.

### Phase 4: Reports
- Sabong daily, monthly, per-session and archive views from `event_summaries`; Number game uses existing rollups.

### Phase 7: Lock-in test
- `tests/test_game_standard.py` fails if a game lacks an adapter method, any of the six rights, vocabulary, workspace URLs or a Records tab.

### Redirects (old to new, query kept)
`/betlogs`, `/cancelT`, `/history`, `/archives`, `/bola/betlogs`, `/bola/cancelled`, `/bola/results` (only without `location_id`), `GET /commissions`.
Six dead templates deleted (done).

## TO DO (in order)

1. ~~Run the full suite and fix old-URL tests~~ (done). ~~Tighten monthly = sum of days assertion~~ (done).
3. **Add missing tests from the plan:** hub shows only permitted games; Bola hidden while dev-gated (gate on and off); declarator/pawn/teller entries; every role per Records tab; query counts (`test_query_counts`); monthly = sum of days; CSV equals table for all tabs.
4. ~~History parity gaps~~ (done: Location, Llamado/Dehado, photo columns).
4b. **Phase 0:** delete stray `*.orig`, `*.rej`, old `dist/` bundles; run the SQL check for tickets with `player_id` before removing dead player code.
5. **Phase 5: Schedule tab** (one list + Create drawer per game; Sabong Events and Bola Draws/Generate; same status chips and actions). Then redirect `/events`, `/events/create`, `/change_name`, `/bola` to `/games/{key}/...` and point adapter `workspace_urls` at the new pages (avoid redirect loops). Decide: keep the location requirement on event create?
6. **Phase 6: Vocabulary:** "Fight #n" in the UI, Tickets / Cancelled / History labels; update dashboard tiles (`dashboard.html` still links old URLs), Staff Panel and declarator labels. Also update `base.html` account menu links (`/archives`, `/commissions`) and `ticket.html` "Back to list".
7. **Bola reports parity:** `/bola/reports` was intentionally NOT redirected (exposure and worst-outcome views missing in Records > Reports). Add those, then redirect. Also add the area filter on Records > History, then redirect `/bola/results` fully.
8. **Docs:** link the Games Standard from `SECURITY.md` / README.
9. **Open decisions defaulted** (change if wrong): one sidebar entry per game; Reports visible only with `reports.view`; Sabong report columns = staked, paid out, house, commission, expenses, net (per-fight rows show commission/expenses as 0).
10. **Known gaps:** the Bola `tickets` right now uses `bola.view` (plan 4.7) but the hub and nav still check `bola.place` for the Play/Watch label; Sabong per-session report has no per-fight commission or expenses.

## Update (round 3)
- Menu, dashboard, wallet and ticket "Back to list" links now point straight at `/records?...` (no redirect hop).
- Added visibility unit tests (Number game hidden when switched off or without `bola.view`; Sabong needs fights/events view) and a History-columns test.
- Still open: Phase 5 (Schedule tab + `/events`, `/change_name`, `/bola` redirects), Phase 6 (vocabulary renames, Staff Panel/declarator labels), Phase 0 cleanup, `/bola/reports` parity, SECURITY.md link. Dashboard still links `/bola/cancelled` (redirects, works).

## Update (round 4): Phase 5, Schedule (partly done)
- Adapter contract now has `schedule(ctx, when)` and `create_spec(ctx)`; the standard test requires both for every game.
- **Bola:** new shared Schedule page `/games/bola/schedule` (Today / Upcoming / Past, status chips, **Create draw** and **Generate from schedules** drawers, posting to the existing endpoints). `workspace_urls["schedule"]` points at it.
- **Sabong:** `schedule()` and `create_spec()` are implemented and the same shared page can render them, but **Sabong's Schedule tab still opens `/events`**. I tried redirecting `/events` into the shared page and reverted it: the Events page is a live card lobby (realtime refresh, quick-register location, location defaults) and a plain table would have dropped that, breaking 10 tests.
- Full suite: **557 passed, 1 skipped**.
- Still open for Phase 5: port the card lobby + location quick-register into the shared Schedule shape, then redirect `/events` and `/bola`. Row actions (Open/Close/Cancel) still live on each session's own page.
- Still open: Phase 6, Phase 0, `/bola/reports` parity, SECURITY.md link.

## Update (round 5): Phase 6, Vocabulary (done for server-rendered text)
- "Game #n" is now **"Fight #n"** in all templates, error/flash messages (betting, events, settlement, streams, purge, declarator desk, staff panel), plus "Fights" for the column/stat/section labels on the fight view, event detail, dashboard, lobby cards, Admin > Locations and the event create form.
- Dashboard tiles renamed to the Records terms: **Tickets**, **History**, **Cancelled**.
- Guard test `test_vocabulary_no_game_number_in_templates` fails if "Game #" comes back in a template.
- Full suite: **558 passed, 1 skipped**.
- **Not changed on purpose:** (1) JavaScript sources (`app/static/js/*.js`, about 40 strings such as "Game #" in the live fight view, declarator and chat) because the hashed bundles in `app/static/dist` must be rebuilt with your build tool (`tools/`, needs `node_modules`); after editing, rebuild and commit `dist`. (2) Stored ledger/pool account names ("Pool · Game #n") so existing history keeps its names.
- Still open: Phase 0 cleanup, card-lobby port for a shared Sabong Schedule, `/bola/reports` parity, SECURITY.md link, the JS wording + dist rebuild.

## Update (round 6): Phase 0 and Phase 7 docs
- Deleted 25 superseded bundles from `app/static/dist` (everything `app/build/manifest.json` no longer references). No `*.orig` / `*.rej` files existed.
- Added `docs/GAMES_STANDARD.md` (vocabulary, where things live, how to add a game, rules) and `docs/GAMES_STANDARD_PLAN.md`; linked from `SECURITY.md`.
- **Dead number-game player code NOT removed:** that needs one SQL check against your live database. Run `SELECT COUNT(*) FROM bola_tickets WHERE player_id IS NOT NULL;` (also in the docs); if it returns 0 the code can go.
- Full suite: **558 passed, 1 skipped**.
- Still open: card-lobby port for a shared Sabong Schedule (then redirect `/events`, `/bola`), `/bola/reports` exposure parity then redirect, JS "Game #" strings + dist rebuild.

## Update (round 7): rebased on your archive + `/bola/reports` parity
- Rebased on your updated archive (mobile nav, live wallet balance, license logo). It passed untouched (567 passed), so nothing of yours was changed or reverted.
- `/bola/reports` now redirects to **Records > Reports** (game=bola). The Records daily table gained **Result** and **Tickets** columns (shown when a game supplies them), and rows link to the draw page, which keeps the exposure / worst-outcome panel and the per-draw CSV. The dashboard tile and the draw page's Reports button link straight to Records. `bola_reports.html` deleted.
- Full suite: **567 passed, 1 skipped**.
- Still open: card-lobby port for a shared Sabong Schedule (then redirect `/events`, `/bola`); the JS "Game #" strings + `dist` rebuild; the dead number-game player code (needs the SQL count on your live DB).
