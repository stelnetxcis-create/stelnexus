# StelNexus: Games Standard & Reorganization Plan

> **Rule from now on:** every game (Sabong, Number game/Bola, and any game added later) follows this document.
> A new game is "done" only when it passes the checklist in section 7.
> Status: **PLAN, nothing implemented yet.** Sections 1-3 describe today; 4-6 describe where we are going.

---

## 1. Where we are today (audit)

Same questions, two different shapes. Sabong grew page by page with legacy names; Bola was built later as one unit.

| Need | Sabong (Meron / Wala) | Number game (Bola) | Problem |
|---|---|---|---|
| Entry point | 5 separate links under "Sabong · Meron / Wala" | 4 separate links under "Number game" | No single "pick your game" place |
| Play / watch live | `/change_name` ("Fight View") | `/bola` + `/bola/draws/{id}` | Legacy name `change_name`; different layouts |
| Schedule: view | `/events` | `/bola` (draw list) | Different page shapes |
| Schedule: create | form on `/events` (`/events/create`) | `/bola/draws/create`, `/bola/draws/generate`, Admin > Bola schedules | Create lives in 3 different places |
| Ticket list | `/betlogs` ("Betting List") | `/bola/betlogs` ("Bola Tickets") | Two pages, two names, two perms (`bets.view` vs `bola.place`) |
| Cancelled | `/cancelT` ("Cancel Ticket") | `/bola/cancelled` | Name differs; "Cancel Ticket" reads like an action |
| History | `/history` ("Game History") | `/bola/results` | Different columns |
| Reports | **none** as a page. Only `/archives` (per closed event, not in nav) + per-event CSV | `/bola/reports` (daily, monthly, per-draw) | Sabong has no daily or monthly report |
| Commissions | `/commissions` (shared, has `game=` filter) | same page | Fine, but separate from everything above |
| Perms for the same idea | `bets.view`, `cancellog.view`, `fights.view`, `reports.view` | `bola.view`, `bola.place`, `bola.exposure` | Inconsistent names per tab |
| Vocabulary | "Game #3" = one fight; "Event" = the show | "Draw", "Slot", "Area" | "Game" means a product and a fight at once |

## 2. What is wrong (the short list)

1. No **Games hub**: you cannot see and pick a game in one place.
2. The same records (tickets, cancelled, history, reports, commissions) live on **9+ different pages** in two layouts.
3. **Sabong has no report page**; Bola does. Money questions get answered differently per game.
4. **Create** is scattered (inline form vs 3 places).
5. **Legacy names** leak into URLs and nav (`change_name`, `cancelT`, `betlogs`).
6. "Game" is ambiguous (product vs fight).
7. Permissions per tab are inconsistent, so it is easy to leak or hide a page by accident.

## 3. Principles (non-negotiable)

1. **One shape for every game.** Same tabs, same order, same names, same columns where meaning is the same.
2. **Pick the game once.** A game switcher sits at the top of every page.
3. **Records are shared pages, not per-game pages.** Tickets, Cancelled, Commissions, History, Reports each have one page with a game filter.
4. **A game is a plug-in.** Shared pages read from a registry; they never import game code directly.
5. **Money rules are shared:** the account bets from its own credit (no player picker), the bettor earns no commission on its own bet, ledger + audit on every money move, idempotent placing, cancel is admin-only.
6. **Role-hidden totals stay hidden** everywhere (`hides_totals`), including CSV.
7. **Old URLs keep working** (redirects) until a release explicitly retires them.

## 4. Target design

### 4.1 Vocabulary (use everywhere: UI, code, docs)

| Term | Meaning | Sabong | Number game |
|---|---|---|---|
| **Game** | the product | Sabong | Number game (Bola) |
| **Session** | the container people show up for | Event | Draw day / Area |
| **Round** | the unit you bet on and declare | **Fight** #n | **Draw** (slot) |
| **Ticket** | one bet | Meron/Wala/Draw ticket | Tumbok/Sahod ticket |

UI rule: stop saying "Game #3" for a fight. Say **Fight #3**. "Game" only ever means the product.

### 4.2 Navigation

```
Home
  Dashboard
  Games            <- NEW hub (cards: Sabong, Number game; live count, next round, Play / Manage)
Sabong             <- workspace (same 4 tabs for every game)
  Live  |  Schedule  |  Tickets  |  Declare (staff only)
Number game
  Live  |  Schedule  |  Tickets  |  Declare (staff only)
Records            <- NEW shared page (game switcher at the top)
  Tickets | Cancelled | Commissions | History | Reports
Network / Money / Admin   (unchanged)
```

