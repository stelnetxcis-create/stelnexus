# Games Standard

How every game (Sabong, Number game, and any future one) plugs into the shared pages.
The original proposal is in [GAMES_STANDARD_PLAN.md](GAMES_STANDARD_PLAN.md).

## Vocabulary
- **Game**: the product (Sabong, Number game). **Session**: Event / Draw day. **Round**: Fight #n / Draw (slot). **Ticket**: one bet.
- A fight is always "Fight #n", never "Game #n" (`tests/test_game_standard.py` guards the templates).

## Where things live
- `app/games/base.py`: the `GameAdapter` contract, normalized rows, `can_see_money`.
- `app/games/<key>.py`: one adapter per game. `app/games/__init__.py`: the registry (`GAMES`).
- `/games`: hub. Schedule (`/games/{key}/schedule`) renders each game's sessions as a table or as cards (`schedule_view`). `/games/{key}/{live|schedule|tickets|declare}`: workspace entry points.
- `/records`: Tickets, Cancelled, Commissions, History, Reports for every game (`/records.csv` for exports).
- Old URLs (`/betlogs`, `/cancelT`, `/history`, `/archives`, `/bola/betlogs`, `/bola/cancelled`, `/bola/results`, `/bola/reports`, `/commissions`) redirect into Records; `/events` redirects to the Sabong Schedule.

## Adding a game
1. Write `app/games/<key>.py` with a `GameAdapter` subclass: `key`, `label`, `icon`, `vocabulary`, the six `perms`
   (`view, place, cancel, report, manage, declare`) plus `tickets`, and implement `workspace_urls`, `summary_today`,
   `tickets`, `cancelled`, `history`, `report_daily`, `report_monthly`, `report_session`, `schedule`, `create_spec` (and `schedule_context` if you use the cards view).
2. Register it in `app/games/__init__.py`.
3. Run the suite: `tests/test_game_standard.py` fails if a method, right or vocabulary term is missing.

## Rules the shared pages enforce
- Roles that hide totals never see stakes, payouts, KPIs or the Tickets/Cancelled/Reports tabs (`can_see_money`).
- A game that fails `visible()` (switched off, dev-gated, no view right) does not exist for that account.
- The hidden Admin shows as "System" in cancel logs.

## Before deleting legacy number-game player code
Run once against the live database; remove `list_tickets(player_id)`, `with_player` and the legacy fallbacks only if it returns 0:

```sql
SELECT COUNT(*) FROM bola_tickets WHERE player_id IS NOT NULL;
```

## One page per session

A session is one page where you watch, bet, declare, sell and (by right) manage it. A Bola draw is `/bola/draws/{id}`; a
Sabong event is `/events/{id}` (`?fight_id=` picks the fight; it opens on the fight happening now). The cards on Live and
on Schedule both open that same page. Sections appear by right, the way Bola stacks its Operations:

| Section | Who sees it |
|---|---|
| Fight panel: video, bet panel, declare, correct, tickets | everyone with `fights.view`; declare / correct by right |
| Event head and **All fights** (pools) | everyone; pools hidden from money-blind roles |
| **Summary**, **Event expenses** | `cashflow.view`, never money-blind roles |
| **Manage event** (cancel the whole event) | `events.manage` |
| Delete event / fight | the Admin only |

**Play** (`/games/sabong/live`) goes straight into the event when exactly one is open, otherwise it shows the event cards
(`?pick=1` always shows them). Old URLs redirect with their query kept: `/change_name?event_id=&fight_id=` becomes
`/events/{id}?fight_id=`, and `/events/create` opens the Create drawer on Schedule.

Refreshing the `event-head` / `event-body` regions skips every fight-level query; refreshing `fight-panel` skips the
event-level ones (player list, expenses, summary).

## Stream

The stream is the Declarator's job, so it is a Staff Panel section: `/staff/stream` (rights `streams.view` /
`streams.manage`, held by Declarators and the Admin, and by a Pawn through the `streams:manage` grant). `/admin/streams`
redirects there. The public player endpoints (`/_live/stream`) are unchanged.

