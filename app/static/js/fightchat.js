/* Livestream chat inside the fight view.
   Short-poll (3s, paused in background tabs). The server decides who may type; this file only shows it.
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
  let last = 0, pinned = true, timer = 0, wasClosed = root.classList.contains('is-closed'), busy = false, failures = 0;
  const seen = new Set();

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 70;
  const toBottom = (smooth) => { log.scrollTo({ top: log.scrollHeight, behavior: smooth ? 'smooth' : 'auto' }); pinned = true; jump.hidden = true; };
  log.addEventListener('scroll', () => { pinned = nearBottom(); if (pinned) jump.hidden = true; });
  jump.addEventListener('click', () => toBottom(true));

  function add(m, fresh) {
    if (seen.has(m.id)) return;
    seen.add(m.id);
    const empty = $('[data-fc-empty]'); if (empty) empty.remove();
    const row = el('article', 'fc-msg fc-tone-' + (m.tone || 'slate') + (m.mine ? ' mine' : '') + (m.lux ? ' is-lux' : ''));
    row.appendChild(el('div', 'fc-av', m.initial));
    const bub = el('div', 'fc-bub'), head = el('header');
    head.appendChild(el('b', null, m.user));
    if (m.lux) { const p = el('span', 'fc-pass fc-lux'); p.innerHTML = '<svg class="ico"><use href="#i-crown"/></svg>'; p.appendChild(document.createTextNode('Lux VIP')); head.appendChild(p); }
    else if (m.pass) { const p = el('span', 'fc-pass'); p.innerHTML = '<svg class="ico"><use href="#i-crown"/></svg>'; p.appendChild(document.createTextNode('Pass')); head.appendChild(p); }
    else if (m.role && m.role !== 'Management') head.appendChild(el('span', 'fc-role', m.role));
    head.appendChild(el('time', null, m.at));
    bub.appendChild(head); bub.appendChild(el('p', null, m.body)); row.appendChild(bub);
    log.appendChild(row);
    if (m.id > last) last = m.id;
    if (fresh && !m.mine && snx.Sound && !document.hidden) snx.Sound.play('tick');
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
  }

  async function poll() {
    clearTimeout(timer);
    if (!document.hidden && !busy) {
      try {
        const first = last === 0;
        const r = await fetch(url + '?after=' + last, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
        if (r.status === 401 || r.status === 403) { conn.dataset.state = 'off'; conn.textContent = 'signed out'; return; }
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        failures = 0;
        const stick = pinned || first;
        d.messages.forEach((m) => add(m, !first));
        apply(d);
        if (d.messages.length) { if (stick) toBottom(!first); else { jump.hidden = false; } }
      } catch (e) { failures++; conn.dataset.state = 'off'; conn.textContent = 'reconnecting'; }
    }
    timer = setTimeout(poll, 3000 * Math.min(1 + failures, 5));
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
    if (!body || busy) return;
    busy = true; form.querySelector('button').disabled = true;
    try { await post(url + '/send', { body }); input.value = ''; count.textContent = '0 / 300'; pinned = true; busy = false; await poll(); }
    catch (e) { say(e.message); }
    finally { busy = false; form.querySelector('button').disabled = false; input.focus(); }
  });

  const unlock = $('[data-fc-unlock]');
  if (unlock) unlock.addEventListener('click', async () => {
    unlock.disabled = true;
    try { const d = await post(url + '/unlock'); apply(d); if (snx.Sound) snx.Sound.play('ok'); say('Chat unlocked for this game.', 'success'); input.focus(); }
    catch (e) { say(e.message); unlock.disabled = false; }
  });

  poll();
})();
