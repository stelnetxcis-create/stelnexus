# StelNexus UI upgrade (Royale)

Unzip over your project root; folder layout matches your project. Only new/changed files are included.
No database migration. No new Python dependencies. Restart the app after copying.

## New
- app/static/css/royale.css        luxury layer (type, KPI sparklines, toasts, skeletons, live tiles, winner banner, seller desk)
- app/static/js/royale.js          sound, toasts, 5s polling, tile flip, result reveal + confetti, page skeleton
- app/static/js/sell.js            seller desk logic
- app/static/fonts/*               Cormorant Garamond + Manrope (self-hosted; the CSP only allows same-origin fonts)
- app/static/img/empty/*           rooster, arena, balls, ticket artwork
- app/web/routes_live.py           /_live/event/{id}, /_live/lobby, /_live/dashboard (JSON, permission-checked, hide totals respected)
- app/web/routes_sell.py           /sell, /sell/place, /sell/undo/{id} (reuse place_bet / cancel_ticket, same rules)
- app/web/templates/sell.html
- tests/test_zz_live_sell.py       5 tests

## Changed
- app/main.py (router registration), app/web/deps.py (flash undo payload), app/web/routes_fights.py (undo on lock / bet mode / llamado / event status)
- app/static/sw.js (cache name bumped so installed apps refresh)
- templates: base, _macros, _lobby, dashboard, events, change_name, betlogs, bola_lobby, history

## Behaviour notes
- Polling is every 5s (dashboard 8s), pauses in background tabs, backs off on errors, and an unchanged state costs a few bytes.
- Sounds are synthesised in the browser (no audio files). They start after the first click or key press (browser rule). The speaker button in the top bar turns them off.
- Sparklines exist only for Stake and Tickets (the only series with history).
- Undo is offered only for reversible actions. Seller-desk Undo cancels the ticket and refunds, within your existing cancel window.
- Flash banners are now toasts. Without JavaScript they still show as normal banners.
