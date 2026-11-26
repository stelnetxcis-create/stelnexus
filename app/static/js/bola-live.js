/* Number game: ticket print reveal, live draw screen, win celebration.
   Presentation only. The declarator still enters the balls; this file watches /bola/draws/<id>/live
   (status + balls once settled + the viewer's own winnings) and dresses the moment up. */
(function () {
  const snx = window.snx || {};
  const play = (n) => { try { if (snx.Sound) snx.Sound.play(n); } catch (e) { /* sound is optional */ } };
  const buzz = (p) => { try { if (navigator.vibrate) navigator.vibrate(p); } catch (e) { /* optional */ } };
  const reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const tone = (n) => 'c' + ((Math.floor((n - 1) / 10) % 5) + 1);
  const peso = (c) => '\u20B1' + (c / 100).toLocaleString('en-PH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const rand = (a, b) => a + Math.random() * (b - a);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const store = (kind) => { try { return window[kind]; } catch (e) { return null; } };
  const seen = (kind, key) => { try { return !!store(kind).getItem(key); } catch (e) { return false; } };
  const mark = (kind, key) => { try { store(kind).setItem(key, '1'); } catch (e) { /* optional */ } };

  /* ================================================================ 1. ticket slides out of the printer */
  (function printReveal() {
    const stage = document.querySelector('[data-bt-stage]');
    if (!stage) return;
    const modal = document.querySelector('[data-bt-modal]');
    let fresh = !!modal;
    if (!fresh) {
      try { const t = parseInt(sessionStorage.getItem('bola-fresh') || '0', 10); fresh = t && Date.now() - t < 90000; sessionStorage.removeItem('bola-fresh'); } catch (e) { fresh = false; }
    }
    if (modal) {
      document.body.classList.add('bl-lock');
      const close = () => {
        modal.classList.add('is-leaving');
        setTimeout(() => { modal.remove(); document.body.classList.remove('bl-lock'); }, reduce ? 0 : 280);
        try { const u = new URL(location.href); u.searchParams.delete('sold'); history.replaceState(null, '', u.pathname + u.search + u.hash); } catch (e) { /* optional */ }
      };
      modal.addEventListener('click', (ev) => {
        if (ev.target.closest('[data-bt-close]') || ev.target === modal) close();
        if (ev.target.closest('[data-bt-print]')) window.print();
      });
      document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && document.body.contains(modal)) close(); });
    }
    if (!fresh || reduce) { stage.classList.add('is-done'); return; }
    stage.setAttribute('data-bt-play', '');
    let k = 0;
    const feed = setInterval(() => { play('tick'); if ((k += 1) >= 7) clearInterval(feed); }, 200);
    setTimeout(() => { play('ok'); buzz([14, 40, 24]); sparkle(stage); }, 1750);
    setTimeout(() => stage.classList.add('is-done'), 1800);
  })();
  function sparkle(host) {
    for (let i = 0; i < 26; i += 1) {
      const s = el('i', 'bt-spark');
      s.style.cssText = 'left:' + rand(8, 92) + '%;top:' + rand(12, 80) + '%;--dx:' + Math.round(rand(-40, 40)) + 'px;--dy:' + Math.round(rand(-70, -15)) + 'px;animation-delay:' + Math.round(rand(0, 500)) + 'ms';
      host.appendChild(s);
      setTimeout(() => s.remove(), 1700);
    }
  }

  /* ================================================================ 2. win celebration */
  function celebrate(opt) {
    if (!opt || !(opt.cents > 0)) return;
    if (opt.key && seen('localStorage', 'bola-win:' + opt.key)) return;
    if (opt.key) mark('localStorage', 'bola-win:' + opt.key);
    const scr = el('div', 'bw-screen'); scr.setAttribute('role', 'dialog'); scr.setAttribute('aria-modal', 'true'); scr.setAttribute('aria-label', 'You won ' + peso(opt.cents));
    scr.appendChild(el('div', 'bw-rays'));
    const card = el('div', 'bw-card');
    card.appendChild(el('small', '', (opt.codes && opt.codes.length > 1 ? 'Winning tickets' : 'Winning ticket')));
    const title = el('h2', 'bw-title');
    'WINNER!'.split('').forEach((ch, i) => { const s = el('span', '', ch); s.style.animationDelay = (0.15 + i * 0.07) + 's'; title.appendChild(s); });
    card.appendChild(title);
    const amt = el('div', 'bw-amount', reduce ? peso(opt.cents) : peso(0)); card.appendChild(amt);
    if (opt.codes && opt.codes.length) {
      const row = el('div', 'bw-codes');
      opt.codes.forEach((c) => row.appendChild(el('span', '', c)));
      card.appendChild(row);
    }
    card.appendChild(el('p', '', 'Credited to the player\u2019s wallet automatically.'));
    const btn = el('button', 'btn btn-brand', 'Collect'); btn.type = 'button'; card.appendChild(btn);
    scr.appendChild(card);
    if (!reduce) {
      for (let i = 0; i < 44; i += 1) {
        const c = el('i', 'bw-coin');
        c.style.cssText = 'left:' + rand(0, 100) + '%;animation-duration:' + rand(2.4, 4.6) + 's;animation-delay:' + rand(0, 2.2) + 's;--s:' + rand(0.7, 1.4);
        scr.appendChild(c);
      }
    }
    document.body.appendChild(scr); document.body.classList.add('bl-lock');
    play('win'); buzz([30, 60, 30, 60, 90]);
    if (!reduce) {
      const t0 = performance.now(), dur = 1900;
      (function step(t) {
        const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 4);
        amt.textContent = peso(Math.round(opt.cents * e));
        if (p < 1) requestAnimationFrame(step); else { amt.classList.add('is-done'); play('ok'); }
      })(t0);
    }
    const close = () => { scr.classList.add('is-leaving'); setTimeout(() => { scr.remove(); document.body.classList.remove('bl-lock'); if (opt.onClose) opt.onClose(); }, reduce ? 0 : 300); document.removeEventListener('keydown', onKey); };
    const onKey = (ev) => { if (ev.key === 'Escape') close(); };
    btn.addEventListener('click', close); document.addEventListener('keydown', onKey); btn.focus();
  }
  snx.bolaCelebrate = celebrate;

  /* ================================================================ 3. live draw screen */
  const root = document.querySelector('[data-bola-live]');
  if (!root) return;
  const id = root.dataset.drawId, url = root.dataset.liveUrl;
  let at = parseInt(root.dataset.scheduledMs, 10);     // follows the server: a skip or a reschedule moves it
  const clock = root.querySelector('[data-bl-clock]');
  const offKey = 'bola-live-off:' + id;
  let skew = 0, last = null, screen = null, mode = '', revealed = false, polling = null, timerId = null;

  const left = () => at - (Date.now() + skew);
  const mmss = (ms) => { const s = Math.max(0, Math.ceil(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60, two = (x) => String(x).padStart(2, '0'); return h ? h + ':' + two(m) + ':' + two(r) : two(m) + ':' + two(r); };

  function build() {
    const scr = el('div', 'bl-screen'); scr.setAttribute('role', 'dialog'); scr.setAttribute('aria-modal', 'true'); scr.setAttribute('aria-label', 'Live draw');
    const x = el('button', 'bl-x', '\u2715'); x.type = 'button'; x.setAttribute('aria-label', 'Close live draw');
    const head = el('div', 'bl-title'); head.appendChild(el('i', 'bl-live')); head.appendChild(el('span', '', 'LIVE DRAW \u00B7 ' + (root.dataset.title || '')));
    const globe = el('div', 'bl-globe'); const glass = el('div', 'bl-glass');
    const max = parseInt(root.dataset.max, 10) || 40;
    for (let i = 0; i < 22; i += 1) {
      const n = 1 + Math.floor(Math.random() * max), b = el('span', 'bl-b ' + tone(n), String(n));
      b.style.cssText = '--x:' + Math.round(rand(40, 84)) + 'px;--y:' + Math.round(rand(40, 84)) + 'px;--r:' + Math.round(rand(-300, 300)) + 'deg;--d1:-' + rand(0, 2).toFixed(2) + 's;--d2:-' + rand(0, 2).toFixed(2) + 's;--dx:' + rand(0.9, 1.7).toFixed(2) + 's;--dy:' + rand(0.7, 1.4).toFixed(2) + 's';
      glass.appendChild(b);
    }
    globe.appendChild(glass); globe.appendChild(el('div', 'bl-base'));
    const count = el('div', 'bl-count', '\u2013'), say = el('div', 'bl-say', ''), slots = el('div', 'bl-slots');
    [['Tumbok', 's1'], ['Sahod', 's2']].forEach((s) => { const w = el('div', 'bl-slot ' + s[1]); w.appendChild(el('small', '', s[0])); w.appendChild(el('b', 'bl-ball', '?')); slots.appendChild(w); });
    const sr = el('div', 'bl-sr'); sr.setAttribute('aria-live', 'polite');
    const done = el('button', 'btn btn-brand bl-done', 'Close'); done.type = 'button'; done.hidden = true;
    /* Skip the wait: only for accounts the server allowed (data-skip-url is absent otherwise; the server checks again). */
    const skip = el('button', 'btn bl-skip', 'Skip wait'); skip.type = 'button'; skip.hidden = !root.dataset.skipUrl;
    [x, head, globe, count, say, slots, skip, done, sr].forEach((n) => scr.appendChild(n));
    x.addEventListener('click', closeScreen); done.addEventListener('click', closeScreen);
    skip.addEventListener('click', () => {
      if (!root.dataset.skipUrl || !window.confirm('Skip the wait? Betting closes now and the draw becomes due at once.')) return;
      skip.disabled = true;
      const f = el('form'); f.method = 'post'; f.action = root.dataset.skipUrl;
      const t = el('input'); t.type = 'hidden'; t.name = 'csrf_token'; t.value = root.dataset.csrf || '';
      f.appendChild(t); document.body.appendChild(f); f.submit();
    });
    scr._ = { count, say, slots, sr, done, skip };
    return scr;
  }
  function open() {
    if (screen || (last && last.status === 'settled' && revealed)) return;
    screen = build(); document.body.appendChild(screen); document.body.classList.add('bl-lock');
    setMode(last && last.status === 'settled' ? 'drawing' : (left() > 0 ? 'count' : 'drawing'));
    screen.querySelector('.bl-x').focus();
    if (last && last.status === 'settled') reveal(last);
  }
  function closeScreen() {
    if (!screen) return;
    mark('sessionStorage', offKey);
    const reload = revealed; const s = screen; screen = null;
    s.classList.add('is-leaving'); setTimeout(() => { s.remove(); document.body.classList.remove('bl-lock'); if (reload) location.reload(); }, reduce ? 0 : 300);
  }
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && screen && !document.querySelector('.bw-screen')) closeScreen(); });
  function setMode(m) { mode = m; if (screen) { screen.dataset.mode = m; if (screen._.skip && root.dataset.skipUrl) screen._.skip.hidden = m !== 'count'; } }

  /* the draw: shuffle, then each ball rolls out and its number spins before it lands */
  function reveal(d) {
    if (!screen || revealed) return;
    revealed = true; setMode('reveal');
    const S = screen._, finals = [d.ball_first, d.ball_second], won = d.won_cents > 0;
    const key = 'bola-seen:' + location.pathname + ':' + finals.join('/'); mark('sessionStorage', key);
    S.count.textContent = ''; S.say.textContent = 'And the numbers are\u2026';
    const land = (i) => {
      const b = S.slots.children[i].querySelector('.bl-ball'), n = finals[i];
      if (reduce) { b.textContent = String(n); b.className = 'bl-ball is-landed ' + tone(n); return; }
      b.className = 'bl-ball is-rolling'; play('tick');
      const spin = setInterval(() => { b.textContent = String(1 + Math.floor(Math.random() * 40)); }, 55);
      setTimeout(() => { clearInterval(spin); b.textContent = String(n); b.className = 'bl-ball is-landed ' + tone(n); play('ok'); buzz(20); }, 1000);
    };
    const t1 = reduce ? 0 : 1500, t2 = t1 + (reduce ? 0 : 1700), t3 = t2 + (reduce ? 300 : 1500);
    screen.classList.add('is-shuffle');
    setTimeout(() => { if (screen) { screen.classList.remove('is-shuffle'); land(0); } }, t1);
    setTimeout(() => { if (screen) land(1); }, t2);
    setTimeout(() => {
      if (!screen) return;
      S.say.textContent = 'Result ' + finals[0] + ' / ' + finals[1];
      S.sr.textContent = 'Result: tumbok ' + finals[0] + ', sahod ' + finals[1] + '.';
      S.done.hidden = false; S.done.focus(); screen.classList.add('is-final');
      if (won) setTimeout(() => celebrate({ cents: d.won_cents, codes: d.won_codes, key: 'draw:' + id + ':' + d.version + ':' + d.won_cents, onClose: () => { if (screen) closeScreen(); } }), reduce ? 0 : 700);
    }, t3);
  }

  function tick() {
    const l = left();
    if (clock) {
      if (last && last.status === 'settled') clock.textContent = 'Result is in';
      else clock.textContent = l > 0 ? 'Draw in ' + mmss(l) : 'Drawing now\u2026';
    }
    const auto = root.dataset.auto === '1' && !seen('sessionStorage', offKey);
    if (!screen && auto && l <= 15000 && l > -900000 && !(last && last.status !== 'open' && last.status !== 'locked' && last.status !== 'scheduled')) open();
    if (!screen || mode === 'reveal') return;
    const S = screen._;
    if (l > 0) {
      setMode('count');
      const txt = l > 10000 ? mmss(l) : String(Math.ceil(l / 1000));
      if (S.count.textContent !== txt) { S.count.textContent = txt; S.count.classList.remove('is-pop'); void S.count.offsetWidth; S.count.classList.add('is-pop'); if (l <= 10000) play('tick'); }
      S.say.textContent = 'The draw starts in';
    } else {
      setMode('drawing'); S.count.textContent = ''; S.say.textContent = 'Drawing\u2026 waiting for the result';
    }
  }

  function poll() {
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } }).then((r) => r.json()).then((d) => {
      skew = d.now_ms - Date.now(); last = d;
      if (d.scheduled_ms) at = d.scheduled_ms;
      if (d.status === 'settled' && d.ball_first != null) {
        if (screen) reveal(d);
        else if (d.won_cents > 0) celebrate({ cents: d.won_cents, codes: d.won_codes, key: 'draw:' + id + ':' + d.version + ':' + d.won_cents });
        if (revealed || !screen) { clearInterval(polling); if (!screen && !root.hidden) { root.hidden = true; } }
      } else if (d.status === 'void') {
        clearInterval(polling); root.hidden = true;
        if (screen) { screen._.say.textContent = 'This draw was called off. Every stake is refunded.'; screen._.count.textContent = ''; screen._.done.hidden = false; setMode('void'); }
      }
    }).catch(() => { /* try again on the next beat */ });
  }

  root.addEventListener('click', (ev) => { if (ev.target.closest('[data-bl-open]')) { try { sessionStorage.removeItem(offKey); } catch (e) { /* optional */ } open(); } });
  poll(); polling = setInterval(poll, 3000);
  timerId = setInterval(tick, 250); tick();
})();
