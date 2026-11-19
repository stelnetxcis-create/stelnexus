# Less polling: partial refresh, SSE chat, idle back-off, ETag/304

Unzip over the project root, run `npm run build` only if you edit JS again (the built output is included), restart the app.
No migration, no new Python dependency. **Caddy needs the updated `deploy/Caddyfile`** (see "Deploy").

## 1. Partial refresh instead of `location.reload()`
- `{% call ui.region('name') %}...{% endcall %}` (in `_macros.html`) marks a box. Asking the same URL with `X-Fragment: name`
  returns only that box (ETag, `304` when unchanged, `Vary: X-Fragment`). `render()` in `deps.py` does the slicing
  (`web/conditional.py`); no cookies are set and the page's current CSRF token is reused, so forms outside the box keep working.
- `StelNexus.refreshRegion(name)` (app.js) swaps the box, keeps scroll, skips while the person is typing or a dialog is open,
  re-binds handlers (`enhance(root)`) and fires `snx:region`.
- Regions: `event-head`, `event-body` (event page), `fight-panel` (fight view; the video and chat are outside it), `lobby`, `live-games` (dashboard).
- `royale.js` `softReload` now refreshes these boxes; it reloads the page only if a box is missing or the server cannot return one.
- `data-autorefresh="N"` (app.js) refreshes `data-autorefresh-region="name"` (or the enclosing region) every N s, stretching while nothing
  changes and not running in a hidden tab. No template uses it today; markup that names no region keeps the old reload.
- Cost note: the server still renders the whole page for a fragment (it skips the notification query). The saving is bytes, DOM work and UX,
  not CPU. Pages that refresh often should use a JSON endpoint like `/_live/*` instead.

## 2. Chat over SSE
- `GET /chat/stream`, `GET /fights/{id}/chat/stream` (`text/event-stream`). Same JSON as the poll endpoints, as `event: state`.
  Other events: `auth` (signed out / not allowed: stream ends), `bye` (server recycles streams every 10 min; browser reconnects), `retry`.
- `services/realtime.py`: an in-process hub. A SQLAlchemy `after_flush`/`after_commit` listener wakes subscribers when a `ChatMessage` is
  committed or a `Fight` changes, so every writer is covered with no call sites. A stream holds no DB connection: each pass uses a short
  `session_scope` and re-authenticates.
- Heartbeat: 20 s (2 s while typing dots are showing). Anything written by another process (CLI, systemd timers) or another worker shows up at
  the next heartbeat. Limits: 4 streams per account, 800 per process (HTTP 429 -> the browser polls instead), 40 connects/min/account.
- Browser: falls back to polling after 3 errors or an HTTP failure and retries the stream a minute later. `Last-Event-ID` resumes.

## 3. Back-off
- Polling fallback: 4 s (fight chat 3 s) -> 15 s after a quiet minute -> 30 s after five. A message, key press, tap or the tab coming back
  snaps it back (`StelNexus.pacer`). Hidden tab: no timers at all; a stream in a tab hidden over 60 s is closed. Errors still slow it up to 5-6x.
- `_live/*` pollers (lobby, event tiles, dashboard) keep their 5-8 s pace on purpose (lock/open state matters to sellers) but stop in hidden tabs.

## 4. ETag / If-None-Match
- `_live/*`, `/chat/messages`, `/fights/{id}/chat` send an `ETag` and answer `304`. The old `?rev=` still works for pages running the previous script.
- Scripts send `If-None-Match` by hand (requests are `no-store`, so the browser never would). The server ignores a `-gzip`/`-zstd`/`-br` suffix a proxy may add.

## Deploy
`deploy/Caddyfile`: stream paths are excluded from `encode` and get `flush_interval -1`. Without this, compression/buffering delays messages
(the browser would still work, just in bursts). Not exercised here (no Caddy in the sandbox): run `caddy validate --config deploy/Caddyfile`.
Keep `--workers 1` (hub and typing indicator are in-process).

## Tests
`tests/test_zz_realtime.py` (13). Two existing tests fail whenever UTC and Manila are on different dates (`test_sabong_and_bola_land_in_one_statement`,
`test_dashboard_card_counts_todays_draw`); they fail identically on the original code.
