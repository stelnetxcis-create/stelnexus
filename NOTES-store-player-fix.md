# Store rename, video player, bet-slip fix

Unpack over the project root and restart the app. No migration, no new dependencies.
After the first load, hard-refresh once (Ctrl+Shift+R) so the new service worker takes over.

## Bet slip "can't place bet / can't click amount"
Cause: the service worker served /static files cache-first, and bola.js had no version, so the browser kept an OLD bola.js
(the one that drew the "1 line(s) · best possible win" table). It did not know the new amount chips or total bar, so chips
did nothing and Place bet stayed disabled.
Fix: bola.js / bola-ui.css / store.css / fightchat.js are versioned; sw.js is now network-first for .js and .css
(cache only as offline fallback) and its cache name is bumped. A stale script can no longer stick.

## Store
- "Chat Store" is now "Store" (page, title, messages, admin setting label).
- Sidebar: Store sits right under Dashboard (was in the Staff group).

## Live video (dummy feed)
- Full-width 16:9 player in the Fight view chat (app/web/templates/_player.html, static/js/stream.js, store.css).
- Play/pause, big play button, full screen: everyone.
- Previous (-10s), Forward (+10s), click/arrow-key seeking, "Go live": Battle Pass (or staff) only. Others see PASS tags,
  a toast and a link to the Store. Live window is 2 minutes; pausing with a pass makes you fall behind live.
- Reusable macro: Esports can call pl.stream_player('esports', ...). Swap .vp-scene for a <video> when a real stream exists.

## Battle Pass covers every stream
- store.stream_access(db, user, "esports") = staff or active pass may chat (tokens are per Meron/Wala game, so not used there).
- store.has_stream_perks() drives the player buttons. Pass perks text now lists Esports and previous/forward.
- Esports itself is still a "Soon" tile; nothing else is wired yet.

Tests: 5 new in tests/test_zz_chat_store.py. Full suite: 250 passed, 1 skipped.
