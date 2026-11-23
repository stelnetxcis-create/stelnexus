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

  /* ---------------------------------------------------------------- feel: sound, haptics, sparks
     Small synthesised sounds (they follow the site's sound switch) and short vibrations make each tap land. */
  const AC = window.AudioContext || window.webkitAudioContext;
  let actx = null;
  function blip(freq, at, dur, type, vol) {
    if (!AC || !(snx.Sound && snx.Sound.on)) return;
    try {
      actx = actx || new AC();
      if (actx.state === 'suspended') actx.resume();
      const o = actx.createOscillator(), g = actx.createGain(), t = actx.currentTime + at;
      o.type = type || 'sine'; o.frequency.setValueAtTime(freq, t);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(vol || 0.06, t + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(g); g.connect(actx.destination); o.start(t); o.stop(t + dur + 0.04);
    } catch (e) { /* sound is optional */ }
  }
  const LADDER = [523.25, 587.33, 659.25, 783.99, 880, 987.77, 1046.5];      // each number you pick sounds a step higher
  const fx = {
    pick(i) { const f = LADDER[Math.min(i, LADDER.length - 1)]; blip(f, 0, 0.16, 'sine', 0.07); blip(f * 2, 0, 0.08, 'triangle', 0.018); buzz(8); },
    unpick() { blip(392, 0, 0.09, 'sine', 0.05); blip(294, 0.05, 0.12, 'sine', 0.04); buzz(5); },
    kind() { blip(440, 0, 0.07, 'triangle', 0.05); blip(660, 0.05, 0.12, 'triangle', 0.05); buzz(10); },
    lock() { [523.25, 659.25, 783.99, 1046.5].forEach((f, i) => blip(f, i * 0.07, 0.22, 'triangle', 0.06)); buzz([10, 40, 14]); },
    chip(v) { blip(2093, 0, 0.05, 'square', 0.025); blip(1568 + Math.min(v, 200), 0.03, 0.2, 'triangle', 0.05); buzz(9); },
    step() { blip(659.25, 0, 0.1, 'sine', 0.05); blip(987.77, 0.07, 0.16, 'sine', 0.05); },
    ready() { [784, 988, 1175, 1568].forEach((f, i) => blip(f, i * 0.06, 0.3, 'sine', 0.06)); blip(2093, 0.26, 0.5, 'sine', 0.03); buzz([12, 30, 12, 30, 22]); },
    send() { for (let i = 0; i < 6; i += 1) blip(500 + i * 120, i * 0.035, 0.1, 'sawtooth', 0.02); },
  };
  function sparks(host, n, big) {
    if (reduce || !host) return;
    const r = host.getBoundingClientRect(), hues = ['#f7de93', '#fff3c4', '#a9d4ff', '#a8f0cf', '#ffa9b5'];
    for (let i = 0; i < n; i += 1) {
      const p = document.createElement('i'), ang = Math.random() * Math.PI * 2, d = (big ? 70 : 38) + Math.random() * (big ? 70 : 30);
      p.className = 'bs-spark';
      p.style.cssText = 'left:' + (r.left + r.width / 2) + 'px;top:' + (r.top + r.height / 2) + 'px;background:' + hues[i % 5] +
        ';--dx:' + Math.round(Math.cos(ang) * d) + 'px;--dy:' + Math.round(Math.sin(ang) * d) + 'px;animation-delay:' + Math.round(Math.random() * 80) + 'ms';
      document.body.appendChild(p); setTimeout(() => p.remove(), 900);
    }
  }

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
  let timer = null, valid = false, closed = false, seq = 0, busy = false;
  // Unified amount: Tumbok is filled first, Sahod follows it until you change Sahod on its own.
  let slot = 'tumbok', linked = true;
  let kindChosen = false, stage = 1, booted = false, advTimer = null, wasComplete = false, idleTimer = null;   // guided flow (see below)
  const pills = Array.from(form.querySelectorAll('[data-slot]')), other = form.querySelector('[data-other]');
  const amtHint = form.querySelector('[data-amt-hint]');
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
        s.style.setProperty('--k', String(i));
        s.textContent = n ? String(n) : String(i + 1);
        if (n) s.title = 'Tap to remove ' + n;
        tray.appendChild(s);
      }
    }
    const left = need() - list.length;
    if (hint) hint.textContent = left > 0 ? (list.length ? left + ' more to go' : 'tap ' + need() + ' numbers') : 'ready \u2713';
    if (picksBox) picksBox.classList.toggle('is-complete', left === 0);
    if (left === 0 && !wasComplete && booted) lockIn();                 // the last number just landed
    wasComplete = left === 0;
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
    const wasValid = valid;
    valid = !reason;
    if (booted && valid && !wasValid) readyBurst();
    submits.forEach((b) => { b.disabled = !valid; });
    if (bar) bar.classList.toggle('is-ready', valid);
    form.classList.toggle('is-closed', closed);
    const done = { 1: kindChosen, 2: left === 0, 3: stake > 0 };
    form.querySelectorAll('[data-step]').forEach((el) => { const st = el.dataset.step; if (done[st] !== undefined) el.classList.toggle('is-done', done[st]); });
    paintAmount();
    progress();
  }

  /* ---- unified amount ---- */
  const amt = (el) => parseFloat(el.value) || 0;
  const money = (v) => '\u20B1' + (Number.isInteger(v) ? String(v) : v.toFixed(2));
  function countTo(el, to) {
    const from = el._v || 0; el._v = to;
    const show = (v) => { el.textContent = v > 0 ? money(v) : '\u2014'; };
    if (reduce || from === to) { show(to); return; }
    const t0 = performance.now(), dur = 380;
    el.classList.remove('is-bump'); void el.offsetWidth; el.classList.add('is-bump');
    (function step(t) {
      if (el._v !== to) return;                          // a newer amount took over
      const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      show(p < 1 ? Math.round(from + (to - from) * e) : to);
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }
  function toss(from, to, i) {                           // a chip is thrown from the rack onto the amount
    if (reduce || !from || !to || !from.animate) return;
    const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
    const g = from.cloneNode(true);
    g.className = 'bs-chip bs-toss'; g.removeAttribute('aria-pressed');
    g.style.cssText = 'left:' + a.left + 'px;top:' + a.top + 'px;width:' + a.width + 'px;height:' + a.height + 'px';
    document.body.appendChild(g);
    const dx = b.left + 44 - (a.left + a.width / 2), dy = b.top + b.height / 2 - (a.top + a.height / 2);
    const run = g.animate([
      { transform: 'translate(0,0) scale(1) rotate(0deg)', opacity: 1 },
      { transform: 'translate(' + dx * 0.5 + 'px,' + (dy * 0.5 - 54) + 'px) scale(1.18) rotate(200deg)', opacity: 1, offset: 0.5 },
      { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(.42) rotate(380deg)', opacity: 0.92 }
    ], { duration: 440, delay: i * 90, easing: 'cubic-bezier(.3,.7,.3,1)', fill: 'both' });
    const done = () => { g.remove(); to.classList.remove('is-catch'); void to.offsetWidth; to.classList.add('is-catch'); setTimeout(() => to.classList.remove('is-catch'), 480); };
    run.onfinish = done; run.oncancel = done;
  }
  function put(which, v) { (which === 'tumbok' ? tumbok : sahod).value = v > 0 ? String(v) : ''; }
  function paintAmount() {
    const t = amt(tumbok), s = amt(sahod), both = t > 0 && s > 0 && t === s;
    pills.forEach((p) => {
      const on = p.dataset.slot === slot, v = p.dataset.slot === 'tumbok' ? t : s;
      p.setAttribute('aria-pressed', String(on)); p.classList.toggle('is-live', on); p.classList.toggle('is-off', !(v > 0));
      countTo(p.querySelector('[data-slot-val]'), v);
      const note = p.querySelector('[data-slot-note]');
      note.textContent = p.dataset.slot === 'sahod' ? (linked && t > 0 ? 'same as Tumbok' : (v > 0 ? '' : 'none')) : (t > 0 ? '' : 'none');
    });
    const cur = slot === 'tumbok' ? t : s;
    form.querySelectorAll('[data-chip]').forEach((c) => {
      const v = parseFloat(c.dataset.chip);
      c.setAttribute('aria-pressed', String(v > 0 && cur === v));
    });
    if (amtHint) {
      amtHint.textContent = slot === 'tumbok'
        ? (t > 0 ? 'Tumbok ' + money(t) + (linked ? ' and Sahod follow it' : '') + ' \u2014 tap an amount to change Tumbok'
          : 'Tap an amount to start with Tumbok')
        : (both ? 'Sahod matches Tumbok \u2014 tap another amount to change Sahod, or None to drop it'
          : 'Now tap the amount for Sahod, or None for a Tumbok-only bet');
    }
  }
  function chooseAmount(v, typed) {
    if (slot === 'tumbok') {
      put('tumbok', v);
      if (linked) put('sahod', v);
      if (!typed) slot = 'sahod';                       // next tap is for Sahod
    } else {
      put('sahod', v);
      linked = v > 0 && v === amt(tumbok);
      if (!typed) slot = 'tumbok';
    }
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
      if (at >= 0) { cur.splice(at, 1); fx.unpick(); }
      else if (cur.length < limit) { cur.push(n); fx.pick(cur.length - 1); ripple(b); }
      else { note.textContent = 'This ticket type takes ' + limit + ' numbers. Tap one to remove it first.'; play('err'); buzz(30);
        if (picksBox) { picksBox.classList.remove('is-shake'); void picksBox.offsetWidth; picksBox.classList.add('is-shake'); }
        return; }
      note.textContent = '';
      const added = at < 0;
      if (added) kindChosen = true;
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
    const rl = ev.target.closest('[data-rail-step]');
    if (rl) { play('tick'); focusStep(+rl.dataset.railStep, true); return; }
    if (ev.target.closest('[data-bola-why]')) { play('tick'); focusStep(stage, true); return; }
    if (ev.target.closest('[data-bola-repeat]')) { repeatLast(); return; }
    const k = ev.target.closest('[data-kind]');
    if (k) {
      kind.value = k.dataset.kind; kindChosen = true; fx.kind(); syncKind();
      if (stage === 2) advance(2);                     // after picking a type, take them to the numbers
      return;
    }
    if (ev.target.closest('[data-bola-clear]')) { setPicked([]); play('tick'); return; }
    if (ev.target.closest('[data-bola-lucky]')) {
      if (busy) return;
      kindChosen = true;
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
          const cur = picked(); cur.push(n); setPicked(cur); fly(balls[n - 1], tray.children[cur.length - 1], n); fx.pick(cur.length - 1); ripple(balls[n - 1]);
          if (k === final.length - 1) setTimeout(() => { busy = false; play('ok'); }, 480);
        }, k * 240));
      }, 50);
      return;
    }
    const pill = ev.target.closest('[data-slot]');
    if (pill) {
      slot = pill.dataset.slot;
      if (slot === 'tumbok' && !(amt(sahod) > 0)) linked = true;     // an empty Sahod follows again
      if (other) other.value = '';
      play('tick'); gate();
      return;
    }
    const chip = ev.target.closest('[data-chip]');
    if (chip) {
      const v = parseFloat(chip.dataset.chip) || 0;
      const aims = slot === 'tumbok' ? (linked ? ['tumbok', 'sahod'] : ['tumbok']) : ['sahod'];   // which pills this tap fills
      chooseAmount(v, false);
      if (other) other.value = '';
      if (v > 0) { aims.forEach((w, i) => toss(chip, pills.find((p) => p.dataset.slot === w), i)); fx.chip(v); } else fx.unpick();
      refresh();
    }
  });
  form.addEventListener('input', (ev) => {
    if (ev.target === other) chooseAmount(parseFloat(other.value) || 0, true);   // typing edits the lit side only
    if (ev.target.matches && ev.target.matches('[data-num]')) kindChosen = true;  // typed numbers mean the type is settled
    paint(); refresh();
  });
  form.addEventListener('submit', (ev) => {
    if (form.classList.contains('is-sending')) { ev.preventDefault(); return; }   // no double taps
    form.classList.add('is-sending'); fx.send(); try { sessionStorage.setItem('bola-fresh', String(Date.now())); } catch (e) { /* optional */ }
    rememberLast();
    const b = ev.submitter;
    if (b) { b.dataset.label = b.textContent; b.textContent = 'Placing\u2026'; }
  });
  window.addEventListener('pageshow', () => {          // back button: un-stick the slip
    form.classList.remove('is-sending');
    submits.forEach((b) => { if (b.dataset.label) b.textContent = b.dataset.label; });
  });
  document.addEventListener('bola:closed', () => { closed = true; gate(); });

  /* ---------------------------------------------------------------- guided flow
     Type -> Numbers -> Amount -> Place. `stage` is the first step still to do. When the person finishes a step the
     page scrolls to the next one (only if it is not already comfortably in view) and glows it, so they always see
     where to go. Going back to edit an earlier step never scrolls: the progress rail and the coach line just update. */
  const secs = { 1: form.querySelector('[data-sec="1"]'), 2: form.querySelector('[data-sec="2"]'), 3: form.querySelector('[data-sec="3"]'), 4: bar };
  const rail = Array.from(form.querySelectorAll('[data-rail-step]'));
  const topbar = () => { const t = document.querySelector('.topbar'); return t ? t.offsetHeight : 0; };
  function setTop() { form.style.setProperty('--bs-top', topbar() + 'px'); }
  setTop(); window.addEventListener('resize', setTop);

  function glow(el) {
    if (!el) return;
    el.classList.remove('is-focus'); void el.offsetWidth; el.classList.add('is-focus');
    clearTimeout(el._glow); el._glow = setTimeout(() => el.classList.remove('is-focus'), 2400);
  }
  function ping() {
    const b = submits[0]; if (!b || b.disabled) return;
    b.classList.remove('is-ping'); void b.offsetWidth; b.classList.add('is-ping');
    clearTimeout(b._ping); b._ping = setTimeout(() => b.classList.remove('is-ping'), 2600);
  }
  function currentStage() {
    if (!kindChosen) return 1;
    if (need() - picked().length > 0) return 2;
    if (!(amt(tumbok) + amt(sahod) > 0)) return 3;
    return 4;
  }
  function focusStep(step, force) {
    const el = secs[step];
    if (!el) return;
    if (step === 4) {                                  // the action bar is pinned to the bottom: only fetch it if it is off screen
      const r = el.getBoundingClientRect();
      if (force || r.top < 0 || r.bottom > window.innerHeight) el.scrollIntoView({ block: 'end', behavior: reduce ? 'auto' : 'smooth' });
      glow(el); ping();
      return;
    }
    const r = el.getBoundingClientRect(), off = topbar() + 64;            // site bar + the progress rail
    const comfy = r.top >= off - 4 && r.top < window.innerHeight * 0.5;
    if (force || !comfy) window.scrollTo({ top: Math.max(0, r.top + window.scrollY - off), behavior: reduce ? 'auto' : 'smooth' });
    glow(el);
  }
  function advance(step) {
    clearTimeout(advTimer);
    // wait for the ball-landing / tap animation to finish before moving the page
    advTimer = setTimeout(() => { if (stage === step) { play('tick'); focusStep(step, false); } }, step === 3 ? 720 : step === 4 ? 420 : 240);
  }
  function coach() {
    if (!why) return;
    const left = need() - picked().length;
    let msg, tone2;
    if (closed) { msg = 'Betting is closed for this draw'; tone2 = 'closed'; }
    else if (stage === 1) { msg = 'Step 1: choose a ticket type'; tone2 = 'todo'; }
    else if (stage === 2) { msg = 'Step 2: pick ' + (left === need() ? need() + ' numbers' : left + ' more number' + (left > 1 ? 's' : '')); tone2 = 'todo'; }
    else if (stage === 3) { msg = 'Step 3: tap an amount to play'; tone2 = 'todo'; }
    else { msg = 'All set. Check the total, then place your bet'; tone2 = 'ok'; }
    why.textContent = msg + (tone2 === 'todo' ? '  \u2192' : '');
    why.dataset.tone = tone2;
    why.title = tone2 === 'todo' ? 'Take me to this step' : '';
  }
  function progress() {
    const next = currentStage(), forward = next > stage, prev = stage;
    stage = next;
    form.style.setProperty('--prog', String(valid ? 1 : (stage - 1) / 4));
    if (booted && forward) {                          // every step just completed gets a small celebration
      for (let n = prev; n < next; n += 1) {
        const li = rail[n - 1]; if (!li) continue;
        li.classList.remove('just-done'); void li.offsetWidth; li.classList.add('just-done');
        setTimeout(() => li.classList.remove('just-done'), 800);
        sparks(li.querySelector('b'), 8, false);
      }
      fx.step();
    }
    idleReset();
    rail.forEach((li) => {
      const n = +li.dataset.railStep;
      li.classList.toggle('is-done', n < stage); li.classList.toggle('is-now', n === stage);
      li.firstElementChild.setAttribute('aria-current', n === stage ? 'step' : 'false');
    });
    Object.keys(secs).forEach((k) => { if (secs[k]) secs[k].classList.toggle('is-now', +k === stage && !closed); });
    coach();
    if (booted && forward && !closed) advance(next);
  }

  /* a wave runs outward across the board from the number you just tapped */
  function ripple(from) {
    if (reduce || !from || !from.animate) return;
    const c = from.getBoundingClientRect(), cx = c.left + c.width / 2, cy = c.top + c.height / 2;
    balls.forEach((b) => {
      if (b === from || b.getAttribute('aria-pressed') === 'true') return;
      const r = b.getBoundingClientRect(), d = Math.hypot(r.left + r.width / 2 - cx, r.top + r.height / 2 - cy);
      if (d > 280) return;
      b.animate([{ transform: 'none' }, { transform: 'translateY(-5px) scale(1.09)', offset: 0.45 }, { transform: 'none' }],
        { duration: 380, delay: d * 0.7, easing: 'ease-out' });
    });
  }
  /* all the numbers are in: the tray chain lights up slot by slot */
  function lockIn() {
    if (!picksBox) return;
    picksBox.classList.remove('is-locked'); void picksBox.offsetWidth; picksBox.classList.add('is-locked');
    setTimeout(() => picksBox.classList.remove('is-locked'), 1400);
    fx.lock(); sparks(picksBox, 14, true);
  }
  /* everything is filled in: the total / Place bar charges up */
  function readyBurst() {
    fx.ready();
    sparks(submits[0], 16, true);
    const hot = sum.win && sum.win.parentElement;
    if (hot) { hot.classList.remove('is-hot'); void hot.offsetWidth; hot.classList.add('is-hot'); setTimeout(() => hot.classList.remove('is-hot'), 1600); }
  }
  /* gone quiet for a while: gently point at the step still waiting */
  function idleReset() {
    clearTimeout(idleTimer);
    if (!booted || closed || stage >= 4) return;
    idleTimer = setTimeout(() => {
      if (document.visibilityState !== 'visible' || stage >= 4 || closed) return;
      glow(secs[stage]);
      if (why) { why.classList.remove('is-nudge'); void why.offsetWidth; why.classList.add('is-nudge'); }
    }, 14000);
  }
  form.addEventListener('pointerdown', (ev) => {       // soft press ripple on every tappable control of the slip
    if (reduce) return;
    const t = ev.target.closest('.bs-kind,.bs-chip,.bs-pill,.bs-rail button,.bs-bar .btn,.bs-repeat');
    if (!t || t.disabled) return;
    const r = t.getBoundingClientRect(), i = document.createElement('bs-rip'), s = Math.max(r.width, r.height) * 1.6;   // own tag: pills hide empty <i>
    i.className = 'bs-rip';
    i.style.cssText = 'width:' + s + 'px;height:' + s + 'px;left:' + (ev.clientX - r.left - s / 2) + 'px;top:' + (ev.clientY - r.top - s / 2) + 'px';
    t.appendChild(i); setTimeout(() => i.remove(), 620);
  });
  ['pointerdown', 'keydown'].forEach((e) => form.addEventListener(e, idleReset));

  /* tap a filled slot to take that number back out */
  if (tray) tray.addEventListener('click', (ev) => {
    const s = ev.target.closest('.bs-slot.is-on');
    if (!s || busy) return;
    const cur = picked(); cur.splice(Array.prototype.indexOf.call(tray.children, s), 1);
    fx.unpick(); setPicked(cur);
  });

  /* repeat the last ticket placed from this device */
  const LAST = 'snx.bola.last', again = form.querySelector('[data-bola-repeat]');
  function rememberLast() {
    try { localStorage.setItem(LAST, JSON.stringify({ k: kind.value, n: picked(), t: tumbok.value, s: sahod.value })); } catch (e) { /* optional */ }
  }
  function lastTicket() {
    try {
      const d = JSON.parse(localStorage.getItem(LAST) || 'null'), max = parseInt(host.dataset.max, 10) || 40;
      if (!d || !COUNT[d.k] || !Array.isArray(d.n) || d.n.length !== COUNT[d.k]) return null;
      if (!d.n.every((n) => Number.isInteger(n) && n >= 1 && n <= max) || new Set(d.n).size !== d.n.length) return null;
      if (!(parseFloat(d.t) > 0 || parseFloat(d.s) > 0)) return null;
      return d;
    } catch (e) { return null; }
  }
  function offerRepeat() {
    const d = again && lastTicket();
    if (!d) return;
    const label = form.querySelector('[data-kind="' + d.k + '"] strong');
    again.textContent = '\u21BB Repeat last ticket \u00B7 ' + (label ? label.textContent + ' ' : '') + d.n.join('\u2013') + ' \u00B7 ' + money(parseFloat(d.t) || 0) + ' / ' + money(parseFloat(d.s) || 0);
    again.hidden = false;
  }
  function repeatLast() {
    const d = lastTicket();
    if (!d || busy) return;
    kind.value = d.k; kindChosen = true; slot = 'tumbok';
    put('tumbok', parseFloat(d.t) || 0); put('sahod', parseFloat(d.s) || 0);
    linked = amt(tumbok) > 0 && amt(tumbok) === amt(sahod);
    if (other) other.value = '';
    play('ok'); buzz(12);
    syncKind(); setPicked(d.n);
  }

  buildGrid();
  syncKind();
  offerRepeat();
  booted = true;
})();