- The sidebar shows **one entry per game** (its workspace) plus **Records**. The 9 scattered links go away.
- **Games hub** (`/games`): a card per game the account may see: state (live now / next round / closed), main button (Watch/Play), second button (Manage, only with the perm), a one-line today summary (rounds, tickets; money only if allowed).
- Game switcher = segmented control `All | Sabong | Number game`, identical on Records and the hub. Remembers last choice per account (cookie, not DB).

### 4.3 Workspace tabs (identical for every game)

| Tab | What it is | Sabong source | Number game source |
|---|---|---|---|
| **Live** | watch / place bets now | Fight View (video, panel) | draw page + slip |
| **Schedule** | **view and create** sessions and rounds, one page | Events list + Create event (drawer) | Draws list + Create / Generate (drawer) |
| **Tickets** | my tickets (staff: network tickets) | Betting List | Bola Tickets |
| **Declare** | declarator desk entry | Staff > Declarator | Staff > Declarator |

"Schedule" fixes the "Sabong has view and create, Bola is unified" mismatch: both get **one list with a Create button** that opens the same kind of drawer/form.

### 4.4 Records page (`/records`)

One page, one layout, five tabs, one filter bar.

```
Records                                   [ All | Sabong | Number game ]
Tickets | Cancelled | Commissions | History | Reports
From [date] To [date]  [Status v]  [Search]                [CSV]
----------------------------------------------------------------------
KPI strip: Staked | Paid out | House | Commission | Net      (hidden for roles that cannot see money)
----------------------------------------------------------------------
table (same columns; game column only when game = All)
pager
```

Tab contents:

| Tab | Columns (shared core) | Notes |
|---|---|---|
| **Tickets** | time, ticket code, game, round label, pick, stake, status, payout | one normalized row (see 4.6) |
| **Cancelled** | time, code, game, round, stake, who cancelled, reason | replaces "Cancel Ticket" and "Bola Cancelled" |
| **Commissions** | per-account statement (existing `/commissions`) | moves in as-is, keeps `game=` filter and claim/convert/withdraw |
| **History** | finished rounds: time, game, session, round, result, pools or balls | replaces "Game History" and "Bola results" |
| **Reports** | **Daily** (one row per session/round) and **Monthly** (one row per day) + per-session detail | Sabong gets the same views Bola has; event archive folds in here |

CSV on every tab. Same columns as the table, same role-hiding rules.

### 4.5 Reports: what Sabong gets (matches Bola)

