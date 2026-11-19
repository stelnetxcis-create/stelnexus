# Static asset build (cache, hash, minify, merge)

Unzip over your project root. Restart the app after copying. No database migration, no new Python dependencies.
`npm ci` is only needed if you want to rebuild (Node 18+); the built output is included.

## What changed
1. **Caddy caches static files.** `deploy/Caddyfile`: `/static/dist/*` gets `Cache-Control: public, max-age=31536000, immutable`. `/sw.js` is untouched (the app sends `no-cache`). The app sends the same header for `/static/dist/*` itself (`core/middleware.py`).
2. **Content-hashed file names.** Output like `site.ad0637f0.css`. Templates use `{{ css('site') }}` / `{{ js('chat') }}` (`core/assets.py`); no template has a `?v=` any more. The service worker is generated per build (`app/build/sw.js`) and its cache name is the build id, so nothing is bumped by hand. `sw.js` is now cache-first for hashed files, stale-while-revalidate for other static files, and still never touches HTML or API calls.
3. **Minify and bundle.** Console pages load `site.css` + `site.js` + `nav-init.js` (3 requests, was 11). Login/register/error pages load `auth.css` + `auth.js`. Feature pages add one small bundle (store, bola-ui, livestream, bola, chat, dash, fightchat, stream, referrals, sell).
4. **Overlapping CSS.** The build drops every declaration that a later rule with the identical selector overrides (451 in all; 244 in the console bundle). It never reorders or merges different selectors, so the cascade is unchanged. The four source files are NOT rewritten into one: the login pages load only `app.css` + `luxe.css`, so luxe.css cannot lose declarations that arena/royale override. `npm run build:report` lists the selectors defined in more than one file, which is the worklist if you ever merge by hand.

## Size (per page load, gzip)
Console page before: 11 files, about 290 KB raw. Now: `site.css` 21 KB + `site.js` 11.5 KB + `nav-init.js` 0.2 KB, about 33 KB gzipped (the same files gzip to roughly 70 KB unbundled). Repeat visits download nothing for these files.

## Follow-up: fonts, per-page JS, animations
- **Font preload.** Console pages preload Manrope and Cormorant Garamond 600 (`<link rel="preload" ... crossorigin>`), using the same hashed URLs as the CSS so nothing downloads twice. Login pages declare no web fonts, so they get none.
- **Per-page JS audit.** `sell`, `chat`, `fightchat`, `stream`, `bola`, `dash`, `referrals` were already loaded only by the pages that use them. `royale.js` (toasts, polling, live tiles) and `luxe.js` (count-up, gold dust, tilt) are used on every console page, so they stay in the cached `site.js`. The only dead weight was the login-page code in `luxe.js`; it moved to `js/login-extras.js`, loaded by the auth bundle only.
- **Animations.** Reduced motion was already honoured (`app.css`, `luxe.css`, the JS effects). New: `js/anim-pause.js` pauses every infinite animation whose element is off-screen and resumes it when it scrolls back (`.is-offscreen` rule at the end of `royale.css`). Checked in Chromium at phone width: off-screen animations paused, on-screen ones running, none at all with reduced motion.

## Day to day
- After editing anything under `app/static/{css,js,img,icons,fonts}`, `sw.js` or `app/asset_bundles.json`: `npm run build`.
- Dev (`ENV=dev`) serves the plain sources whenever the build is stale, so an edit is never hidden. Prod trusts the build and logs a warning if it is stale.
- `npm run build:check` fails if the build is stale; `scripts/security_check.sh` runs it.
- Add a file to a bundle or add a bundle in `app/asset_bundles.json`.

## New
- tools/build_static.mjs, tools/css-dedupe.mjs, package.json, package-lock.json
- app/asset_bundles.json, app/core/assets.py
- app/static/dist/* and app/build/* (build output; commit them, or run the build on deploy)
- tests/test_assets.py, app/static/js/anim-pause.js, app/static/js/login-extras.js

## Changed
- app/static/js/luxe.js, app/static/css/royale.css, deploy/Caddyfile, app/static/sw.js, app/main.py (/sw.js serves the generated worker), app/core/middleware.py, app/web/deps.py
- templates: base, login, register, live, errors/404, errors/500, chat, dashboard, referrals, sell, bola_draw, bola_lobby, store, change_name
- tests: test_web, test_v3, test_bola_ui, test_zz_chat_store, test_zz_store_v2 (they asserted hand-set `?v=` numbers; two of those already failed before this change)
- scripts/security_check.sh, README.md, .gitignore

## Not covered
- Images referenced from templates or the database (`/static/img/venues/*`, `/arena/*`, icons) keep their names and the 1-hour cache. Fonts and images that the CSS points at are hashed.
- No source maps are emitted.
