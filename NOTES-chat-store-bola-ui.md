# Chat Store + Fight-view livestream chat + number-game UI

Unpack over your project root. Restart the app: the startup migration adds `chat_messages.fight_id` and creates
`chat_passes` / `chat_tokens` by itself. No new dependencies.

## Livestream chat (Fight view)
- Chat panel sits above the game card in Fight view (`/change_name`). Everyone who can open Fight view can READ it.
- Typing is locked unless you have: staff access (`chat.use`), a running Battle Pass, or a Chat Token spent on that game.
- When the declarator declares the result the room closes for everyone, shows "Declarator announced: <winner>", plays the win sound, history stays readable.
- Messages are stored per game (`chat_messages.fight_id`). The staff room at `/chat` only shows messages with no game.
- Slow mode 2s, 40 msgs/min, 300 chars, short-poll every 3s (same approach as the rest of the console).

## Chat Store (`/store`, sidebar "Chat Store")
- Battle Pass: 999 PHP, 30 days, chat on every game. Renewing stacks onto the end of the current pass.
- Chat Tokens: 99 PHP buys 3 chats (`store.token_uses`). Each use unlocks ONE game, tied to that game once spent. Unused uses never expire.
- Paid from the buyer's wallet into house revenue (balanced ledger txn `store.pass` / `store.token`, idempotent, audited, shows in money history).
- Prices/length/on-off are editable in Admin Panel -> Variables -> "Store" (`store.pass_price_cents`, `store.pass_days`, `store.token_price_cents`, `store.enabled`).
- Declarators and Pawns have no wallet, so they cannot buy (a Pawn with the chat grant already has staff access).

## Number game UI
- Lottery-ball board (colour per tens, pop animation, pick-order badge), pick tray, Lucky pick, Clear.
- Ticket type as big tabs, quick stake chips with Tumbok/Sahod/Both target, live line cards, sticky total bar.
- Place buttons stay disabled until the ticket is complete. Cut-off countdown on slip and lobby, drawn-result balls roll in.
- Lobby table became draw cards. Form field names and endpoints are unchanged.

Tests: tests/test_zz_chat_store.py (8). Full suite: 245 passed.

## Video
The fight-view chat has a 16:9 'Live match video' placeholder; no stream is wired yet.
