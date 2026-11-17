/* Number game: ball board, bet slip, countdowns.
   The server stays the source of truth: this file only shows what /bola/draws/<id>/preview returns
   and what the form will post (fields n1..n5, tumbok, sahod are unchanged). */
(function () {
  const snx = window.snx || {};
  const play = (n) => { try { if (snx.Sound) snx.Sound.play(n); } catch (e) { /* sound is optional */ } };
  const buzz = (ms) => { try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* optional */ } };
  const peso = (c) => '\u20B1' + (c / 100).toLocaleString('en-PH', { minimumFractionDigits: 2 });
  const tone = (n) => 'c' + ((Math.floor((n - 1) / 10) % 5) + 1);

  /* ---------------------------------------------------------------- countdowns (slip + lobby) */
  function startClocks() {
    const clocks = Array.from(document.querySelectorAll('[data-cutoff]'));
    if (!clocks.length) return;
    const fmt = (s) => {
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      const two = (x) => String(x).padStart(2, '0');
      return h ? h + ':' + two(m) + ':' + two(sec) : two(m) + ':' + two(sec);
    };
    function tick() {
      const now = Date.now();
      clocks.forEach((el) => {
        const at = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(el.dataset.cutoff) ? el.dataset.cutoff : el.dataset.cutoff + 'Z').getTime();
        const left = Math.max(0, Math.round((at - now) / 1000));
        const label = el.querySelector('[data-clock-text]') || el;
        if (left <= 0) {
          label.textContent = 'Betting closed';
          el.classList.remove('is-soon', 'is-urgent'); el.classList.add('is-closed');
          if (!el.dataset.done) { el.dataset.done = '1'; document.dispatchEvent(new CustomEvent('bola:closed')); }
        } else {
          label.textContent = 'Closes in ' + fmt(left);
          el.classList.toggle('is-urgent', left <= 60);
          el.classList.toggle('is-soon', left > 60 && left <= 300);
        }
      });
    }
    tick(); setInterval(tick, 1000);
  }
  startClocks();

  const form = document.querySelector('[data-bola-slip]');
  if (!form) return;

  /* ---------------------------------------------------------------- slip */
  const COUNT = { pair: 2, casas3: 3, casas6: 4, casas10: 5 };
  const kind = form.querySelector('[name=kind]');
  const out = form.querySelector('[data-bola-preview]');
  const host = form.querySelector('[data-bola-grid]');
  const tray = form.querySelector('[data-bola-tray]');
  const tumbok = form.querySelector('[name=tumbok]'), sahod = form.querySelector('[name=sahod]');
  const sum = { lines: form.querySelector('[data-sum-lines]'), stake: form.querySelector('[data-sum-stake]'), win: form.querySelector('[data-sum-win]') };
  const submits = Array.from(form.querySelectorAll('button[type=submit]'));
  const balls = [];
  let timer = null, target = 'both', valid = false, closed = false, seq = 0;

  const boxes = () => Array.from(form.querySelectorAll('[data-num]'));
  const need = () => COUNT[kind.value] || 2;
  const picked = () => boxes().map((b) => parseInt(b.value, 10)).filter((n) => Number.isInteger(n) && n > 0);

  function paint() {
    const list = picked(), pos = new Map(list.map((n, i) => [n, i + 1]));
    balls.forEach((b) => {
      const o = pos.get(+b.dataset.n);
      b.setAttribute('aria-pressed', o ? 'true' : 'false');
      if (o) b.dataset.order = String(o); else b.removeAttribute('data-order');
    });
    if (tray) {
      tray.textContent = '';
      for (let i = 0; i < need(); i += 1) {
        const s = document.createElement('span');
        const n = list[i];
        s.className = 'bs-slot' + (n ? ' is-on ' + tone(n) : '');
        s.textContent = n ? String(n) : String(i + 1);
        tray.appendChild(s);
      }
    }
  }
  function setPicked(list) {
    boxes().forEach((el, i) => { el.value = list[i] !== undefined ? String(list[i]) : ''; });
    paint(); refresh();
  }
  function gate() {
    const stake = (parseFloat(tumbok.value) || 0) + (parseFloat(sahod.value) || 0);
    valid = picked().length === need() && stake > 0 && !closed;
    submits.forEach((b) => { b.disabled = !valid; });
  }

  function buildGrid() {
    if (!host) return;
    const max = parseInt(host.dataset.max, 10) || 40;
    for (let n = 1; n <= max; n += 1) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'bola-ball ' + tone(n);
      b.dataset.n = String(n);
      const t = document.createElement('span'); t.textContent = String(n); b.appendChild(t);
      b.setAttribute('aria-pressed', 'false');
      b.setAttribute('aria-label', 'Number ' + n);
      balls.push(b);
      host.appendChild(b);
    }
    const note = document.createElement('div');
    note.className = 'bola-grid-note';
    host.appendChild(note);
    host.addEventListener('click', (ev) => {
      const b = ev.target.closest('.bola-ball');
      if (!b) return;
      const n = +b.dataset.n, cur = picked(), at = cur.indexOf(n), limit = need();
      if (at >= 0) { cur.splice(at, 1); play('tick'); }
      else if (cur.length < limit) { cur.push(n); play('tick'); buzz(8); }
      else { note.textContent = 'This ticket type takes ' + limit + ' numbers. Tap one to remove it first.'; play('err'); buzz(30); return; }
      note.textContent = '';
      setPicked(cur);
    });
  }

  function syncKind() {
    const n = need();
    boxes().forEach((el, i) => { el.closest('label').hidden = i >= n; if (i >= n) el.value = ''; });
    form.querySelectorAll('[data-kind]').forEach((b) => b.setAttribute('aria-pressed', b.dataset.kind === kind.value ? 'true' : 'false'));
    paint(); refresh();
  }

  function refresh() {
    gate();
    clearTimeout(timer);
    const mine = ++seq;
    timer = setTimeout(async () => {
      const nums = boxes().map((b) => b.value.trim()).filter(Boolean).join(',');
      if (!nums) { out.textContent = ''; setSummary(0, 0, 0); return; }
      const t = tumbok.value || '0', s = sahod.value || '0';
      const url = form.dataset.preview + '?kind=' + encodeURIComponent(kind.value) + '&numbers=' + encodeURIComponent(nums) +
        '&tumbok=' + encodeURIComponent(t) + '&sahod=' + encodeURIComponent(s);
      try {
        const r = await fetch(url, { credentials: 'same-origin' });
        const d = await r.json();
        if (mine !== seq) return;                       // a newer tap already asked
        if (!d.ok) { out.className = 'bs-preview small muted'; out.textContent = d.error; setSummary(0, 0, 0); return; }
        renderPreview(d);
      } catch (e) { out.textContent = ''; }
    }, 220);
  }
  function setSummary(lines, stake, win) {
    if (sum.lines) sum.lines.textContent = String(lines);
    if (sum.stake) sum.stake.textContent = peso(stake);
    if (sum.win) sum.win.textContent = peso(win);
  }
  function renderPreview(d) {
    out.className = 'bs-preview small';
    out.textContent = '';
    const grid = document.createElement('div'); grid.className = 'bs-lines';
    d.lines.forEach((l) => {
      const card = document.createElement('div'); card.className = 'bs-line';
      const pair = document.createElement('div'); pair.className = 'pair';
      [l.first, l.second].forEach((n, i) => {
        if (i) { const dash = document.createElement('span'); dash.className = 'muted'; dash.textContent = '\u2013'; pair.appendChild(dash); }
        const m = document.createElement('span'); m.className = 'mini ' + tone(n); m.textContent = String(n); pair.appendChild(m);
      });
      const pay = document.createElement('div'); pay.className = 'pay';
      const a = document.createElement('div'); a.textContent = 'in order '; const ab = document.createElement('b'); ab.textContent = peso(l.tumbok_payout_cents); a.appendChild(ab);
      const c = document.createElement('div'); c.textContent = 'reversed '; const cb = document.createElement('b'); cb.textContent = peso(l.sahod_payout_cents); c.appendChild(cb);
      pay.appendChild(a); pay.appendChild(c);
      card.appendChild(pair); card.appendChild(pay); grid.appendChild(card);
    });
    out.appendChild(grid);
    setSummary(d.line_count, d.stake_cents, d.max_payout_cents);
  }

  /* quick-pick, clear, ticket-type tabs */
  form.addEventListener('click', (ev) => {
    const k = ev.target.closest('[data-kind]');
    if (k) { kind.value = k.dataset.kind; play('tick'); syncKind(); return; }
    if (ev.target.closest('[data-bola-clear]')) { setPicked([]); play('tick'); return; }
    if (ev.target.closest('[data-bola-lucky]')) {
      const max = parseInt(host.dataset.max, 10) || 40, pool = [];
      for (let n = 1; n <= max; n += 1) pool.push(n);
      for (let i = pool.length - 1; i > 0; i -= 1) { const j = Math.floor(Math.random() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
      setPicked(pool.slice(0, need())); play('ok'); buzz(15); return;
    }
    const seg = ev.target.closest('[data-target]');
    if (seg) {
      target = seg.dataset.target;
      form.querySelectorAll('[data-target]').forEach((b) => b.setAttribute('aria-pressed', b === seg ? 'true' : 'false'));
      return;
    }
    const chip = ev.target.closest('[data-chip]');
    if (chip) {
      const v = chip.dataset.chip;
      if (target !== 'sahod') tumbok.value = v;
      if (target !== 'tumbok') sahod.value = v;
      play('tick'); buzz(8); refresh();
    }
  });
  form.addEventListener('input', () => { paint(); refresh(); });
  form.addEventListener('submit', () => { form.classList.add('is-sending'); play('ok'); });
  document.addEventListener('bola:closed', () => { closed = true; gate(); });

  buildGrid();
  syncKind();
})();
