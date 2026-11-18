# Store v2: bigger Twitch-style chat, slow mode, Alias Token, greyed-out options, DEV revoke

Unzip over your project root. Only new/changed files are included.
No manual migration: the two new tables (`alias_tokens`, `chat_aliases`) are created by `create_all` on start. No new dependencies. Restart the app.

## Chat (fight view)
- Messages are dense single lines like Twitch: `[time] [badge] name: text`, not big bubbles. Many more fit on screen.
- Size presets S / M / L (cog button), remembered per browser. On phones the chat is a tall fixed panel under the video (it no longer shrinks to its content).
- Names get stable colours; Lux messages keep the gold highlight.
- Slow mode already existed (hard-coded 2 s for everyone). It is now the setting `store.slow_mode_seconds` (default 3, 0 = off).
  Battle Pass / Chat Token chatters are slowed. **Lux VIP and staff are never slowed.** The chat shows a "Slow mode · 3s" chip
  (green/gold "Slow mode off · Lux VIP" for Lux) and the Send button counts down.
  A 40 messages/minute anti-spam guard still applies to everybody, Lux included.

## Store
- Passes stay big. Chat Tokens and the new Alias Token are small "material" tiles under **Consumables**; their rules are always shown.
- Inferior options are greyed out AND refused by the server:
  - Lux running: no Battle Pass, no Chat Tokens.
  - Battle Pass running: no Chat Tokens (Lux is still a valid upgrade).
- The Battle Pass card used to show the Lux expiry (any tier counted as "pass"). It now shows only the Battle Pass's own days.
- **Alias Token**, 200 PHP (`store.alias_price_cents`), one-time consumable. Type a name, press Use token: the token is spent and chat shows the alias instead of the username.
  A later change costs another token. Rules: 3-20 chars, letters/numbers/space/dot/dash/underscore; unique (case, space and punctuation blind);
  refused if it matches another username or looks like staff/system (admin, system, staff, management, ...). A refused alias costs nothing.
  Players never see the real account behind an alias; staff (chat.use / Admin) see it on hover.

## DEV: revoke an effect
Developer tools menu -> **Store effects** (`/dev/store`, Admin only). Look up by username or alias; revoke Battle Pass, Lux VIP, unused chat tokens,
unused alias tokens, or the alias. A reason is required (audit log, action `dev.store_revoke`). Tick Refund to reverse the purchase in the ledger;
leave it off to only remove the effect. Chat-token refund only applies to packs with nothing used.

## Tests
`tests/test_zz_store_v2.py` (11 tests). Full suite: 287 passed.
