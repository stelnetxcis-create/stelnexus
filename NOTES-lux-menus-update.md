# Lux VIP, 30-chat tokens, nav + menu changes

Unpack over the project root and restart. The startup migration adds `chat_passes.tier` by itself. No new dependencies.
Hard-refresh the browser once (Ctrl+Shift+R).

## Store
- NEW Lux VIP Battle Pass, 2,999 PHP / 30 days (store.lux_price_cents, store.lux_days). Same perks as the Battle Pass
  (chat on every stream incl. Esports, previous/forward on video) plus flashy, glowing chat messages with a "Lux VIP" crest.
  Buyers get a thank-you (purchase message + card on the Store page): "we humbly thank you ... keeps the servers running".
  Stacks on renewal. Ledger kind store.lux ("Lux VIP Battle Pass" in money history).
- Chat tokens: 99 PHP = 30 chats (was 3). One pack only works on games from 3 different events (store.token_events, editable
  in Admin -> Variables -> Store). After it has been used in 3 events, leftover chats stay with those events (still usable on
  other games in those same events, refused on a 4th event with a clear message).

## Navigation
- Admin Panel moved out of "Staff" to sit next to Dashboard (Dashboard, Store, Admin Panel). Same permission as before:
  Admin, and Pawns the Admin granted panel access.
- "Livestream Chat (exp)" removed from the menu. Chat is shown through the games (Meron/Wala now, Esports later). The /chat
  staff room route still exists and is still permission-protected.
- Commission moved from the sidebar into the Wallet dropdown; only shows for roles that hold commissions.view
  (Master, Cavo, Kubrador, Admin). Players never see it.
- Referral codes moved from the sidebar into the account (profile) dropdown; shows for roles with referrals.manage.

## Menus no longer overlap
Wallet, account, notifications and dev menus: opening one closes the others; click outside or Esc closes (static/js/menus.js).

Tests: 6 new/updated in tests/test_zz_chat_store.py, 1 updated in tests/test_v3.py. Full suite green.
