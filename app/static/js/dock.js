/* Fight-view dock: chat and the bet slip stop floating over the picture.
   A slim bar sits on the bottom edge of the video (Chat | Bet). Tapping a tab slides that panel in from the side
   (wide stage) or up from the bottom (a phone held upright, full screen). One panel at a time, and the payout
   scene underneath NEVER moves or rescales when a panel opens. While the chat is closed its newest messages drift
   past in a small live feed, like a live stream, so nothing is missed.

   It only drives the existing panels (their collapse buttons), so every chat / betting rule stays where it was.
   When the stage is narrow and not full screen the panels stay stacked under the video, exactly as before. */
(function () {
  'use strict';
  const stage = document.querySelector('[data-stage]');
  const screenEl = stage && stage.querySelector('[data-vp-screen]');
  if (!stage || !screenEl) return;
  const chat = stage.querySelector('[data-fc-overlay]');
  const bet = stage.querySelector('[data-bp]');
  if (!chat && !bet) return;
  const snx = (window.snx = window.snx || {});
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- the bar ---------------------------------------------------------------------------------------------------
  const bar = document.createElement('div');
  bar.className = 'dk-bar';
  bar.innerHTML =
    (chat ? '<button type="button" class="dk-tab dk-chat" data-dk="chat" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg><span>Chat</span><i class="dk-badge" hidden>0</i></button>' : '') +
    (bet ? '<button type="button" class="dk-tab dk-bet" data-dk="bet" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="7" rx="7" ry="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 7v5c0 1.7 3.1 3 7 3s7-1.3 7-3V7M5 12v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5" fill="none" stroke="currentColor" stroke-width="2"/></svg><span data-dk-bet-label>Bet</span></button>' : '');
  const scrim = document.createElement('div'); scrim.className = 'dk-scrim'; scrim.hidden = true;
  const feed = document.createElement('div'); feed.className = 'dk-feed'; feed.setAttribute('aria-hidden', 'true');
  screenEl.append(scrim, feed, bar);
  const tab = (k) => bar.querySelector('[data-dk="' + k + '"]');
  const panelOf = (k) => (k === 'chat' ? chat : bet);
  const toggleOf = (k) => (k === 'chat' ? chat && chat.querySelector('[data-fc-toggle]') : bet && bet.querySelector('[data-bp-toggle]'));
  const isShut = (el) => !el || el.classList.contains('is-collapsed') || getComputedStyle(el).display === 'none';
  const unavailable = (el) => !el || el.style.display === 'none';

  let ready = false;
  // ---- mode: dock (side drawer / bottom sheet) or the old stacked layout -------------------------------------------
  function mode() {
    const fs = stage.classList.contains('is-fs') || document.fullscreenElement === stage;
    const w = screenEl.offsetWidth, h = screenEl.offsetHeight;
    const on = fs || w >= 561;
    const was = stage.classList.contains('dk-on');
    stage.classList.toggle('dk-on', on);
    stage.classList.toggle('dk-sheet', on && (w < 561 || h > w * 1.05));
    stage.classList.toggle('dk-side', on && !(w < 561 || h > w * 1.05));
    stage.classList.toggle('dk-short', on && h < 300);
    stage.style.setProperty('--dk-bar', on ? '52px' : '0px');
    // the panels are positioned against the whole stage (picture + control bar), so tell the CSS where the picture is
    const sr = screenEl.getBoundingClientRect(), tr = stage.getBoundingClientRect();
    stage.style.setProperty('--dk-st', Math.max(0, Math.round(sr.top - tr.top)) + 'px');
    stage.style.setProperty('--dk-sb', Math.max(0, Math.round(tr.bottom - sr.bottom)) + 'px');
    stage.style.setProperty('--dk-sh', Math.round(sr.height) + 'px');
    sync();
    if (snx.fitScene) snx.fitScene();
    // entering the dock (full screen): everything starts folded, the picture first. Leaving it: back to the stacked layout, bet slip open.
    if (on && !was && ready) setTimeout(() => { setOpen('chat', false); setOpen('bet', false); }, 0);
    if (!on && was && ready) setTimeout(() => setOpen('bet', true), 0);
  }

  // ---- open / close (through the panels' own collapse buttons) -----------------------------------------------------
  function setOpen(k, open) {
    const el = panelOf(k), t = toggleOf(k);
    if (!el || !t || unavailable(el)) return;
    if (isShut(el) === !open) return;
    t.click();
  }
  function open(k) {
    const other = k === 'chat' ? 'bet' : 'chat';
    if (stage.classList.contains('dk-on')) setOpen(other, false);
    setOpen(k, true);
  }
  bar.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-dk]'); if (!b) return;
    const k = b.dataset.dk, el = panelOf(k);
    if (unavailable(el)) return;
    if (isShut(el)) open(k); else setOpen(k, false);
  });
  scrim.addEventListener('click', () => { setOpen('chat', false); setOpen('bet', false); });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && stage.classList.contains('dk-on')) { setOpen('chat', false); setOpen('bet', false); }
  });
  snx.dockOpen = open;                                           // fightview opens the bet slip when a game's betting opens

  // ---- keep the bar in step with the panels ------------------------------------------------------------------------
  function sync() {
    ['chat', 'bet'].forEach((k) => {
      const el = panelOf(k), b = tab(k); if (!b) return;
      const gone = unavailable(el), shut = isShut(el);
      b.classList.toggle('is-open', !shut);
      b.classList.toggle('is-off', gone);
      b.setAttribute('aria-expanded', String(!shut));
      b.disabled = gone;
    });
    const dock = stage.classList.contains('dk-on');
    scrim.hidden = !(dock && stage.classList.contains('dk-sheet') && (!isShut(chat) || !isShut(bet)));
    const lab = bar.querySelector('[data-dk-bet-label]');
    if (lab && bet) {
      const g = bet.querySelector('[data-sl="name"]'), gone = unavailable(bet);
      lab.textContent = gone ? 'Betting closed' : 'Bet' + (g && /\d/.test(g.textContent) ? ' · ' + g.textContent.trim() : '');
    }
    feed.hidden = !dock || !isShut(chat) || unavailable(chat);
  }
  [chat, bet].forEach((el) => { if (el && window.MutationObserver) new MutationObserver(sync).observe(el, { attributes: true, attributeFilter: ['class', 'style'] }); });
  if (bet && window.MutationObserver) { const n = bet.querySelector('[data-sl="name"]'); if (n) new MutationObserver(sync).observe(n, { childList: true, characterData: true, subtree: true }); }

  // chat unread count on the tab
  const unread = chat && chat.querySelector('[data-fc-unread]'), badge = bar.querySelector('.dk-badge');
  function syncBadge() { if (!unread || !badge) return; const n = parseInt(unread.textContent, 10) || 0; badge.hidden = unread.hidden || !n; badge.textContent = n > 99 ? '99+' : String(n); }
  if (unread && window.MutationObserver) new MutationObserver(syncBadge).observe(unread, { attributes: true, childList: true, characterData: true, subtree: true });

  // ---- live feed: the newest chat lines while the chat is closed --------------------------------------------------
  const log = chat && chat.querySelector('[data-fc-log]');
  const FEED_MAX = 4, FEED_MS = 9000;
  if (log && window.MutationObserver) {
    new MutationObserver((muts) => {
      muts.forEach((m) => m.addedNodes.forEach((n) => {
        if (!(n instanceof HTMLElement) || !n.classList.contains('fc-msg') || n.classList.contains('no-anim')) return;   // history is not "new"
        const who = n.querySelector('.fc-name'), txt = n.querySelector('.fc-text');
        if (!txt) return;
        const row = document.createElement('div'); row.className = 'dk-msg' + (n.classList.contains('mine') ? ' mine' : '');
        const b = document.createElement('b'); b.textContent = who ? who.textContent.replace(/[:\s]+$/, '') : '';
        const s = document.createElement('span'); s.textContent = txt.textContent;
        row.append(b, s); feed.appendChild(row);
        while (feed.children.length > FEED_MAX) feed.firstElementChild.remove();
        setTimeout(() => { row.classList.add('is-out'); setTimeout(() => row.remove(), reduce ? 0 : 450); }, FEED_MS);
      }));
    }).observe(log, { childList: true });
  }

  // ---- first paint and changes of size / full screen ----------------------------------------------------------------
  if (window.ResizeObserver) new ResizeObserver(mode).observe(screenEl);
  stage.addEventListener('snx:fs', () => setTimeout(mode, 0));
  document.addEventListener('fullscreenchange', () => setTimeout(mode, 0));
  mode(); syncBadge(); window.addEventListener('load', () => { ready = true; });
  if (document.readyState === 'complete') ready = true;
  // a panel that was left open before the dock existed starts closed: the picture comes first
  // fold a panel once its own script has set it up (the bet panel is created a little after the page loads)
  function foldWhenReady(el, k, tries) {
    if (!el) return;
    if (el.dataset.fpReady) { if (stage.classList.contains('dk-on')) setOpen(k, false); sync(); return; }
    if (tries < 40) setTimeout(() => foldWhenReady(el, k, tries + 1), 100);
  }
  foldWhenReady(chat, 'chat', 0); foldWhenReady(bet, 'bet', 0);
})();
