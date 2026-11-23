/* Fight view glue: the game strip, switching games without reloading the video, and the one live poll that feeds
   the strip and the bet panel.

   - The strip is rendered by the server (so it works at first paint); this file recolours it from /_live/event/ID
     every few seconds and adds chips for games created while you watch.
   - Tapping a chip switches game in place: the URL changes (back button works), the video keeps running, the chat
     re-points at the new room (snx.fightChat.switchFight), the bet panel follows (select), and the "fight-panel" box
     (pool, declare form, tickets) is refreshed through the page's partial-refresh code (StelNexus.refreshRegion).
   - When the game on screen changes state (locked, declared) the same box is refreshed, as the old games grid did. */
(function () {
  'use strict';
  const strip = document.querySelector('[data-fv-strip]');
  const stage = document.querySelector('[data-stage]');
  if (!strip || !stage) return;
  const snx = window.snx || {};
  const eventId = Number(strip.dataset.event);
  const player = stage.querySelector('[data-player]');
  let activeId = Number(strip.dataset.active) || 0;
  let fights = [], hide = false, keys = new Map(), first = true;

  const bpEl = document.querySelector('[data-bp]');
  const bp = bpEl && snx.betPanel ? snx.betPanel(bpEl, { onPick: (id) => go(id, true) }) : null;
  if (bp) bp.select(activeId);

  // ONE rule, game by game (server: events.stream_phase). Each row of the poll carries `ph`:
  //   open     the current game, not started: bet panel + chat are shown
  //   onair    the current game is on air (or locked): bets and chat are closed until it is declared
  //   waiting  a later game: opens only when every game before it is done (2 after 1, 3 after 2 ...)
  //   ended    declared / void
  // The strip stays visible in every phase, so you can still look at, or jump to, any game.
  const liveBar = document.querySelector('[data-fv-live]');
  const chatRoot = document.querySelector('[data-fightchat]');
  const chatOverlay = chatRoot && chatRoot.querySelector('[data-fc-overlay]');
  let lastPh = null, curId = null;
  const phaseOf = (f) => (f && f.ph) || 'open';
  const numOf = (id) => { const g = fights.find((x) => x.id === id); return g ? g.n : ''; };
  function expandPanels() {
    if (stage.classList.contains('dk-on') && window.snx && snx.dockOpen) { snx.dockOpen('bet'); return; }   // dock: just the bet slip
    // chat first, bet panel last: on a narrow phone only one panel fits, and the bet panel is the one that matters
    const els = [...stage.querySelectorAll('.fc-overlay.is-collapsed, .bp.is-collapsed')]
      .filter((o, i, all) => all.indexOf(o) === i && !o.hidden && getComputedStyle(o).display !== 'none');
    els.sort((a, b) => Number(a.classList.contains('bp')) - Number(b.classList.contains('bp')));
    els.forEach((o) => { const t = o.querySelector('[aria-expanded="false"]'); if (t) t.click(); });
  }
  function setPhase(ph, f) {
    const open = ph === 'open';
    if (bp) bp.setLive(!open);
    if (chatOverlay) chatOverlay.style.display = (ph === 'onair' || ph === 'waiting') ? 'none' : '';   // an ended game keeps its chat history readable
    if (chatRoot) chatRoot.classList.toggle('is-onair', ph === 'onair');
    // "going live" = the SAME game moving open -> onair (staff pressed Start); switching games is not
    if (lastPh !== null && lastPh !== ph && (ph === 'onair' || lastPh === 'onair'))
      document.dispatchEvent(new CustomEvent('snx:live', { detail: { on: ph === 'onair' } }));
    // a game's betting window opens (it is now the current game): unfold the chat and bet panels so nobody misses it
    if (lastPh !== null && lastPh !== ph && ph === 'open') setTimeout(expandPanels, 60);
    lastPh = ph;
    if (!liveBar) return;
    liveBar.dataset.live = ph === 'onair' ? '1' : '0';
    liveBar.dataset.phase = ph;
    const n = f ? f.n : '';
    const t = liveBar.querySelector('[data-fv-live-text]');
    if (t) t.textContent =
      ph === 'open' ? 'Game #' + n + ' \u00B7 pre-show \u00B7 betting and chat are open'
      : ph === 'onair' ? (f && f.sd ? 'Game #' + n + ' is on air \u00B7 betting and chat are closed until it is done'
                                    : 'Game #' + n + ' \u00B7 betting is closed')
      : ph === 'waiting' ? 'Game #' + n + ' opens after game #' + (numOf(curId) || '?') + ' is done'
      : 'Game #' + n + ' \u00B7 ended';
    liveBar.querySelectorAll('[data-fv-live-form]').forEach((form) => {
      form.hidden = form.dataset.fvLiveForm === 'start' ? ph !== 'open' : !(ph === 'onair' && f && f.sd);
      const fid = form.querySelector('input[name="fight_id"]'); if (fid && f) fid.value = f.id;
    });
  }
  const activeFight = () => fights.find((x) => x.id === activeId);
  if (liveBar) {                                     // server state at first paint, before the first poll
    const n0 = Number((strip.querySelector('.is-active') || {}).dataset?.n) || '';
    setPhase(liveBar.dataset.phase || 'open', { id: activeId, n: n0, sd: liveBar.dataset.live === '1' });
  }

  // ---- payout scene in the video area ----
  //   open     live multipliers / pools that COUNT UP as bets come in (+₱ chips, ▲▼ on the multiple)
  //   waiting  "up next" card for a game whose turn has not come
  //   ended    how it ended: winner, final multiple, totals (from /_live/fight/ID/final, the applied settlement)
  //   hidden   on air: the video shows
  const scene = document.querySelector('[data-pbs]');
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const peso = (c) => '\u20B1' + (Number(c || 0) / 100).toLocaleString('en-PH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtNum = (kind, v) => kind === 'x' ? (v ? (v / 10000).toFixed(2) : '\u2014')
    : kind === 'peso' ? peso(v) : kind === 'tk' ? Math.round(v) + (Math.round(v) === 1 ? ' ticket' : ' tickets') : String(Math.round(v));
  const q1 = (sel) => scene.querySelector(sel);
  const textOf = (k, v) => { const e = q1('[data-pb="' + k + '"]'); if (e && e.textContent !== v) e.textContent = v; };
  // count a number up/down to its new value; `snap` sets it instantly (new game, reduced motion)
  function setNum(k, to, snap, fromZero) {
    const e = q1('[data-pbn="' + k + '"]'); if (!e) return 0;
    const kind = e.dataset.fmt;
    const cur = e._v !== undefined ? e._v : Number(e.dataset.v || 0);
    if (fromZero) e._v = 0;
    const from = fromZero ? 0 : cur;
    if (e._raf) cancelAnimationFrame(e._raf);
    if (snap || reduceMotion || from === to) { e._v = to; e.textContent = fmtNum(kind, to); return to - cur; }
    const t0 = performance.now(), dur = Math.min(900, 350 + Math.abs(to - from) / 40);
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur), ease = 1 - Math.pow(1 - p, 3);
      e._v = from + (to - from) * ease; e.textContent = fmtNum(kind, e._v);
      if (p < 1) e._raf = requestAnimationFrame(step); else { e._v = to; e.textContent = fmtNum(kind, to); }
    };
    e._raf = requestAnimationFrame(step);
    return to - cur;
  }
  function pulse(side, deltaCents, bpsDelta) {
    const box = q1('[data-pb-side="' + side + '"]'); if (!box) return;
    if (deltaCents > 0) {
      box.classList.remove('pbs-hit'); void box.offsetWidth; box.classList.add('pbs-hit');
      const chip = document.createElement('span'); chip.className = 'pbs-plus'; chip.textContent = '+' + peso(deltaCents);
      box.appendChild(chip); setTimeout(() => chip.remove(), 1700);
    }
    const dir = q1('[data-pb-dir="' + side + '"]');
    if (dir && bpsDelta) {
      dir.textContent = bpsDelta > 0 ? '\u25B2' : '\u25BC'; dir.className = 'pbs-dir ' + (bpsDelta > 0 ? 'up' : 'down');
      clearTimeout(dir._t); dir._t = setTimeout(() => { dir.textContent = ''; dir.className = 'pbs-dir'; }, 3200);
    }
  }
  let sceneFight = null, finalKey = '', finalReq = 0;
  const FINALS = new Map();
  function paintFinal(d) {
    const win = d.res === 'meron' || d.res === 'wala';
    textOf('rh', d.res === 'meron' ? 'MERON WINS' : d.res === 'wala' ? 'WALA WINS' : d.res === 'draw' ? 'DRAW' : 'CANCELLED');
    const w = q1('[data-pb="rw"]');
    if (w) {
      const nm = d.res === 'meron' ? d.nm : d.res === 'wala' ? d.nw : (d.res === 'draw' ? 'Stakes refunded' : (d.auto_cancel ? 'Odds too low: stakes refunded' : 'Stakes refunded'));
      const role = win && d.lla ? (d.lla === d.res ? 'Llamado' : 'Dehado') : '';
      w.textContent = nm; if (role) { const em = document.createElement('em'); em.textContent = role; w.appendChild(document.createTextNode(' ')); w.appendChild(em); }
    }
    scene.dataset.res = d.res || '';
    if (d.hide) return;
    setNum('fx', win ? d.mult : 0, false, true); setNum('ftt', d.tt || 0, false, true);
    setNum('fpaid', d.paid || 0, false, true); setNum('fwin', d.winners || 0, false, true); setNum('fcut', d.cut || 0, false, true); setNum('fpc', d.pc || 0, false, true);
  }
  async function loadFinal(f) {
    const k = f.id + ':' + (f.at || '');
    if (k === finalKey) return;
    finalKey = k;
    const req = ++finalReq;
    try {
      let d = FINALS.get(k);
      if (!d) {
        const r = await fetch('/_live/fight/' + f.id + '/final', { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
        if (!r.ok) throw new Error(r.status);
        d = await r.json(); FINALS.set(k, d);
      }
      if (req === finalReq && activeId === f.id) paintFinal(d);
    } catch (e) { finalKey = ''; }
  }

  // The chat and bet overlays float over the video. Keep the scene's content in the room that is left between
  // them (and re-fit when they are moved, resized, collapsed or hidden); too little room = overlap a little.
  let fitQ = 0;
  function fit() {
    fitQ = 0;
    if (!scene || scene.hidden) return;
    const sr = scene.getBoundingClientRect(); if (!sr.width) return;
    if (stage.classList.contains('dk-on')) {
      // dock mode: panels slide OVER the scene, so the scene never reflows; it only keeps clear of the dock bar
      scene.style.setProperty('--pbs-l', '14px'); scene.style.setProperty('--pbs-r', '14px');
      // Measure instead of trusting CSS: clamp the scene to what is really on screen (mobile browsers can make the
      // fullscreen box taller than the visible area), keep clear of the dock bar, and shrink the content to fit.
      const vv = window.visualViewport, vh = vv ? vv.height : window.innerHeight;
      const bar = stage.querySelector('.dk-bar'), br = bar ? bar.getBoundingClientRect() : null;
      const barH = br && br.height ? br.height : 52;
      const off = Math.max(0, sr.bottom - vh);
      const T = 8, Bm = Math.round(barH + 8 + off);
      scene.style.setProperty('--pbs-t', T + 'px'); scene.style.setProperty('--pbs-b', Bm + 'px');
      const live = Array.from(scene.querySelectorAll('.pbs-pane')).find((e) => getComputedStyle(e).display !== 'none');
      const box = scene.querySelector('.pbs-stage');
      if (live && box) {
        box.style.transform = ''; box.style.transformOrigin = '50% 0';
        const room = sr.height - T - Bm - 4, need = live.scrollHeight;
        const k = need > room && room > 60 ? Math.max(0.55, room / need) : 1;
        if (k < 1) box.style.transform = 'scale(' + k.toFixed(3) + ')';
        box.style.alignSelf = k < 1 ? 'start' : '';
      }
      return;
    }
    const boxes = [];
    stage.querySelectorAll('.fc-overlay').forEach((o) => {
      if (o.hidden || getComputedStyle(o).display === 'none') return;
      const r = o.getBoundingClientRect(); if (!r.width || !r.height) return;
      boxes.push({ c: o.classList.contains('is-collapsed'), l: r.left - sr.left, r: r.right - sr.left, t: r.top - sr.top, b: r.bottom - sr.top });
    });
    const MIN = 200;
    let L = 0, R = sr.width, T = 0, B = sr.height;
    boxes.forEach((o) => { if (o.c) return; if (o.l + o.r < sr.width) L = Math.max(L, o.r); else R = Math.min(R, o.l); });   // a folded pill is small: ignore it sideways
    if (R - L < MIN) {
      // Not enough room sideways (a phone held upright): use the band above / below the panels instead.
      L = 0; R = sr.width;
      boxes.forEach((o) => { if (o.t + o.b < sr.height) T = Math.max(T, o.b); else B = Math.min(B, o.t); });
      if (B - T < 130) { const mid = (T + B) / 2; T = Math.max(0, mid - 65); B = Math.min(sr.height, T + 130); }
    }
    scene.style.setProperty('--pbs-l', Math.round(L + 10) + 'px');
    scene.style.setProperty('--pbs-r', Math.round(sr.width - R + 10) + 'px');
    scene.style.setProperty('--pbs-t', Math.round(T + 6) + 'px');
    scene.style.setProperty('--pbs-b', Math.round(sr.height - B + 6) + 'px');
  }
  const queueFit = () => { if (!fitQ) fitQ = requestAnimationFrame(fit); };
  window.snx = window.snx || {}; window.snx.fitScene = () => queueFit();
  if (scene) {
    if (window.ResizeObserver) { const ro = new ResizeObserver(queueFit); ro.observe(scene); stage.querySelectorAll('.fc-overlay').forEach((o) => ro.observe(o)); }
    if (window.MutationObserver) { const mo = new MutationObserver(queueFit); stage.querySelectorAll('.fc-overlay').forEach((o) => mo.observe(o, { attributes: true, attributeFilter: ['class', 'style', 'hidden'] })); }
    window.addEventListener('resize', queueFit); if (window.visualViewport) window.visualViewport.addEventListener('resize', queueFit); document.addEventListener('fullscreenchange', () => setTimeout(queueFit, 120)); document.addEventListener('pointerup', () => setTimeout(queueFit, 60));
    queueFit();
  }

  // the bet slip shows each side's live multiple (and MAX), so the payout is visible even while the slip covers the scene
  const slip = document.querySelector('[data-bp]');
  function paintSlip(f) {
    if (!slip || !f) return;
    [['meron', f.mm, f.xm], ['wala', f.mw, f.xw]].forEach(([side, bps, capped]) => {
      const e = slip.querySelector('[data-bp-x="' + side + '"]'); if (!e) return;
      e.textContent = bps ? (bps / 10000).toFixed(2) + 'x' + (capped ? ' MAX' : '') : '';
      e.classList.toggle('is-max', !!capped);
      slip.dataset['x' + side] = bps || 0;
    });
    slip.dispatchEvent(new CustomEvent('snx:odds'));
  }
  function paintScene(f) {
    if (!scene || !f) return;
    paintSlip(f);
    const ph = phaseOf(f);
    const st = ph === 'open' ? 'open' : ph === 'onair' ? (f.sd ? 'hidden' : 'open') : ph === 'waiting' ? 'waiting' : 'ended';
    scene.hidden = st === 'hidden';
    queueFit();
    if (scene.dataset.state !== st) { scene.dataset.state = st; if (st !== 'ended') finalKey = ''; }
    const fresh = sceneFight !== f.id; sceneFight = f.id;
    if (st === 'open') {
      textOf('kick', ph === 'open' ? 'Betting open' : 'Betting closed'); textOf('gn', String(f.n));
      textOf('nm', f.nm); textOf('nw', f.nw);
      textOf('tm', f.lla ? (f.lla === 'meron' ? 'Llamado' : 'Dehado') : ''); textOf('tw', f.lla ? (f.lla === 'wala' ? 'Llamado' : 'Dehado') : '');
      if (f.pm !== undefined) {
        const dm = setNum('pm', f.pm, fresh), dw = setNum('pw', f.pw, fresh);
        const xm = setNum('mm', f.mm, fresh), xw = setNum('mw', f.mw, fresh);
        setNum('cm', f.cm, fresh); setNum('cw', f.cw, fresh); setNum('tt', f.tt, fresh);
        setNum('np', f.fx ? 0 : (f.np || 0), fresh); setNum('cut', f.fx ? 0 : (f.cut || 0), fresh); setNum('pc', f.fx ? 0 : (f.pc || 0), fresh); setNum('gt', f.fx ? 0 : (f.gt || 0), fresh);
        if (!fresh) { pulse('meron', dm, xm); pulse('wala', dw, xw); }
        const tot = (f.pm + f.pw) || 1;
        q1('[data-pb-bar-m]').style.width = (f.pm / tot * 100).toFixed(1) + '%';
        q1('[data-pb-bar-w]').style.width = (f.pw / tot * 100).toFixed(1) + '%';
        q1('[data-pb-side="meron"]').classList.toggle('is-low', !!f.lm);
        q1('[data-pb-side="wala"]').classList.toggle('is-low', !!f.lw);
        const sm = q1('[data-pb-side="meron"]'), sw = q1('[data-pb-side="wala"]');
        sm.classList.toggle('is-max', !!f.xm); sw.classList.toggle('is-max', !!f.xw);
        sm.dataset.max = f.am ? (f.am / 10000).toFixed(2) + 'x' : ''; sw.dataset.max = f.aw ? (f.aw / 10000).toFixed(2) + 'x' : '';
      }
    } else if (st === 'waiting') {
      textOf('gn2', String(f.n)); textOf('nm2', f.nm); textOf('nw2', f.nw);
      textOf('waitmsg', 'Betting opens after game #' + (numOf(curId) || '?') + ' is done');
    } else if (st === 'ended') {
      textOf('gn3', String(f.n));
      loadFinal(f);
    }
  }
  const key = (f) => (f.res ? 'res:' + f.res : 'st:' + f.st);
  const ended = (f) => !!f.res || f.st === 'settled' || f.st === 'void';
  const url = (id) => '/change_name?event_id=' + eventId + '&fight_id=' + id;
  const chipOf = (id) => strip.querySelector('[data-fight="' + id + '"]');

  function paintChip(a, f) {
    a.className = 'fv-chip st-' + f.st + (f.res ? ' res-' + f.res : '') + (f.id === activeId ? ' is-active' : '') + (f.hot ? ' is-hot' : '') + (f.sd && !ended(f) ? ' is-air' : '') + ' ph-' + (f.ph || 'open');
    a.dataset.n = f.n; a.dataset.m = f.nm; a.dataset.w = f.nw; a.dataset.st = f.st; a.dataset.res = f.res || '';
    a.title = 'Game ' + f.n + ' · ' + f.nm + ' vs ' + f.nw + ' · ' + (f.res || f.st).toUpperCase();
    if (f.id === activeId) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
  }

  function renderStrip() {
    const list = strip.querySelector('[data-fv-list]') || strip;
    const have = new Set();
    fights.forEach((f) => {
      let a = chipOf(f.id);
      if (!a) {                                   // a game created while watching
        a = document.createElement('a');
        a.dataset.fight = f.id; a.href = url(f.id);
        const b = document.createElement('b'); b.textContent = f.n; a.appendChild(b);
        const nodes = Array.from(list.querySelectorAll('[data-fight]')).filter((x) => Number(x.dataset.n) > f.n);
        list.insertBefore(a, nodes[0] || null);
      }
      paintChip(a, f); have.add(String(f.id));
    });
    Array.from(list.querySelectorAll('[data-fight]')).forEach((a) => { if (!have.has(a.dataset.fight)) a.remove(); });
    if (typeof edges === 'function') edges();
  }

  function centerActive() {
    const on = strip.querySelector('.is-active');
    if (on && on.scrollIntoView) { try { on.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' }); } catch (e) { on.scrollIntoView(); } }
  }

  // ---- strip controls for events with hundreds of games: arrows, wheel, drag, "go to current", "go to #" ----
  const listEl = strip.querySelector('[data-fv-list]');
  const prevB = strip.querySelector('[data-fv-prev]'), nextB = strip.querySelector('[data-fv-next]');
  const nowB = strip.querySelector('[data-fv-now]'), jumpI = strip.querySelector('[data-fv-jump]');
  function edges() {
    if (!listEl) return;
    if (prevB) prevB.disabled = listEl.scrollLeft <= 2;
    if (nextB) nextB.disabled = listEl.scrollLeft + listEl.clientWidth >= listEl.scrollWidth - 2;
  }
  if (listEl) {
    const page = (dir) => listEl.scrollBy({ left: dir * Math.max(120, listEl.clientWidth * 0.8), behavior: 'smooth' });
    if (prevB) prevB.addEventListener('click', () => page(-1));
    if (nextB) nextB.addEventListener('click', () => page(1));
    listEl.addEventListener('scroll', edges, { passive: true });
    window.addEventListener('resize', edges);
    // mouse wheel scrolls the strip sideways (a plain wheel only moves vertically)
    listEl.addEventListener('wheel', (ev) => {
      if (Math.abs(ev.deltaY) <= Math.abs(ev.deltaX)) return;
      const max = listEl.scrollWidth - listEl.clientWidth;
      if ((ev.deltaY < 0 && listEl.scrollLeft <= 0) || (ev.deltaY > 0 && listEl.scrollLeft >= max)) return;
      ev.preventDefault(); listEl.scrollLeft += ev.deltaY;
    }, { passive: false });
    // click-and-drag with a mouse (touch already scrolls natively)
    let down = null, dragged = false;
    listEl.addEventListener('pointerdown', (ev) => { if (ev.pointerType === 'mouse' && ev.button === 0) { down = { x: ev.clientX, left: listEl.scrollLeft, id: ev.pointerId }; dragged = false; } });
    listEl.addEventListener('pointermove', (ev) => {
      if (!down) return;
      const dx = ev.clientX - down.x;
      if (!dragged && Math.abs(dx) > 5) { dragged = true; listEl.classList.add('is-drag'); try { listEl.setPointerCapture(down.id); } catch (e) { /* ignore */ } }
      if (dragged) listEl.scrollLeft = down.left - dx;
    });
    const endDrag = () => { down = null; listEl.classList.remove('is-drag'); };
    listEl.addEventListener('pointerup', endDrag); listEl.addEventListener('pointercancel', endDrag);
    listEl.addEventListener('click', (ev) => { if (dragged) { ev.preventDefault(); ev.stopPropagation(); dragged = false; } }, true);
  }
  if (nowB) nowB.addEventListener('click', () => {
    if (curId && curId !== activeId) go(curId, true); else centerActive();
  });
  if (jumpI) {
    jumpI.addEventListener('keydown', (ev) => {
      ev.stopPropagation();                          // typing a number must not trigger player shortcuts
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      const n = Number(jumpI.value);
      const f = fights.find((x) => x.n === n);
      if (!f) { if (snx.toast) snx.toast('There is no game #' + (jumpI.value || '?') + ' in this event.', { level: 'warning', ttl: 3000 }); return; }
      jumpI.value = ''; jumpI.blur();
      if (f.id === activeId) centerActive(); else go(f.id, true);
    });
  }

  async function refreshPanel(force) {
    const api = window.StelNexus;
    if (!api || !api.refreshRegion) { location.reload(); return; }
    const r = await api.refreshRegion('fight-panel', { force: !!force });
    if (r === 'failed') location.reload();
    else if (r === 'skipped' && snx.toast) snx.toast('This game changed.', { level: 'info', ttl: 12000, action: { label: 'Refresh', run: () => refreshPanel(true) } });
  }

  function go(id, push) {
    const f = fights.find((x) => x.id === id);
    if (!f || id === activeId) return;
    activeId = id;
    if (push !== false) { try { history.pushState({ fv: id }, '', url(id)); } catch (e) { /* ignore */ } }
    fights.forEach((g) => { const a = chipOf(g.id); if (a) paintChip(a, g); });
    centerActive();
    if (player) player.dispatchEvent(new CustomEvent('snx:game', { detail: { id: f.id, n: f.n, left: f.nm, right: f.nw, ended: ended(f) } }));
    if (snx.fightChat) snx.fightChat.switchFight(f);
    if (bp) bp.select(id);
    lastPh = null;                                  // a different game: not a "going live" event
    setPhase(phaseOf(f), f);                        // panels follow the game now on screen
    paintScene(f);
    if (snx.Sound) snx.Sound.play('tick');
    refreshPanel(true);
  }

  strip.addEventListener('click', (ev) => {
    const a = ev.target.closest('[data-fight]');
    if (!a || ev.defaultPrevented || ev.button > 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    const id = Number(a.dataset.fight);
    if (!fights.some((x) => x.id === id)) return;                // not known yet: let the link do a normal page load
    ev.preventDefault();
    go(id, true);
  });
  window.addEventListener('popstate', () => {
    const id = Number(new URLSearchParams(location.search).get('fight_id'));
    if (id && id !== activeId && fights.some((x) => x.id === id)) go(id, false); else if (id !== activeId) location.reload();
  });

  // A bet or undo answers with that game's fresh pools and live multiples (`row`, same shape as a poll row), so the
  // payout moves with the reply instead of waiting for the next poll. A poll that was ASKED FOR before that reply
  // landed may still carry the old pools, so onData drops it (the next poll, a couple of seconds later, is current).
  let rowAt = 0;
  document.addEventListener('snx:live-row', (e) => {
    const row = e.detail;
    if (!row || !fights.some((x) => x.id === row.id)) return;
    fights = fights.map((x) => (x.id === row.id ? Object.assign({}, x, row) : x));
    rowAt = performance.now();
    renderStrip();
    { const a = activeFight(); if (a) { setPhase(phaseOf(a), a); paintScene(a); } }
    if (bp) bp.setFights(fights, hide);
  });

  function onData(d, _isFirst, sentAt) {
    if (sentAt !== undefined && sentAt < rowAt) return;
    hide = !!d.hide; fights = d.fights;
    const prevCur = curId;
    curId = d.event ? d.event.cur : null;
    renderStrip();
    { const a = activeFight(); if (a) { setPhase(phaseOf(a), a); paintScene(a); } }
    // the next game just opened (the one before it was declared): say so, with a way to jump to it
    if (!first && prevCur && curId && curId !== prevCur && curId !== activeId && snx.toast) {
      snx.toast('Game ' + numOf(curId) + ' is open for bets.', { level: 'info', ttl: 15000, action: { label: 'Go', run: () => go(curId, true) } });
      if (snx.Sound) snx.Sound.play('open');
    }
    if (bp) bp.setFights(fights, hide);
    const active = fights.find((f) => f.id === activeId);
    let changed = null;
    fights.forEach((f) => {
      const k = key(f), was = keys.get(f.id);
      if (!first && was && was !== k) {
        if (f.id === activeId) changed = f;
        if (snx.Sound && f.id !== activeId) snx.Sound.play(f.st === 'open' ? 'open' : 'lock');
      }
      keys.set(f.id, k);
    });
    if (changed) {
      const what = changed.res ? 'was declared ' + changed.res.toUpperCase() : changed.st === 'locked' ? 'betting is now closed' : changed.st === 'open' ? 'betting is open again' : 'changed';
      if (snx.toast) snx.toast('Game ' + changed.n + ' ' + what + '. Updating…', { level: 'info', ttl: 2000 });
      if (player) player.dispatchEvent(new CustomEvent('snx:game', { detail: { id: changed.id, n: changed.n, left: changed.nm, right: changed.nw, ended: ended(changed) } }));
      setTimeout(() => refreshPanel(false), 1200);
    } else if (first && active) { /* first paint is server-rendered */ }
    if (first) { centerActive(); first = false; }
  }

  // 2 s while the game on screen still takes bets (the server shares one build per change, so this is cheap), 3 s otherwise
  const pollEvery = () => { const a = activeFight(); return a && a.st === 'open' && phaseOf(a) === 'open' ? 2000 : 3000; };
  if (snx.poller) snx.poller('/_live/event/' + eventId, (d, isFirst, sentAt) => onData(d, isFirst, sentAt), pollEvery, strip);
})();
