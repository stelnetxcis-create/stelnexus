# Fight view: phone update (livestream_ui_update_plan)

1. Orientation button (`data-vp-orient`, touch screens only): full screen + lock landscape; tap again to flip landscape/portrait.
   iPhone Safari cannot lock: stage is rotated with CSS (`.is-rot`, only in the fixed-position fallback).
2. `static/js/floatpanel.js`: shared drag / S-M-L / transparency / collapse / saved position. Chat uses prefix `snx.fc.` (saved settings kept), bet panel `snx.bp.`.
3. Bet panel (`_betpanel.html`, `betpanel.js`, `sellcore.js`): Seller Desk controls over the video, posting to the existing `/sell/place` and `/sell/undo`.
   Shown only to accounts with `Perm.SELL_DESK` (same as /sell). `sell.js` now just mounts the same component.
4. Game strip (`_lobby.html: game_strip`, `fightview.js`): chips replace the Games card on a game's page; switching is in place (URL, chat, bet panel, fight-panel region); video keeps playing.
5. Phone layout in `store.css` (end of file): safe areas, short-landscape two-column bet panel, panels stack/collapse on narrow stages.
Also fixed: collapsed chat pill was 2px wide on narrow stages; page entrance animation broke `position:fixed` full-screen fallback.
Not testable here: real iOS/Android orientation lock and notch insets.
