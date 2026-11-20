/* Bet panel inside the fight-view video: the Seller Desk controls (player, Meron / Wala / Draw, amount chips,
   keypad, Sell, short Undo list) as a floating panel. It is the shared seller component (sellcore.js) wrapped in
   the shared floating panel (floatpanel.js): drag, S / M / L, transparency, collapse and a remembered position
   come from there. It posts to the existing /sell/place and /sell/undo, so every betting rule (KYC, credit, bet mode,
   locked games, idempotency key, cancel window) is enforced by the server exactly as on the Seller Desk.
   S = compact (no keypad, no history), M = normal, L = big buttons.
   On the livestream page it is the EARLY BET panel: the server only takes these bets before the stream starts. */
(function () {
  'use strict';
  const snx = (window.snx = window.snx || {});

  snx.betPanel = function betPanel(el, opts) {
    opts = opts || {};
    const stage = el.closest('[data-stage]');
    const screenEl = stage.querySelector('[data-vp-screen]') || stage;
    const q = (s) => el.querySelector(s);
    const phone = () => window.matchMedia('(max-width:640px)').matches;
    const panel = snx.floatPanel(el, {
      store: 'snx.bp.', label: 'bet panel', stage, screen: screenEl,
      head: q('.fc-head'), grip: q('[data-bp-grip]'), toggle: q('[data-bp-toggle]'),
      cog: q('[data-bp-cog]'), settings: q('[data-bp-set]'),
      sizeBtns: el.querySelectorAll('[data-fp-size]'), alpha: q('[data-bp-alpha]'),
      alphaOut: q('[data-bp-alpha-out]'), resetBtn: q('[data-bp-reset]'),
      corner: { fx: 1, fy: 1 },                              // bottom right: chat owns the top left, the LIVE badge the top right
      defaultSize: () => (phone() ? 's' : 'm'),
      defaultCollapsed: () => false,                         // early betting is the point of the pre-show: open it
      onLayout: () => short(),
    });
    // a phone held sideways (floating over a wide, short picture): two columns, no wasted height.
    // Not when the panel is stacked under the video: there it has the whole page width.
    function short() {
      const floating = getComputedStyle(el).position === 'absolute';
      el.toggleAttribute('data-short', floating && screenEl.offsetWidth >= 520 && screenEl.offsetHeight > 0 && screenEl.offsetHeight < 420);
    }
    short();
    const seller = snx.Seller(q('[data-sl-root]'), { panel: true, onPick: opts.onPick });
    // once the stream has started the panel is of no use: say so and fold it away.
    // Back in pre-show (or first paint before the stream) it opens, whatever collapsed state was remembered.
    let wasLive = null;
    function setLive(on) {
      on = !!on;
      seller.setLive(on);
      el.classList.toggle('is-live', on);
      const note = q('[data-bp-ended]'); if (note) note.hidden = !on;
      if (on) panel.setCollapsed(true, false);
      else if (wasLive !== false) panel.setCollapsed(false, false);
      wasLive = on;
    }
    // maximizing during the pre-show: make sure the panel is open over the video
    const stageEl = el.closest('[data-stage]');
    if (stageEl) stageEl.addEventListener('snx:fs', (ev) => { if (ev.detail && wasLive === false) panel.setCollapsed(false, false); });
    return { panel, seller, setFights: seller.setFights, select: seller.select, setLive };
  };
})();
