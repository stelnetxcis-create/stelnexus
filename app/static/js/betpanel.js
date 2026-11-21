/* Bet panel inside the fight-view video: the bet controls (Meron / Wala / Draw, amount chips,
   keypad, Bet, short Undo list) as a floating panel. It is the shared bet component (sellcore.js) wrapped in
   the shared floating panel (floatpanel.js): drag, S / M / L, transparency, collapse and a remembered position
   come from there. It posts to /bets/early/place and /bets/early/undo, so every betting rule (KYC, credit, bet mode,
   locked games, idempotency key, cancel window) is enforced by the server exactly as on the normal bet form.
   S = compact (no keypad, no history), M = normal, L = big buttons.
   On the livestream page it is the BET panel: the logged-in account bets for itself, and the server only takes bets before the game starts. */
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
      defaultCollapsed: () => false,                         // betting is the point of the pre-show: open it
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
    // `on` = the game on screen is on air: its early betting is closed, so the whole panel goes away (it is not
    // folded: nothing to show). Switching to an upcoming game calls setLive(false) and the panel is back, open.
    let wasLive = null;
    function setLive(on) {
      on = !!on;
      seller.setLive(on);
      el.classList.toggle('is-live', on);
      el.style.display = on ? 'none' : '';
      if (!on && wasLive !== false) panel.setCollapsed(false, false);
      wasLive = on;
    }
    // maximizing during the pre-show: make sure the panel is open over the video
    const stageEl = el.closest('[data-stage]');
    if (stageEl) stageEl.addEventListener('snx:fs', (ev) => { if (ev.detail && wasLive === false) panel.setCollapsed(false, false); });
    return { panel, seller, setFights: seller.setFights, select: seller.select, setLive };
  };
})();
