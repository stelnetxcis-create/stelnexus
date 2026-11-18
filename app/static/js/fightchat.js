/* Livestream chat inside the fight view.
   Short-poll (3s, paused in background tabs). The server decides who may type (and how fast: slow mode);
   this file only shows it. Messages are dense Twitch-style lines: [badges] name: text.
   Everything user-written goes in through textContent, never innerHTML. */
(function () {
  const root = document.querySelector('[data-fightchat]');
  if (!root) return;
  const $ = (s) => root.querySelector(s);
  const log = $('[data-fc-log]'), form = $('[data-fc-form]'), input = form.elements.body;
  const conn = $('[data-fc-conn]'), air = $('[data-fc-air]'), jump = $('[data-fc-jump]');
  const count = $('[data-fc-count]'), winner = $('[data-fc-winner]'), resultBox = $('[data-fc-result]');
  const url = root.dataset.url;
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const snx = window.snx || {};
  let settled = false, last = 0, pinned = true, timer = 0, wasClosed = root.classList.contains('is-closed'), busy = false, failures = 0;
  const seen = new Set();

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 70;
  const toBottom = (smooth) => { log.scrollTo({ top: log.scrollHeight, behavior: smooth ? 'smooth' : 'auto' }); pinned = true; jump.hidden = true; };
  log.addEventListener('scroll', () => { pinned = nearBottom(); if (pinned) jump.hidden = true; });
  jump.addEventListener('click', () => toBottom(true));

  const MAX_ROWS = 400;                                // dense lines are cheap: keep a long scrollback
  const PALETTE = ['#ff9aa8', '#7fbcff', '#7ee8bd', '#f2cd7c', '#cdb9ff', '#ffb27a', '#8be3e3', '#e8a0ff'];
  const colorOf = (name) => { let h = 0; for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0; return PALETTE[h % PALETTE.length]; };
  let unread = 0;
  function add(m, fresh, into) {
    if (seen.has(m.id)) return;
    seen.add(m.id);
    const empty = $('[data-fc-empty]'); if (empty) empty.remove();
    const row = el('article', 'fc-msg' + (fresh ? '' : ' no-anim') + ' fc-tone-' + (m.tone || 'slate') + (m.mine ? ' mine' : '') + (m.lux ? ' is-lux' : ''));
    row.appendChild(el('time', 'fc-t', m.at));
    if (m.lux) { const p = el('span', 'fc-pass fc-lux'); p.innerHTML = '<svg class="ico"><use href="#i-crown"/></svg>'; p.appendChild(document.createTextNode('Lux')); row.appendChild(p); }
    else if (m.pass) { const p = el('span', 'fc-pass'); p.innerHTML = '<svg class="ico"><use href="#i-crown"/></svg>'; p.appendChild(document.createTextNode('Pass')); row.appendChild(p); }
    if (m.role && m.role !== 'Management') row.appendChild(el('span', 'fc-role', m.role));
    const name = el('b', 'fc-name', m.user);
    if (m.tone === 'slate' && !m.lux) name.style.color = colorOf(m.user);
    if (m.real) name.title = 'Alias of ' + m.real;                 // staff only: the server sends it to nobody else
    row.appendChild(name);
    row.appendChild(el('span', 'fc-colon', ':'));
    row.appendChild(el('span', 'fc-text', m.body));
    (into || log).appendChild(row);
    if (m.id > last) last = m.id;
    if (fresh && !m.mine) { bumpUnread(); if (snx.Sound && !document.hidden && !collapsed()) snx.Sound.play('tick'); }
  }

  function apply(d) {
    root.classList.toggle('is-closed', !!d.locked);
    root.classList.toggle('is-locked', !d.locked && !d.can_send);
    air.textContent = d.locked ? 'ENDED' : 'ON AIR';
    conn.dataset.state = 'on'; conn.textContent = 'live';
    const tok = root.querySelector('[data-fc-tokens]'); if (tok) tok.textContent = d.tokens;
    root.dataset.tokens = d.tokens;
    if (d.locked && d.result) {
      resultBox.className = 'fc-result ' + d.result.side;
      winner.textContent = d.result.side === 'meron' || d.result.side === 'wala' ? d.result.label + ' wins' : d.result.label;
      if (!wasClosed) {                       // the declarator just announced it while we were watching
        if (snx.toast) snx.toast('Result announced: ' + winner.textContent + '. Chat is now closed.', { level: 'success' });
        if (snx.Sound) snx.Sound.play('win');
      }
    }
    wasClosed = !!d.locked;
    input.disabled = !d.can_send;
    slowSecs = d.slow || 0;
    paintSlow(d);
    if (d.slow_left > 0) startCooldown(d.slow_left);
    refreshSend();
  }

  // ------------------------------------------------------------------ slow mode: chip + send cooldown
  const slowChip = $('[data-fc-slow]'), sendBtn = form.querySelector('button');
  let slowSecs = 0, cooldownUntil = 0, cooldownTimer = 0;
  function paintSlow(d) {
    if (!slowChip) return;
    slowChip.classList.remove('is-off', 'is-lux');
    if (d.slow > 0) { slowChip.hidden = false; slowChip.textContent = 'Slow mode · ' + d.slow + 's'; }
    else if (d.slow_global > 0) {                                  // slow mode exists, but not for this person
      slowChip.hidden = false; slowChip.classList.add('is-off');
      slowChip.textContent = d.has_lux ? 'Slow mode off · Lux VIP' : 'Slow mode off · staff';
      if (d.has_lux) slowChip.classList.add('is-lux');
    } else slowChip.hidden = true;
  }
  function cooldownLeft() { return Math.max(0, Math.ceil((cooldownUntil - Date.now()) / 1000)); }
  function refreshSend() {
    const left = cooldownLeft();
    sendBtn.disabled = busy || left > 0 || input.disabled;
    sendBtn.textContent = left > 0 ? 'Wait ' + left + 's' : 'Send';
    if (left <= 0 && cooldownTimer) { clearInterval(cooldownTimer); cooldownTimer = 0; }
  }
  function startCooldown(secs) {
    cooldownUntil = Math.max(cooldownUntil, Date.now() + secs * 1000);
    if (!cooldownTimer) cooldownTimer = setInterval(refreshSend, 250);
    refreshSend();
  }

  async function poll() {
    clearTimeout(timer);
    if (!document.hidden && !busy) {
      try {
        const first = last === 0;
        const ac = new AbortController(), kill = setTimeout(() => ac.abort(), 8000);
        const r = await fetch(url + '?after=' + last, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store', signal: ac.signal });
        clearTimeout(kill);
        if (r.status === 401 || r.status === 403) { conn.dataset.state = 'off'; conn.textContent = 'signed out'; return; }
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        failures = 0;
        const stick = pinned || first;
        const frag = document.createDocumentFragment();
        d.messages.forEach((m) => add(m, !first, frag));
        if (frag.childNodes.length) log.appendChild(frag);
        while (log.childElementCount > MAX_ROWS) log.firstElementChild.remove();
        apply(d);
        if (d.locked && d.messages.length === 0 && !first) { settled = true; }
        if (d.messages.length) { if (stick) toBottom(!first); else { jump.hidden = false; } }
      } catch (e) { failures++; conn.dataset.state = 'off'; conn.textContent = 'reconnecting'; }
    }
    if (settled) { conn.textContent = 'ended'; return; }                       // match over and history loaded: stop polling
    timer = setTimeout(poll, (collapsed() ? 6000 : 3000) * Math.min(1 + failures, 5));
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  async function post(path, body) {
    const r = await fetch(path, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(body || {}) });
    let d = {}; try { d = await r.json(); } catch (e) { /* non-JSON error page */ }
    if (!r.ok) throw new Error(d.detail || 'Could not do that. Try again.');
    return d;
  }
  const say = (msg, level) => (snx.toast ? snx.toast(msg, { level: level || 'warning' }) : alert(msg));

  input.addEventListener('input', () => {
    const n = input.value.length;
    count.textContent = n + ' / 300';
    count.style.color = n > 280 ? '#ff9aa8' : '';
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = input.value.trim();
    if (!body || busy || cooldownLeft() > 0) return;
    busy = true; refreshSend();
    try {
      await post(url + '/send', { body }); input.value = ''; count.textContent = '0 / 300'; pinned = true;
      if (slowSecs > 0) startCooldown(slowSecs);
      busy = false; await poll();
    }
    catch (e) { say(e.message); }
    finally { busy = false; refreshSend(); input.focus(); }
  });

  const unlock = $('[data-fc-unlock]');
  if (unlock) unlock.addEventListener('click', async () => {
    unlock.disabled = true;
    try { const d = await post(url + '/unlock'); apply(d); if (snx.Sound) snx.Sound.play('ok'); say('Chat unlocked for this game.', 'success'); input.focus(); }
    catch (e) { say(e.message); unlock.disabled = false; }
  });

  // ------------------------------------------------------------------ overlay: collapse / opacity / drag
  const overlay = root.querySelector('[data-fc-overlay]'), tgl = root.querySelector('[data-fc-toggle]');
  const stage = root.querySelector('[data-stage]') || overlay.parentElement;
  const unreadEl = root.querySelector('[data-fc-unread]');
  // Stage metrics: the panel is sized against the video SCREEN (not the whole player, which also holds the
  // control bar), so on a small window it never covers the controls or grows taller than the picture.
  const screenEl = stage.querySelector('[data-vp-screen]') || stage;
  function measure() {
    const s = stage.getBoundingClientRect(), v = screenEl.getBoundingClientRect();
    if (!v.width || !v.height) return;
    stage.style.setProperty('--fc-sw', Math.round(v.width) + 'px');
    stage.style.setProperty('--fc-sh', Math.round(v.height) + 'px');
    stage.style.setProperty('--fc-st', Math.round(v.top - s.top) + 'px');
  }
  measure();
  new MutationObserver(() => { conn.title = conn.textContent; }).observe(conn, { childList: true });   // the dot form needs a tooltip
  const store = { get: (k) => { try { return localStorage.getItem('snx.fc.' + k); } catch (e) { return null; } },
                  set: (k, v) => { try { localStorage.setItem('snx.fc.' + k, v); } catch (e) { /* blocked */ } } };
  const collapsed = () => !!overlay && overlay.classList.contains('is-collapsed');
  function bumpUnread() { if (!collapsed()) return; unread++; unreadEl.textContent = unread > 99 ? '99+' : String(unread); unreadEl.hidden = false; }
  function clearUnread() { unread = 0; unreadEl.hidden = true; }

  function setCollapsed(c, persist) {
    overlay.classList.toggle('is-collapsed', c);
    tgl.setAttribute('aria-expanded', String(!c));
    tgl.setAttribute('aria-label', c ? 'Expand chat' : 'Collapse chat');
    if (!c) { clearUnread(); toBottom(false); }
    clamp();
    if (persist) store.set('collapsed', c ? '1' : '0');
  }

  // transparency (remembered)
  const alpha = root.querySelector('[data-fc-alpha]'), alphaOut = root.querySelector('[data-fc-alpha-out]');
  function setAlpha(pct, persist) {
    pct = Math.min(100, Math.max(20, Math.round(+pct || 82)));
    overlay.style.setProperty('--fc-a', (pct / 100).toFixed(2));
    alpha.value = String(pct); alphaOut.textContent = pct + '%';
    if (persist) store.set('alpha', String(pct));
  }
  setAlpha(store.get('alpha') || 82);
  alpha.addEventListener('input', () => setAlpha(alpha.value, true));

  // size presets (S / M / L): width, text size and height are CSS; this only remembers the choice
  const sizeBtns = root.querySelectorAll('[data-fc-size]');
  function setSize(sz, persist) {
    if (!['s', 'm', 'l'].includes(sz)) sz = 'm';
    overlay.dataset.size = sz;
    sizeBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.fcSize === sz)));
    if (persist) store.set('size', sz);
    clamp(); if (pinned) toBottom(false);
  }
  sizeBtns.forEach((b) => b.addEventListener('click', () => setSize(b.dataset.fcSize, true)));
  setSize(store.get('size') || (window.matchMedia('(min-width:641px)').matches ? 'm' : 'l'), false);

  const setBox = root.querySelector('[data-fc-set]'), cog = root.querySelector('[data-fc-cog]');
  cog.addEventListener('click', () => { const open = setBox.hidden; setBox.hidden = !open; cog.setAttribute('aria-expanded', String(open)); });

  // drag: stored as a fraction of the stage so it survives resize + full screen
  let pos = null;                                          // {fx, fy} of free space (0..1) or null = default corner
  try { pos = JSON.parse(store.get('pos') || 'null'); } catch (e) { pos = null; }
  const free = () => {                                     // room the panel can travel inside the video screen
    const v = screenEl.getBoundingClientRect();
    const pad = parseFloat(getComputedStyle(overlay).left) || 12;
    return { w: Math.max(0, v.width - 2 * pad - overlay.offsetWidth), h: Math.max(0, v.height - 2 * pad - overlay.offsetHeight) };
  };
  const draggable = () => getComputedStyle(overlay).position === 'absolute';
  function place() {
    if (!draggable() || !pos) { overlay.style.setProperty('--fc-x', '0px'); overlay.style.setProperty('--fc-y', '0px'); return; }
    const f = free();
    overlay.style.setProperty('--fc-x', Math.round(pos.fx * f.w) + 'px');
    overlay.style.setProperty('--fc-y', Math.round(pos.fy * f.h) + 'px');
  }
  function clamp() { requestAnimationFrame(place); }

  const grip = root.querySelector('[data-fc-grip]'), head = root.querySelector('.fc-overlay .fc-head');
  let drag = null;
  head.addEventListener('pointerdown', (ev) => {
    if (ev.button > 0 || !draggable() || ev.target.closest('button,input,a')) return;
    const f = free(), curX = pos ? pos.fx * f.w : 0, curY = pos ? pos.fy * f.h : 0;
    drag = { id: ev.pointerId, sx: ev.clientX, sy: ev.clientY, curX, curY, f };
    overlay.classList.add('is-dragging');
    try { head.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
  });
  head.addEventListener('pointermove', (ev) => {
    if (!drag || ev.pointerId !== drag.id) return;
    const x = Math.min(drag.f.w, Math.max(0, drag.curX + ev.clientX - drag.sx));
    const y = Math.min(drag.f.h, Math.max(0, drag.curY + ev.clientY - drag.sy));
    pos = { fx: drag.f.w ? x / drag.f.w : 0, fy: drag.f.h ? y / drag.f.h : 0 };
    overlay.style.setProperty('--fc-x', Math.round(x) + 'px');
    overlay.style.setProperty('--fc-y', Math.round(y) + 'px');
  });
  const endDrag = (ev) => {
    if (!drag || (ev && ev.pointerId !== drag.id)) return;
    drag = null; overlay.classList.remove('is-dragging');
    if (pos) store.set('pos', JSON.stringify(pos));
  };
  head.addEventListener('pointerup', endDrag);
  head.addEventListener('pointercancel', endDrag);
  const reset = () => { pos = null; store.set('pos', 'null'); place(); };
  grip.addEventListener('dblclick', reset);
  root.querySelector('[data-fc-reset]').addEventListener('click', reset);
  // keyboard: arrow keys nudge when the title is focused
  grip.tabIndex = 0;
  grip.addEventListener('keydown', (ev) => {
    const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[ev.key];
    if (!d || !draggable()) return;
    ev.preventDefault();
    const f = free(), x = (pos ? pos.fx * f.w : 0) + d[0] * 24, y = (pos ? pos.fy * f.h : 0) + d[1] * 24;
    pos = { fx: f.w ? Math.min(1, Math.max(0, x / f.w)) : 0, fy: f.h ? Math.min(1, Math.max(0, y / f.h)) : 0 };
    place(); store.set('pos', JSON.stringify(pos));
  });

  // re-fit whenever the stage changes size (window resize, full screen on/off, rotate)
  if ('ResizeObserver' in window) {
    const ro = new ResizeObserver(() => { measure(); if (!drag) place(); });
    ro.observe(stage); ro.observe(screenEl);
    // when the panel itself changes size (window resize, size preset, tier change) keep the newest line in view
    new ResizeObserver(() => { if (pinned) log.scrollTop = log.scrollHeight; }).observe(log);
  }
  stage.addEventListener('snx:fs', () => { measure(); clamp(); toBottom(false); });

  tgl.addEventListener('click', () => setCollapsed(!collapsed(), true));
  const saved = store.get('collapsed');
  const tinyStage = screenEl.getBoundingClientRect().height < 170;      // too small to float a chat over
  setCollapsed(saved === null ? (window.matchMedia('(max-width:640px)').matches || tinyStage) : saved === '1', false);
  place();

  poll();
})();
