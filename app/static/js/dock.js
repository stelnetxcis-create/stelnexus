/* Fight-view dock: chat and the bet slip stop floating over the picture.
   A slim bar sits on the bottom edge of the video (Chat | Bet). Tapping a tab slides that panel in from the side
   (wide stage) or up from the bottom (a phone held upright, full screen). One panel at a time, and the payout
   scene underneath NEVER moves or rescales when a panel opens. While the chat is closed its newest messages drift
   past in a small live feed, like a live stream, so nothing is missed.

   It only drives the existing panels (their collapse buttons), so every chat / betting rule stays where it was.
   Desktop windows that are narrow and not full screen keep the stacked layout, exactly as before.
   A TOUCH device in that state (a phone held upright, not full screen) is "gated": the picture is just the picture. The chat,
   payout card and bet slip are not shown around it, the fight strip on the video does not respond, and a message with a Full
   screen button says where everything is. Tapping the strip shows the message again. Full screen is where you bet and chat. */
(function () {
  'use strict';
  const stage = document.querySelector('[data-stage]');
  const screenEl = stage && stage.querySelector('[data-vp-screen]');
  if (!stage || !screenEl) return;
  const chat = stage.querySelector('[data-fc-overlay]');
  const bet = stage.querySelector('[data-bp]');
  const pbs = stage.querySelector('[data-pbs]');
  if (!chat && !bet && !pbs) return;
  const snx = (window.snx = window.snx || {});
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- the bar ---------------------------------------------------------------------------------------------------
  const bar = document.createElement('div');
  bar.className = 'dk-bar';
  const SVG = {
    pay: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 20V10M12 20V4M19 20v-7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>'
  };
  bar.innerHTML =
    (pbs ? '<button type="button" class="dk-tab dk-pay" data-dk="pay" aria-expanded="true" title="Show / hide the payout">' + SVG.pay + '<span>Payout</span></button>' : '') +
    (chat ? '<button type="button" class="dk-tab dk-chat" data-dk="chat" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg><span>Chat</span><i class="dk-badge" hidden>0</i></button>' : '') +
    (bet ? '<button type="button" class="dk-tab dk-bet" data-dk="bet" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="7" rx="7" ry="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 7v5c0 1.7 3.1 3 7 3s7-1.3 7-3V7M5 12v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5" fill="none" stroke="currentColor" stroke-width="2"/></svg><span data-dk-bet-label>Bet</span></button>' : '') ;
  const fab = document.createElement('button'); fab.type = 'button'; fab.className = 'pbs-fab'; fab.hidden = true;
  fab.innerHTML = SVG.pay + '<span>Payout</span>'; fab.setAttribute('aria-label', 'Show payout');
  const scrim = document.createElement('div'); scrim.className = 'dk-scrim'; scrim.hidden = true;
  const feed = document.createElement('div'); feed.className = 'dk-feed'; feed.setAttribute('aria-hidden', 'true');
  // ---- gate: a phone held upright and not full screen ---------------------------------------------------------------
  const coarse = window.matchMedia ? window.matchMedia('(pointer: coarse)') : { matches: false };
  const gateMsg = document.createElement('div');
  gateMsg.className = 'fv-gate-msg'; gateMsg.setAttribute('role', 'status');
  gateMsg.innerHTML = '<span>Use <b>full screen</b> to bet, chat and see the payout.</span><button type="button" data-fv-gate-full>Full screen</button>';
  let gateToastAt = 0;
  function gateHit() {                                            // someone tried a control that lives in full screen
    gateMsg.classList.remove('is-hit'); void gateMsg.offsetWidth; gateMsg.classList.add('is-hit');
    ['[data-vp-full]', '[data-vp-orient]'].forEach((sel) => {
      const b = stage.querySelector(sel); if (!b) return;
      b.classList.remove('is-nudge'); void b.offsetWidth; b.classList.add('is-nudge');
      setTimeout(() => b.classList.remove('is-nudge'), 1400);
    });
    if (Date.now() - gateToastAt > 2500) {
      gateToastAt = Date.now();
      if (snx.toast) snx.toast('Use full screen to bet, chat and see the payout.', { level: 'info' });
      if (snx.Sound) snx.Sound.play('tick');
      if (navigator.vibrate) navigator.vibrate(25);
    }
  }
  stage.addEventListener('click', (ev) => {                      // capture phase: runs before the strip's own handlers
    if (!stage.classList.contains('fv-gate')) return;
    if (ev.target.closest('[data-fv-gate-full]')) { const f = stage.querySelector('[data-vp-full]'); if (f) f.click(); return; }
    if (ev.target.closest('[data-fv-strip]')) { ev.preventDefault(); ev.stopImmediatePropagation(); gateHit(); }
  }, true);
  screenEl.append(scrim, feed, bar, fab, gateMsg);
  const tab = (k) => bar.querySelector('[data-dk="' + k + '"]');
  const panelOf = (k) => (k === 'chat' ? chat : bet);
  const toggleOf = (k) => (k === 'chat' ? chat && chat.querySelector('[data-fc-toggle]') : bet && bet.querySelector('[data-bp-toggle]'));
  const isShut = (el) => !el || el.classList.contains('is-collapsed') || getComputedStyle(el).display === 'none';
  const unavailable = (el) => !el || el.style.display === 'none';

  // ---- payout card: shown / hidden by the user (remembered for this tab, so it stays hidden from game to game) ----------
  const PKEY = 'snx.pbs.hidden';
  const pbsOn = () => !!pbs && !pbs.classList.contains('is-min');
  const pbsLive = () => !!pbs && !pbs.hidden;                       // the scene is itself hidden while the game is on air
  function setPbs(show) {
    if (!pbs) return;
    pbs.classList.toggle('is-min', !show);
    try { sessionStorage.setItem(PKEY, show ? '0' : '1'); } catch (e) { /* storage blocked */ }
    if (snx.fitScene) snx.fitScene();
    sync();
  }
  try { if (pbs && sessionStorage.getItem(PKEY) === '1') pbs.classList.add('is-min'); } catch (e) { /* ignore */ }
  if (pbs) {
    pbs.addEventListener('click', (ev) => { if (ev.target.closest('[data-pbs-hide]')) setPbs(false); });
    fab.addEventListener('click', () => setPbs(true));
    if (window.MutationObserver) new MutationObserver(() => sync()).observe(pbs, { attributes: true, attributeFilter: ['hidden', 'class'] });
  }
  const chatOpen = () => !!chat && !unavailable(chat) && !isShut(chat);
  const betOpen = () => !!bet && !unavailable(bet) && !isShut(bet);

  let ready = false;
  // ---- wide PC layout: settings, memory of what was open ---------------------------------------------------------
  const WIDE_MIN_W = 1000, WIDE_MIN_H = 420, WKEY = 'snx.dk.wide';
  const fine = window.matchMedia ? window.matchMedia('(hover: hover) and (pointer: fine)') : null;
  const isWide = () => stage.classList.contains('dk-wide');
  let programmatic = false;                                   // true while the dock itself clicks a panel's toggle
  const mem = () => {
    let m = {};
    try { m = JSON.parse(localStorage.getItem(WKEY) || '{}') || {}; } catch (e) { m = {}; }
    return { chat: m.chat !== false, bet: m.bet !== false };   // both open the first time
  };
  function remember() {                                       // the viewer's own choice, kept for next time
    if (!isWide() || !ready) return;
    const m = mem();
    if (chat && !unavailable(chat)) m.chat = chatOpen();
    if (bet && !unavailable(bet)) m.bet = betOpen();
    try { localStorage.setItem(WKEY, JSON.stringify(m)); } catch (e) { /* storage blocked */ }
  }
  const afterUser = () => setTimeout(remember, 0);
  function applyWide(only) {                                  // open what was last open (everything, the first time)
    const m = mem();
    if (!only || only === 'chat') setOpen('chat', m.chat);
    if (!only || only === 'bet') setOpen('bet', m.bet);
  }

  // ---- mode: dock (side drawer / bottom sheet) or the old stacked layout -------------------------------------------
  function mode() {
    const fs = stage.classList.contains('is-fs') || document.fullscreenElement === stage;
    const w = screenEl.offsetWidth, h = screenEl.offsetHeight;
    const on = fs || w >= 561;
    const was = stage.classList.contains('dk-on');
    const wasWide = isWide();
    const wide = on && (!fine || fine.matches) && w >= WIDE_MIN_W && h >= WIDE_MIN_H;
    stage.classList.toggle('dk-wide', wide);
    stage.classList.toggle('dk-on', on);
    stage.classList.toggle('dk-sheet', on && (w < 561 || h > w * 1.05));
    stage.classList.toggle('dk-side', on && !(w < 561 || h > w * 1.05));
    stage.classList.toggle('dk-short', on && h < 300);
    stage.classList.toggle('fv-gate', !on && coarse.matches);   // touch device, narrow, not full screen
    stage.style.setProperty('--dk-bar', on ? '52px' : '0px');
    // the panels are positioned against the whole stage (picture + control bar), so tell the CSS where the picture is
    const sr = screenEl.getBoundingClientRect(), tr = stage.getBoundingClientRect();
    // wide layout: how tall the payout card is, so the lower-left bet slip never runs into it
    const pbsBox = pbs && pbs.querySelector('.pbs-stage');
    const pbsH = wide && pbsBox && pbsLive() && pbsOn() ? Math.round(pbsBox.offsetHeight || 0) : 0;
    stage.style.setProperty('--dk-pbs-h', pbsH + 'px');
    stage.classList.toggle('dk-tight', wide && (h - 52 - pbsH - 60) < 470);
    stage.style.setProperty('--dk-st', Math.max(0, Math.round(sr.top - tr.top)) + 'px');
    stage.style.setProperty('--dk-sb', Math.max(0, Math.round(tr.bottom - sr.bottom)) + 'px');
    stage.style.setProperty('--dk-sh', Math.round(sr.height) + 'px');
    sync();
    if (snx.fitScene) snx.fitScene();
    // entering the dock (full screen): everything starts folded, the picture first. Leaving it: back to the stacked layout, bet slip open.
    if (wide && !wasWide && ready) setTimeout(() => applyWide(), 0);             // room for everything: open what was open
    else if (on && !was && ready && !wide) setTimeout(() => { setOpen('chat', false); setOpen('bet', false); }, 0);
    else if (on && wasWide && !wide && ready) setTimeout(() => { setOpen('chat', false); setOpen('bet', false); }, 0);   // back to the one-panel dock
    if (!on && was && ready) setTimeout(() => setOpen('bet', true), 0);
  }

  // ---- open / close (through the panels' own collapse buttons) -----------------------------------------------------
  function setOpen(k, open) {
    const el = panelOf(k), t = toggleOf(k);
    if (!el || !t || unavailable(el)) return;
    if (isShut(el) === !open) return;
    programmatic = true;
    try { t.click(); } finally { programmatic = false; }
  }
  function open(k) {
    const other = k === 'chat' ? 'bet' : 'chat';
    if (stage.classList.contains('dk-on') && !isWide()) setOpen(other, false);       // one at a time, except in the wide layout
    setOpen(k, true);
  }
  bar.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-dk]'); if (!b) return;
    const k = b.dataset.dk;
    if (k === 'pay') { if (pbsLive()) setPbs(!pbsOn()); return; }
    const el = panelOf(k);
    if (unavailable(el)) return;
    if (isShut(el)) open(k); else setOpen(k, false);
    afterUser();
  });
  [['chat', chat], ['bet', bet]].forEach(([k, el]) => {          // the panels' own chevrons count as the viewer's choice too
    const t = toggleOf(k);
    if (t) t.addEventListener('click', () => { if (!programmatic) afterUser(); });
  });
  scrim.addEventListener('click', () => { setOpen('chat', false); setOpen('bet', false); });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && stage.classList.contains('dk-on') && !isWide()) { setOpen('chat', false); setOpen('bet', false); }
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
    const pb = tab('pay');
    if (pb) { const live = pbsLive(); pb.classList.toggle('is-open', live && pbsOn()); pb.classList.toggle('is-off', !live); pb.disabled = !live; pb.setAttribute('aria-expanded', String(live && pbsOn())); }
    stage.classList.toggle('dk-drawer', chatOpen() || betOpen());
    stage.classList.toggle('dk-pbs', pbsLive() && pbsOn());
    fab.hidden = dock || !pbs || !pbsLive() || pbsOn();
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
  window.addEventListener('orientationchange', () => setTimeout(mode, 200));
  document.addEventListener('fullscreenchange', () => setTimeout(mode, 0));
  mode(); syncBadge(); window.addEventListener('load', () => { ready = true; });
  if (document.readyState === 'complete') ready = true;
  // a panel that was left open before the dock existed starts closed: the picture comes first
  // fold a panel once its own script has set it up (the bet panel is created a little after the page loads)
  function foldWhenReady(el, k, tries) {
    if (!el) return;
    if (el.dataset.fpReady) {
      if (isWide()) applyWide(k);                              // wide PC: open what was last open (both, the first time)
      else if (stage.classList.contains('dk-on')) setOpen(k, false);
      sync(); return;
    }
    if (tries < 40) setTimeout(() => foldWhenReady(el, k, tries + 1), 100);
  }
  foldWhenReady(chat, 'chat', 0); foldWhenReady(bet, 'bet', 0);
})();
