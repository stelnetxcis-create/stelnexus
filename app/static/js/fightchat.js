/* Livestream chat inside the fight view.
   Server-sent events, with short-poll as the fallback (3 s, stretching to 15-30 s when quiet, off in background tabs).
   The server decides who may type (and how fast: slow mode);
   this file only shows it. Messages are dense Twitch-style lines: [badges] name: text.
   Everything user-written goes in through textContent, never innerHTML. */
(function () {
  const root = document.querySelector('[data-fightchat]');
  if (!root) return;
  const $ = (s) => root.querySelector(s);
  const log = $('[data-fc-log]'), form = $('[data-fc-form]'), input = form.elements.body;
  const conn = $('[data-fc-conn]'), air = $('[data-fc-air]'), jump = $('[data-fc-jump]');
  const count = $('[data-fc-count]'), winner = $('[data-fc-winner]'), resultBox = $('[data-fc-result]');
  let url = root.dataset.url;
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const snx = (window.snx = window.snx || {});
  let settled = false, last = 0, pinned = true, wasClosed = root.classList.contains('is-closed'), busy = false, failures = 0;
  let loaded = false, etag = '', es = null, sseFails = 0, pollT = 0, hiddenT = 0, retryT = 0;
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
    air.textContent = d.locked ? 'ENDED' : 'PRE-SHOW';
    conn.dataset.state = 'on'; conn.textContent = 'live';
    const tok = root.querySelector('[data-fc-tokens]'); if (tok) tok.textContent = d.tokens;
    root.dataset.tokens = d.tokens;
    if (d.locked && d.result) {
      resultBox.className = 'fc-result ' + d.result.side;
      winner.textContent = d.result.side === 'meron' || d.result.side === 'wala' ? d.result.label + ' wins' : d.result.label;
      if (wasClosed === false) {              // the declarator just announced it while we were watching
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

  // ------------------------------------------------------------------ delivery
  // Stream first (/fights/ID/chat/stream: the server pushes new lines, the result being announced, slow-mode changes).
  // If it cannot be had we poll the same URL without "/stream", politely: If-None-Match (304 while nothing changed),
  // 3 s -> 15 s after a quiet minute -> 30 s after five (a message, key press or tap snaps it back), nothing in a hidden tab.
  const pace = (snx.pacer || (window.StelNexus && window.StelNexus.pacer))
    ? (snx.pacer || window.StelNexus.pacer)({ base: 3000, onWake: () => schedulePoll(0) })
    : { bump() {}, delay: () => 3000 };
  const connected = () => !!es && es.readyState === 1;

  function onData(d) {
    const first = !loaded;
    loaded = true;
    const stick = pinned || first;
    const frag = document.createDocumentFragment();
    d.messages.forEach((m) => add(m, !first, frag));
    if (frag.childNodes.length) log.appendChild(frag);
    while (log.childElementCount > MAX_ROWS) log.firstElementChild.remove();
    apply(d);
    if (d.messages.length) { pace.bump(); if (stick) toBottom(!first); else { jump.hidden = false; } }
    // Result announced and every line delivered (a full page means more are waiting): this chat is over.
    if (d.locked && d.messages.length < 100) settled = true;
    if (settled) finish();
  }

  function finish() {
    window.clearTimeout(pollT); window.clearTimeout(hiddenT); window.clearTimeout(retryT);
    closeStream();
    conn.textContent = 'ended';
  }

  async function poll() {
    if (settled || document.hidden || busy) return;
    try {
      const ac = new AbortController(), kill = setTimeout(() => ac.abort(), 8000);
      const headers = { Accept: 'application/json' };
      if (etag) headers['If-None-Match'] = etag;       // by hand: the request is no-store, so the browser would not
      const r = await fetch(url + '?after=' + last, { headers, credentials: 'same-origin', cache: 'no-store', signal: ac.signal });
      clearTimeout(kill);
      if (r.status === 401 || r.status === 403) { settled = true; conn.dataset.state = 'off'; conn.textContent = 'signed out'; return; }
      if (r.status === 304) { failures = 0; conn.dataset.state = 'on'; conn.textContent = 'live'; return; }
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      failures = 0;
      onData(d);
      etag = r.headers.get('ETag') || '';              // only after it was applied: a half-applied answer must come again
    } catch (e) { failures++; conn.dataset.state = 'off'; conn.textContent = 'reconnecting'; }
  }

  function schedulePoll(ms) {
    window.clearTimeout(pollT);
    if (settled || document.hidden || connected()) return;
    pollT = window.setTimeout(async () => {
      await poll();
      const base = pace.delay() * (collapsed() ? 2 : 1);               // a collapsed overlay is looked at less
      schedulePoll(Math.min(base, 60000) * Math.min(1 + failures, 5));
    }, ms);
  }

  function closeStream() { if (es) { es.close(); es = null; } }

  function openStream() {
    if (settled || es || typeof window.EventSource === 'undefined') return false;
    let source;
    try { source = new window.EventSource(url + '/stream?after=' + last); } catch (e) { return false; }
    es = source;
    source.addEventListener('open', () => { sseFails = 0; failures = 0; window.clearTimeout(pollT); conn.dataset.state = 'on'; conn.textContent = 'live'; });
    source.addEventListener('state', (ev) => { try { onData(JSON.parse(ev.data)); } catch (e) { /* skip a bad frame; the next one repairs the view */ } });
    source.addEventListener('auth', (ev) => {            // signed out, or this game is not visible to the account any more
      closeStream(); settled = true;
      conn.dataset.state = 'off'; conn.textContent = 'signed out';
    });
    source.addEventListener('bye', () => {               // the server recycles long streams: reconnect from where we are
      closeStream();
      window.clearTimeout(retryT);
      retryT = window.setTimeout(() => { if (!settled && !document.hidden && !openStream()) schedulePoll(0); }, 300);
    });
    source.addEventListener('retry', () => { source.onerror(); });
    source.onerror = () => {
      sseFails++;
      conn.dataset.state = 'off'; conn.textContent = 'reconnecting';
      if (source.readyState === 2 || sseFails >= 3) {    // the browser gave up (HTTP 401 / 429 ...) or keeps failing: poll, retry later
        closeStream();
        schedulePoll(0);
        window.clearTimeout(retryT);
        retryT = window.setTimeout(() => { if (!settled && !document.hidden) openStream(); }, 60000);
      }
    };
    return true;
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      window.clearTimeout(pollT);                        // a hidden tab polls nothing
      window.clearTimeout(hiddenT);
      hiddenT = window.setTimeout(closeStream, 60000);   // a long absence also frees the server's stream slot
      return;
    }
    window.clearTimeout(hiddenT);
    pace.bump();
    if (settled || connected()) return;
    if (!openStream()) poll().then(() => schedulePoll(pace.delay()));
  });
  window.addEventListener('pagehide', () => { settled = true; finish(); });
  ['pointerdown', 'keydown', 'focusin'].forEach((evt) => root.addEventListener(evt, () => pace.bump(), { passive: true }));

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
      const sent = await post(url + '/send', { body }); input.value = ''; count.textContent = '0 / 300'; pinned = true;
      if (slowSecs > 0) startCooldown(slowSecs);
      busy = false;
      pace.bump();
      if (connected()) {                                  // the stream delivers it in a moment; if it did not, fetch it
        const id = sent && sent.id;
        window.setTimeout(() => { if (id && !seen.has(id)) poll(); }, 1500);
      } else await poll();
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

  // ------------------------------------------------------------------ overlay: collapse / opacity / size / drag
  // All of that lives in the shared floating-panel module (floatpanel.js); the chat only supplies its elements,
  // its storage prefix (kept as "snx.fc." so people's saved settings carry over) and what to do when they change.
  const overlay = root.querySelector('[data-fc-overlay]'), tgl = root.querySelector('[data-fc-toggle]');
  const stage = root.querySelector('[data-stage]') || overlay.parentElement;
  const unreadEl = root.querySelector('[data-fc-unread]');
  // Stage metrics: the panel is sized against the video SCREEN (not the whole player, which also holds the
  // control bar), so on a small window it never covers the controls or grows taller than the picture.
  const screenEl = stage.querySelector('[data-vp-screen]') || stage;
  function measure() {
    const w = screenEl.offsetWidth, h = screenEl.offsetHeight;
    if (!w || !h) return;
    stage.style.setProperty('--fc-sw', w + 'px');
    stage.style.setProperty('--fc-sh', h + 'px');
  }
  measure();
  new MutationObserver(() => { conn.title = conn.textContent; }).observe(conn, { childList: true });   // the dot form needs a tooltip
  const collapsed = () => !!overlay && overlay.classList.contains('is-collapsed');
  function bumpUnread() { if (!collapsed()) return; unread++; unreadEl.textContent = unread > 99 ? '99+' : String(unread); unreadEl.hidden = false; }
  function clearUnread() { unread = 0; unreadEl.hidden = true; }

  const panel = snx.floatPanel(overlay, {
    store: 'snx.fc.', label: 'chat', stage, screen: screenEl,
    head: root.querySelector('.fc-overlay .fc-head'), grip: root.querySelector('[data-fc-grip]'), toggle: tgl,
    cog: root.querySelector('[data-fc-cog]'), settings: root.querySelector('[data-fc-set]'),
    sizeBtns: root.querySelectorAll('[data-fc-size]'), alpha: root.querySelector('[data-fc-alpha]'),
    alphaOut: root.querySelector('[data-fc-alpha-out]'), resetBtn: root.querySelector('[data-fc-reset]'),
    corner: { fx: 0, fy: 0 },
    defaultSize: () => (window.matchMedia('(min-width:641px)').matches ? 'm' : 'l'),
    defaultCollapsed: () => window.matchMedia('(max-width:640px)').matches || screenEl.offsetHeight < 170,   // too small to float a chat over
    onCollapse: (c) => { if (!c) { clearUnread(); toBottom(false); } },
    onSize: () => { if (pinned) toBottom(false); },
    onLayout: measure,
  });
  if ('ResizeObserver' in window) {
    // when the panel itself changes size (window resize, size preset, tier change) keep the newest line in view
    new ResizeObserver(() => { if (pinned) log.scrollTop = log.scrollHeight; }).observe(log);
  }
  stage.addEventListener('snx:fs', () => toBottom(false));

  // ------------------------------------------------------------------ switching game without reloading the page
  // The game strip calls this: the video and the panel stay, the chat just re-points at another game's room.
  const emptyHtml = log.innerHTML;
  const headSub = root.querySelector('.fc-title small');
  const unlockNote = root.querySelector('[data-fc-unlock] span:last-child');
  function switchFight(g) {
    if (!g || !g.id || String(g.id) === root.dataset.fight) return;
    window.clearTimeout(pollT); window.clearTimeout(hiddenT); window.clearTimeout(retryT);
    closeStream();
    settled = false; last = 0; etag = ''; loaded = false; failures = 0; sseFails = 0; wasClosed = null; busy = false;
    seen.clear(); clearUnread();
    log.innerHTML = emptyHtml; pinned = true; jump.hidden = true;
    root.dataset.fight = String(g.id);
    root.dataset.url = '/fights/' + g.id + '/chat';
    url = root.dataset.url;
    root.setAttribute('aria-label', 'Livestream chat for game ' + g.n);
    if (headSub) headSub.textContent = 'Game #' + g.n + ' \u00B7 ' + g.nm + ' vs ' + g.nw;
    if (unlockNote) unlockNote.textContent = 'Unlocks Game #' + g.n + ' only';
    resultBox.className = 'fc-result'; winner.textContent = '';
    conn.dataset.state = 'wait'; conn.textContent = 'connecting';
    if (!openStream()) { poll(); schedulePoll(pace.delay()); }
  }
  snx.fightChat = { switchFight, panel };

  if (!openStream()) { poll(); schedulePoll(pace.delay()); }
})();
