/* Bet panel inside the fight-view video: the bet controls (Meron / Wala / Draw, amount chips,
   keypad, Bet, short Undo list) as a floating panel. It is the shared bet component (betcore.js) wrapped in
   the shared floating panel (floatpanel.js): drag, S / M / L, transparency, collapse and a remembered position
   come from there. It posts to /bets/live/place and /bets/live/undo, so every betting rule (KYC, credit, bet mode,
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
      if (!on && wasLive !== false && !(stage.classList.contains('dk-on'))) panel.setCollapsed(false, false);   // in the dock the Bet tab opens it
      wasLive = on;
    }
    // maximizing during the pre-show: make sure the panel is open over the video
    const stageEl = el.closest('[data-stage]');
    if (stageEl) stageEl.addEventListener('snx:fs', (ev) => { if (ev.detail && wasLive === false && !stage.classList.contains('dk-on')) panel.setCollapsed(false, false); });
    // dock mode: no keypad by default; "Other" opens it. A line under the Bet button says what the bet could win.
    const other = q('[data-bp-other]'), est = q('[data-bp-est]'), amtEl = q('[data-sl="amt"]');
    if (other) other.addEventListener('click', () => {
      const on = el.classList.toggle('show-pad'); other.setAttribute('aria-pressed', String(on));
    });
    function estimate() {
      if (!est) return;
      const side = ['meron', 'wala'].find((k) => { const b = q('[data-sl="side-' + k + '"]'); return b && b.classList.contains('is-on'); });
      const amt = Number(((amtEl && amtEl.textContent) || '').replace(/[^\d]/g, '')) || 0;
      const bps = side ? Number(el.dataset['x' + side]) || 0 : 0;
      est.hidden = !(side && amt && bps);
      if (!est.hidden) est.textContent = 'Wins about \u20B1' + Math.floor(amt * bps / 10000).toLocaleString('en-PH') + ' at ' + (bps / 10000).toFixed(2) + 'x \u00B7 moves as bets come in';
    }
    if (window.MutationObserver) {
      const mo = new MutationObserver(estimate);
      if (amtEl) mo.observe(amtEl, { childList: true, characterData: true, subtree: true });
      el.querySelectorAll('[data-sl^="side-"]').forEach((b) => mo.observe(b, { attributes: true, attributeFilter: ['class'] }));
    }
    el.addEventListener('snx:odds', estimate);
    return { panel, seller, setFights: seller.setFights, select: seller.select, setLive };
  };
})();