- **Daily:** pick a date, one row per event (or fight), columns: staked, paid out, house/tong, commissions, expenses, net.
- **Monthly:** pick a month, one row per day, month total.
- **Per event detail:** the current event summary + expenses, plus CSV (today's `/events/{id}/report.csv`).
- Built from the existing `event_summaries` / `fight_figures`, so numbers equal today's archive. A test asserts that daily totals = sum of archive rows.

### 4.6 Code structure (so a new game is one adapter)

```
app/games/
  __init__.py     # registry: GAMES = {"sabong": ..., "bola": ...}; helpers: visible_games(ctx)
  base.py         # GameAdapter protocol + the normalized row dataclasses
  sabong.py       # SabongAdapter  (wraps services/events, betting, reports, settlement)
  bola.py         # BolaAdapter    (wraps services/bola, bola_settlement, reports)
app/web/routes_games.py      # /games hub + per-game workspace redirects
app/web/routes_records.py    # /records and its tabs + CSV
app/web/templates/records/   # one shared layout + per-tab partials
app/web/templates/_gamebar.html   # game switcher macro
```

`GameAdapter` (each method returns normalized rows, never ORM objects):

```python
key, label, icon, vocabulary          # "sabong", "Sabong", ... {"session": "Event", "round": "Fight"}
perms                                  # view, place, cancel, report, manage, declare  (see 4.7)
workspace_urls(ctx)                    # live / schedule / tickets / declare
summary_today(ctx)                     # hub card numbers (None when money is hidden)
tickets(ctx, filters) -> Page[TicketRow]
cancelled(ctx, filters) -> Page[CancelRow]
history(ctx, filters) -> Page[RoundRow]
report_daily(ctx, day) / report_monthly(ctx, year, month) / report_session(ctx, id)
commission_lines(ctx, filters)         # already shared via commissions service
```

Shared normalized rows: `TicketRow(game, code, at, who, round_label, pick, stake, status, payout, url)`,
`CancelRow(... who, reason)`, `RoundRow(game, session, round, result, at, pools_or_balls, url)`, `ReportRow(label, staked, paid, house, commission, expenses, net)`.

### 4.7 Permissions (one naming rule)

Every game exposes the same six rights, mapped to existing perms so **no data migration**:

| Right | Sabong perm | Bola perm |
|---|---|---|
| view | `fights.view` | `bola.view` |
| place | `bets.place` | `bola.place` |
| tickets (list) | `bets.view` | `bola.place` -> **make `bola.view`-based** |
| cancelled | `cancellog.view` | `cancellog.view` |
| report | `reports.view` | `bola.exposure` -> keep, mapped |
| manage (create/open/lock) | `events.manage` / `events.open` | `bola.manage` / `bola.open` |
| declare | `fights.declare` | `bola.declare` |

The adapter owns this map; the Records and hub pages only ask `adapter.can(ctx, "report")`. Future games must define all six. The Bola dev-only gate (`BOLA_DEV_ONLY`) keeps working because the registry's `visible_games(ctx)` hides a game whose view right is stripped.

### 4.8 Redirects (old -> new), kept until retired

| Old | New |
|---|---|
| `/change_name` (+ query) | `/games/sabong/live` (same query) |
| `/events`, `/events/create` | `/games/sabong/schedule` |
| `/betlogs` | `/records?tab=tickets&game=sabong` |
| `/cancelT` | `/records?tab=cancelled&game=sabong` |
| `/history` | `/records?tab=history&game=sabong` |
| `/archives` | `/records?tab=reports&game=sabong` |
| `/bola` | `/games/bola/live` |
| `/bola/betlogs` | `/records?tab=tickets&game=bola` |
| `/bola/cancelled` | `/records?tab=cancelled&game=bola` |
| `/bola/results` | `/records?tab=history&game=bola` |
| `/bola/reports` | `/records?tab=reports&game=bola` |
| `/commissions*` | `/records?tab=commissions` (account page stays) |

POST endpoints (place, cancel, declare, open/lock...) **do not move** in this plan; only pages move.

## 5. Phases

Each phase is shippable on its own, ends with a green full test run, and changes one thing.

### Phase 0: Baseline and cleanup (small)
- Delete stray files: `*.orig`, `*.rej`, superseded `dist/` bundles.
- Remove dead number-game player code (`list_tickets(player_id)`, `with_player`, legacy fallbacks) **only if** the live DB has no tickets with `player_id` (check with one SQL count).
- Record query-count baselines (`tests/test_query_counts.py`).
- **Done when:** suite green, nothing user-visible changes.

### Phase 1: Registry + Games hub + nav
- Add `app/games/` (base, registry, Sabong and Bola adapters with only `key/label/perms/workspace_urls/summary_today`).
- `/games` hub page and `_gamebar.html` switcher.
- New sidebar: Dashboard, Games, one entry per game, Records (Records can point at the old pages for now).
- Add the redirect table from 4.8 for pages that already moved (none yet).
- **Tests:** hub shows only permitted games; Bola hidden while dev-gated; declarator/pawn/teller see correct entries.
- **Done when:** both games reachable from the hub in 1 click; old links still work.

### Phase 2: Records shell + Tickets + Cancelled
- `/records` layout (filter bar, KPI strip, table, pager, CSV).
- Adapters implement `tickets()` and `cancelled()`; normalized rows.
- Old `/betlogs`, `/cancelT`, `/bola/betlogs`, `/bola/cancelled` redirect here.
- **Tests:** same ticket appears with same numbers in old and new view (compare before redirecting); hidden-totals roles get no money columns or KPIs; CSV equals table.
- **Done when:** all four old pages are redirects.

### Phase 3: History + Commissions tabs
- Adapters implement `history()`.
- Mount existing commissions UI as a Records tab (claim/convert/withdraw endpoints unchanged).
- **Done when:** `/history`, `/bola/results`, `/commissions` land in Records.

### Phase 4: Reports (Sabong catches up)
- Add `reports.sabong_daily / sabong_monthly / sabong_session` next to the Bola ones, reusing existing figures.
- Reports tab: Daily / Monthly / Session, both games, same layout.
- Fold `/archives` and `/events/{id}/report.csv` in.
- **Tests:** daily total = sum of per-event archive rows; monthly = sum of days; hidden-totals roles see nothing.
- **Done when:** Sabong has everything Bola Reports has.

### Phase 5: Schedule tab (view + create, one shape)
- One Schedule page per game: list with filters (today / upcoming / past), **Create** drawer.
- Sabong: Events list + Create event. Bola: Draws list + Create + Generate from schedules.
- Same status chips and row actions (Open / Close / Cancel or Void).
- **Done when:** both games' create flows start from Schedule and look the same.

### Phase 6: Vocabulary, dashboard, staff panel
- Rename UI text per 4.1 ("Fight #n", Tickets, Cancelled, History).
- Dashboard tiles and Staff Panel links point at the new pages.
- Update the declarator desk labels to the same terms.

### Phase 7: Lock it in
- Add `tests/test_game_standard.py`: for every game in the registry, assert it implements all adapter methods, defines all six rights, and exposes all 4 workspace URLs and all 5 Records tabs.
- Update `SECURITY.md`/README with the Games Standard link.
- **Done when:** adding a game without an adapter fails CI.

## 6. Order and size (rough)

| Phase | Size | Risk | Needs your decision first? |
|---|---|---|---|
| 0 | S | low | confirm DB check for old player tickets |
| 1 | M | low | nav labels, hub design |
| 2 | M | medium (money columns visibility) | none |
| 3 | M | low | none |
| 4 | M-L | medium (new numbers) | confirm which columns finance wants |
| 5 | L | medium (create flows) | event create requires a location today: keep? |
| 6 | S | low | confirm "Fight #n" wording |
| 7 | S | low | none |

## 7. Standard for every game (definition of done)

A game is complete only if **all** boxes are true:

**Structure**
- [ ] Adapter in `app/games/<key>.py`, registered in the registry.
- [ ] Workspace has Live, Schedule, Tickets, Declare (staff) using the shared layout.
- [ ] Records tabs (Tickets, Cancelled, History, Reports, Commissions) work for the game.
- [ ] Vocabulary follows 4.1 (session / round / ticket).

**Money and rules**
- [ ] Bets come from the logged-in account's own credit; no player picker.
- [ ] Placing is atomic and idempotent (key), uses the ledger, writes an audit row.
- [ ] Bettor earns no commission on its own bet; the upline waterfall is documented.
- [ ] Cancel is admin-only; refund goes to the bettor's own wallet.
- [ ] Payouts credit the bettor's wallet.
- [ ] Settings live under one prefix (`<key>.*`) and have a dev-gate flag if unfinished.

**Access**
- [ ] All six rights defined (view, place, cancel, report, manage, declare) and mapped in the adapter.
- [ ] Roles that hide totals see no money in pages, KPIs or CSV.
- [ ] Declarator can open/close and declare only what its role allows.

**Quality**
- [ ] CSV for every Records tab; same columns as the table.
- [ ] Empty states, mobile layout, keyboard use, readable contrast.
- [ ] Tests: service, web, permission, query count, and the standard test in Phase 7.
- [ ] Old URLs (if any) redirect; no dead code left behind.

## 8. Risks and how we handle them

| Risk | Handling |
|---|---|
| A role sees money it should not (shared pages) | Every adapter method receives `ctx`; one shared `can_see_money(ctx)`; tests per role for each tab |
| Slow pages (shared queries across games) | Adapters return paginated, pre-aggregated rows; extend `test_query_counts` |
| Bookmarked or printed old URLs break | Redirect table (4.8), kept for at least one release |
| CSV consumers break on new columns | Keep column order of today's CSVs; add new columns at the end only |
| Number game still dev-gated | Registry hides it automatically; tests run with the gate on and off |
| Big-bang merge | Phases are independent; each ships with the old page still redirecting |

## 9. Open decisions (answer before Phase 1)

1. **Names:** OK with "Games" hub, "Records", and "Fight #n"? Anything else you prefer to call them?
2. **Sidebar:** one entry per game (Live/Schedule/Tickets/Declare as tabs inside) or keep a short list of links under each game?
3. **Reports columns for Sabong:** staked, paid out, house/tong, commissions, expenses, net. Anything finance needs added?
4. **Event creation** currently requires choosing a location when areas exist. Keep this in Schedule > Create?
5. **Who sees Records > Reports:** Admin and Master only (as `reports.view` today), or also Agents for their own network?
6. **Retire old URLs** after one release, or keep the redirects permanently?

## 10. Appendix: current inventory (for reference while migrating)

**Sabong pages:** `/events`, `/events/{id}`, `/change_name`, `/history`, `/betlogs`, `/cancelT`, `/archives`, `/events/{id}/report.csv`, `/commissions`, `/cashflow`.
**Sabong actions (stay as they are):** `/events/create|status|stream|cancel|expenses`, `/fights/{id}/lock|odds|betmode|llamado|declare|proof|correct`, `/bets/place|live/place|live/undo|{id}/cancel`.
**Bola pages:** `/bola`, `/bola/results`, `/bola/draws/{id}`, `/bola/betlogs`, `/bola/cancelled`, `/bola/reports`, `/bola/agents`, `/bola/draws/{id}/report.csv`, Admin > Bola schedules.
**Bola actions (stay as they are):** `/bola/bets/place|{id}/cancel`, `/bola/draws/create|generate|{id}/open|lock|void|expenses|declare|proof|correct`, `/bola/runs/{id}/approve|reject`.
**Shared:** `/commissions*`, `/cashflow`, `/dashboard`, Staff Panel (Declarator, Teller).
