# Games Standard

How every game (Sabong, Number game, and any future one) plugs into the shared pages.
The original proposal is in [GAMES_STANDARD_PLAN.md](GAMES_STANDARD_PLAN.md).

## Vocabulary
- **Game**: the product (Sabong, Number game). **Session**: Event / Draw day. **Round**: Fight #n / Draw (slot). **Ticket**: one bet.
- A fight is always "Fight #n", never "Game #n" (`tests/test_game_standard.py` guards the templates).

## Where things live
- `app/games/base.py`: the `GameAdapter` contract, normalized rows, `can_see_money`.
- `app/games/<key>.py`: one adapter per game. `app/games/__init__.py`: the registry (`GAMES`).
- `/games`: hub. `/games/{key}/{live|schedule|tickets|declare}`: workspace entry points.
- `/records`: Tickets, Cancelled, Commissions, History, Reports for every game (`/records.csv` for exports).
- Old URLs (`/betlogs`, `/cancelT`, `/history`, `/archives`, `/bola/betlogs`, `/bola/cancelled`, `/bola/results`, `/commissions`) redirect into Records.

## Adding a game
1. Write `app/games/<key>.py` with a `GameAdapter` subclass: `key`, `label`, `icon`, `vocabulary`, the six `perms`
   (`view, place, cancel, report, manage, declare`) plus `tickets`, and implement `workspace_urls`, `summary_today`,
   `tickets`, `cancelled`, `history`, `report_daily`, `report_monthly`, `report_session`, `schedule`, `create_spec`.
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
