/* Number game: ball board, bet slip, countdowns.
   The server stays the source of truth: this file only shows what /bola/draws/<id>/preview returns
   and what the form will post (fields n1..n5, tumbok, sahod are unchanged). */
(function () {
  const snx = window.snx || {};
  const play = (n) => { try { if (snx.Sound) snx.Sound.play(n); } catch (e) { /* sound is optional */ } };
  const buzz = (ms) => { try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* optional */ } };
  const peso = (c) => '\u20B1' + (c / 100).toLocaleString('en-PH', { minimumFractionDigits: 2 });
  const reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
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
  document.querySelectorAll('.bd-card').forEach((c, i) => c.style.setProperty('--i', String(Math.min(i, 12))));

  /* ---------------------------------------------------------------- drawn-result reveal (once per result) */
  (function reveal() {
    const box = document.querySelector('.bs-result');
    const orbs = box ? Array.from(box.querySelectorAll('.bball')) : [];
    if (!box || !orbs.length || reduce) return;
    const key = 'bola-seen:' + location.pathname + ':' + orbs.map((o) => o.textContent).join('/');
    try { if (sessionStorage.getItem(key)) return; sessionStorage.setItem(key, '1'); } catch (e) { /* show it anyway */ }
    box.setAttribute('aria-busy', 'true');
    const finals = orbs.map((o) => o.textContent);
    orbs.forEach((o, i) => {
      const stop = 900 + i * 450;
      const id = setInterval(() => { o.textContent = String(1 + Math.floor(Math.random() * 40)); }, 55);
      setTimeout(() => {
        clearInterval(id); o.textContent = finals[i]; o.classList.add('is-landed'); play('ok');
        if (i === orbs.length - 1) { box.removeAttribute('aria-busy'); box.classList.add('is-revealed'); confetti(box); }
      }, stop);
    });
  })();
  function confetti(host) {
    const r = host.getBoundingClientRect(), hues = ['#f7de93', '#a9d4ff', '#a8f0cf', '#ffa9b5', '#d9c6ff'];
    for (let i = 0; i < 28; i += 1) {
      const p = document.createElement('i');
      p.className = 'bs-confetti';
      p.style.cssText = 'left:' + (r.left + r.width * (0.2 + Math.random() * 0.6)) + 'px;top:' + (r.top + r.height / 2) + 'px;background:' + hues[i % 5] +
        ';--dx:' + Math.round((Math.random() - 0.5) * 320) + 'px;--dy:' + Math.round(-60 - Math.random() * 160) + 'px;--rot:' + Math.round(Math.random() * 720) + 'deg;animation-delay:' + Math.round(Math.random() * 120) + 'ms';
      document.body.appendChild(p);
      setTimeout(() => p.remove(), 1500);
    }
  }

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
  let timer = null, target = 'both', valid = false, closed = false, seq = 0, busy = false;
  const shown = { lines: 0, stake: 0, win: 0 };
  const why = form.querySelector('[data-bola-why]'), hint = form.querySelector('[data-bola-hint]'), picksBox = form.querySelector('.bs-picks');
  const bar = form.querySelector('.bs-bar');

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
    const left = need() - list.length;
    if (hint) hint.textContent = left > 0 ? (list.length ? left + ' more to go' : 'tap ' + need() + ' numbers') : 'ready \u2713';
    if (picksBox) picksBox.classList.toggle('is-complete', left === 0);
    form.style.setProperty('--fill', String(Math.min(1, list.length / need())));
  }
  function fly(fromEl, slot, n) {
    if (reduce || !fromEl || !slot || !slot.animate) return;
    const a = fromEl.getBoundingClientRect(), b = slot.getBoundingClientRect();
    const g = document.createElement('span');
    g.className = 'bs-ghost ' + tone(n); g.textContent = String(n);
    g.style.cssText = 'left:' + a.left + 'px;top:' + a.top + 'px;width:' + a.width + 'px;height:' + a.height + 'px';
    slot.classList.add('is-landing');
    document.body.appendChild(g);
    const dx = b.left - a.left, dy = b.top - a.top, k = b.width / a.width;
    const run = g.animate([
      { transform: 'translate(0,0) scale(1.1)', opacity: 1 },
      { transform: 'translate(' + dx * 0.5 + 'px,' + (dy * 0.5 - 46) + 'px) scale(1.2)', opacity: 1, offset: 0.5 },
      { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + k + ')', opacity: 1 }
    ], { duration: 460, easing: 'cubic-bezier(.3,.7,.3,1)' });
    const done = () => { g.remove(); slot.classList.remove('is-landing'); slot.classList.add('is-land'); setTimeout(() => slot.classList.remove('is-land'), 500); };
    run.onfinish = done; run.oncancel = done;
  }
  function setPicked(list) {
    boxes().forEach((el, i) => { el.value = list[i] !== undefined ? String(list[i]) : ''; });
    paint(); refresh();
  }
  function gate() {
    const stake = (parseFloat(tumbok.value) || 0) + (parseFloat(sahod.value) || 0);
    const left = need() - picked().length;
    let reason = '';
    if (closed) reason = 'Betting is closed for this draw';
    else if (left > 0) reason = 'Pick ' + left + ' more number' + (left > 1 ? 's' : '');
    else if (!(stake > 0)) reason = 'Add an amount (tap a chip)';
    valid = !reason;
    submits.forEach((b) => { b.disabled = !valid; });
    if (why) why.textContent = reason;
    if (bar) bar.classList.toggle('is-ready', valid);
    const done = { 2: left === 0, 3: stake > 0 };
    form.querySelectorAll('[data-step]').forEach((el) => { const st = el.dataset.step; if (done[st] !== undefined) el.classList.toggle('is-done', done[st]); });
    const cur = target !== 'sahod' ? tumbok.value : sahod.value;
    form.querySelectorAll('[data-chip]').forEach((c) => c.setAttribute('aria-pressed', String(cur !== '' && parseFloat(cur) === parseFloat(c.dataset.chip))));
  }

  function buildGrid() {
    if (!host) return;
    const max = parseInt(host.dataset.max, 10) || 40;
    for (let n = 1; n <= max; n += 1) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'bola-ball ' + tone(n);
      b.dataset.n = String(n);
      b.style.setProperty('--i', String(n));
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
      if (!b || busy) return;
      const n = +b.dataset.n, cur = picked(), at = cur.indexOf(n), limit = need();
      if (at >= 0) { cur.splice(at, 1); play('tick'); }
      else if (cur.length < limit) { cur.push(n); play('tick'); buzz(8); }
      else { note.textContent = 'This ticket type takes ' + limit + ' numbers. Tap one to remove it first.'; play('err'); buzz(30);
        if (picksBox) { picksBox.classList.remove('is-shake'); void picksBox.offsetWidth; picksBox.classList.add('is-shake'); }
        return; }
      note.textContent = '';
      const added = at < 0;
      setPicked(cur);
      if (added) fly(b, tray && tray.children[cur.length - 1], n);
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
  function tween(el, key, to, fmt) {
    if (!el) return;
    const from = shown[key]; shown[key] = to;
    if (reduce || from === to) { el.textContent = fmt(to); return; }
    const t0 = performance.now(), dur = 420;
    el.classList.remove('is-bump'); void el.offsetWidth; el.classList.add('is-bump');
    (function step(t) {
      const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      if (shown[key] !== to) return;                    // a newer value took over
      el.textContent = fmt(Math.round(from + (to - from) * e));
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }
  function setSummary(lines, stake, win) {
    tween(sum.lines, 'lines', lines, String);
    tween(sum.stake, 'stake', stake, peso);
    tween(sum.win, 'win', win, peso);
  }
  function renderPreview(d) {
    out.className = 'bs-preview small';
    out.textContent = '';
    const grid = document.createElement('div'); grid.className = 'bs-lines';
    d.lines.forEach((l) => {
      const card = document.createElement('div'); card.className = 'bs-line'; card.style.setProperty('--i', String(Math.min(grid.children.length, 10)));
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
      if (busy) return;
      const max = parseInt(host.dataset.max, 10) || 40, pool = [];
      for (let n = 1; n <= max; n += 1) pool.push(n);
      for (let i = pool.length - 1; i > 0; i -= 1) { const j = Math.floor(Math.random() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
      const final = pool.slice(0, need());
      if (reduce) { setPicked(final); play('ok'); buzz(15); return; }
      busy = true; setPicked([]);
      let spins = 0;
      const id = setInterval(() => {                    // a light sweeps the board like a lottery machine
        balls.forEach((b) => b.classList.remove('is-scan'));
        balls[Math.floor(Math.random() * balls.length)].classList.add('is-scan'); play('tick');
        if ((spins += 1) < 20) return;
        clearInterval(id); balls.forEach((b) => b.classList.remove('is-scan'));
        final.forEach((n, k) => setTimeout(() => {
          const cur = picked(); cur.push(n); setPicked(cur); fly(balls[n - 1], tray.children[cur.length - 1], n); play('tick'); buzz(10);
          if (k === final.length - 1) setTimeout(() => { busy = false; play('ok'); }, 480);
        }, k * 240));
      }, 50);
      return;
    }
    const seg = ev.target.closest('[data-target]');
    if (seg) {
      target = seg.dataset.target;
      form.querySelectorAll('[data-target]').forEach((b) => b.setAttribute('aria-pressed', b === seg ? 'true' : 'false'));
      play('tick'); gate();
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
  form.addEventListener('submit', (ev) => {
    if (form.classList.contains('is-sending')) { ev.preventDefault(); return; }   // no double taps
    form.classList.add('is-sending'); play('ok'); try { sessionStorage.setItem('bola-fresh', String(Date.now())); } catch (e) { /* optional */ }
    const b = ev.submitter;
    if (b) { b.dataset.label = b.textContent; b.textContent = 'Placing\u2026'; }
  });
  window.addEventListener('pageshow', () => {          // back button: un-stick the slip
    form.classList.remove('is-sending');
    submits.forEach((b) => { if (b.dataset.label) b.textContent = b.dataset.label; });
  });
  document.addEventListener('bola:closed', () => { closed = true; gate(); });

  buildGrid();
  syncKind();
})();
