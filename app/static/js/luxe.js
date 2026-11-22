/* StelNexus LUXE behaviour: count-up on stat values + pointer spotlight. No deps, ~1KB. */
(function () {
  'use strict';
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('fx-min');
  if (reduce) return;

  // Count-up: only for values shaped like  <prefix><number><suffix>  with no digits in prefix/suffix.
  var re = /^([^\d-]*)(-?\d[\d,]*(?:\.\d+)?)([^\d]*)$/;
  document.querySelectorAll('.stat-value').forEach(function (el) {
    var txt = el.textContent.trim(), m = re.exec(txt);
    if (!m) return;
    var raw = m[2], target = parseFloat(raw.replace(/,/g, ''));
    if (!isFinite(target) || target === 0) return;
    var dec = (raw.split('.')[1] || '').length, commas = raw.indexOf(',') > -1;
    var fmt = function (n) {
      var s = n.toFixed(dec);
      return commas ? Number(s).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }) : s;
    };
    var t0 = null, dur = 900;
    el.setAttribute('aria-label', txt);
    (function tick(t) {
      if (t0 === null) t0 = t;
      var p = Math.min((t - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = p < 1 ? m[1] + fmt(target * e) + m[3] : txt;
      if (p < 1) requestAnimationFrame(tick);
    })(performance.now());
  });

  // Spotlight: one delegated listener, rAF-throttled, fine pointers only. Not in lite / min.
  if (document.documentElement.classList.contains('fx-lite') || !matchMedia('(pointer: fine)').matches) return;
  var pending = null, last = null;
  document.addEventListener('pointermove', function (e) {
    var c = e.target.closest && e.target.closest('.card,.stat');
    if (!c) return;
    last = { c: c, x: e.clientX, y: e.clientY };
    if (pending) return;
    pending = requestAnimationFrame(function () {
      var r = last.c.getBoundingClientRect();
      last.c.style.setProperty('--mx', (last.x - r.left) + 'px');
      last.c.style.setProperty('--my', (last.y - r.top) + 'px');
      pending = null;
    });
  }, { passive: true });
})();

/* ===== LUXE 3D: coin, gold dust, tilt ===== */
(function () {
  'use strict';
  if (matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('fx-min')) return;

  // 1) login hero coin: JS-driven angle so there is no loop seam and speed changes are smooth
  var brand = document.querySelector('.auth-card .auth-brand');
  if (brand) {
    var stage = document.createElement('div');
    stage.className = 'coin-stage'; stage.setAttribute('aria-hidden', 'true');
    var coin = document.createElement('div'); coin.className = 'coin';
    for (var k = -6; k <= 6; k++) { var s = document.createElement('i'); s.style.setProperty('--k', k); coin.appendChild(s); }
    ['f', 'b'].forEach(function (c) { var d = document.createElement('div'); d.className = 'face ' + c; coin.appendChild(d); });
    stage.appendChild(coin); brand.parentNode.insertBefore(stage, brand);
    var small = brand.querySelector('img'); if (small) small.style.display = 'none';
    var ang = 20, spd = 50, last = performance.now(), cr = 0;
    var spin = function (t) {
      var dt = Math.min((t - last) / 1000, .05); last = t;
      var target = document.body.classList.contains('is-submitting') ? 420 : 50;
      spd += (target - spd) * Math.min(dt * 4, 1);
      ang = (ang + spd * dt) % 360;
      coin.style.transform = 'rotateX(14deg) rotateY(' + ang.toFixed(2) + 'deg)';
      stage.style.setProperty('--sh', (.4 + .6 * Math.abs(Math.cos(ang * Math.PI / 180))).toFixed(3));
      cr = requestAnimationFrame(spin);
    };
    cr = requestAnimationFrame(spin);
    document.addEventListener('visibilitychange', function () { if (document.hidden) cancelAnimationFrame(cr); else { last = performance.now(); cr = requestAnimationFrame(spin); } });
  }

  // lite / min: no full-screen canvas running at 60 fps, no tilt (the coin above is login-only and stays)
  if (document.documentElement.classList.contains('fx-lite')) return;

  // 2) gold dust: ~40 particles on one canvas, paused when tab hidden
  var cv = document.createElement('canvas'); cv.id = 'gold-dust'; cv.setAttribute('aria-hidden', 'true');
  document.body.prepend(cv);
  var ctx = cv.getContext('2d'), W = 0, H = 0, dpr = Math.min(devicePixelRatio || 1, 2), P = [], raf = 0;
  var N = innerWidth < 860 ? 22 : 42;
  function size() { W = innerWidth; H = innerHeight; cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); }
  function spawn(i) { return { x: Math.random() * W, y: i ? H + 10 : Math.random() * H, r: Math.random() * 1.6 + .4, v: Math.random() * .35 + .1, a: Math.random() * .5 + .15, p: Math.random() * 6.28 }; }
  size(); for (var i = 0; i < N; i++) P.push(spawn(0));
  addEventListener('resize', size, { passive: true });
  function draw(t) {
    if (document.documentElement.classList.contains('modal-open')) { raf = requestAnimationFrame(draw); return; }
    ctx.clearRect(0, 0, W, H);
    for (var j = 0; j < N; j++) {
      var q = P[j]; q.y -= q.v; q.x += Math.sin(t / 2200 + q.p) * .25;
      if (q.y < -10) P[j] = q = spawn(1);
      var tw = .6 + .4 * Math.sin(t / 700 + q.p);
      ctx.beginPath(); ctx.fillStyle = 'rgba(247,222,147,' + (q.a * tw).toFixed(3) + ')';
      ctx.arc(q.x, q.y, q.r, 0, 6.283); ctx.fill();
    }
    raf = requestAnimationFrame(draw);
  }
  raf = requestAnimationFrame(draw);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) cancelAnimationFrame(raf); else raf = requestAnimationFrame(draw);
  });

  // 3) 3D tilt on stat tiles / pools (fine pointers only)
  if (!matchMedia('(pointer: fine)').matches) return;
  var el = null, nx = 0, ny = 0, tick = 0;
  document.addEventListener('pointermove', function (e) {
    var c = e.target.closest && e.target.closest('.stat,.pool-side');
    if (el && el !== c) { el.style.setProperty('--rx', '0deg'); el.style.setProperty('--ry', '0deg'); el.classList.remove('is-tilting'); }
    el = c; if (!c) return;
    var r = c.getBoundingClientRect(); nx = (e.clientX - r.left) / r.width - .5; ny = (e.clientY - r.top) / r.height - .5;
    if (tick) return;
    tick = requestAnimationFrame(function () {
      el.classList.add('is-tilting');
      el.style.setProperty('--ry', (nx * 9).toFixed(2) + 'deg'); el.style.setProperty('--rx', (-ny * 9).toFixed(2) + 'deg'); tick = 0;
    });
  }, { passive: true });
  document.addEventListener('pointerleave', function () { if (el) { el.style.setProperty('--rx', '0deg'); el.style.setProperty('--ry', '0deg'); } }, true);
})();

/* ===== LUXE copy + sidebar collapse v2 ===== */
(function () {
  'use strict';
  // Replace the old default tagline only (custom taglines are respected)
  var OLD = 'Betting Operations Console';
  var p = document.querySelector('.auth-brand p');
  if (p && p.textContent.trim() === OLD) p.textContent = 'Every fight, settled to the centavo.';
  var sp = document.querySelector('.brand span');
  if (sp && sp.textContent.trim() === OLD) sp.textContent = 'Settled to the centavo';

  var side = document.getElementById('sidebar'); if (!side) return;
  var KEY = 'snx.nav.collapsed', body = document.body, root = document.documentElement;
  if (root.classList.contains('nav-collapsed')) body.classList.add('nav-collapsed');
  function set(on) { root.classList.toggle('nav-collapsed', on); body.classList.toggle('nav-collapsed', on); try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) {} sync(); }
  var h = document.createElement('button');
  h.type = 'button'; h.className = 'nav-handle';
  h.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>';
  function sync() {
    var c = body.classList.contains('nav-collapsed');
    h.setAttribute('aria-expanded', String(!c));
    h.setAttribute('aria-label', c ? 'Expand sidebar' : 'Collapse sidebar');
    h.title = (c ? 'Expand sidebar' : 'Collapse sidebar') + '  ( [ )';
  }
  h.addEventListener('click', function () { set(!body.classList.contains('nav-collapsed')); });
  document.body.appendChild(h); sync();
  document.addEventListener('keydown', function (e) {
    if (e.key !== '[' || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target.tagName; if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || e.target.isContentEditable) return;
    set(!body.classList.contains('nav-collapsed'));
  });
  // tooltips for icon-only mode (replaces native title)
  var tip = document.createElement('div'); tip.className = 'nav-tip'; tip.setAttribute('role', 'tooltip'); document.body.appendChild(tip);
  side.querySelectorAll('.nav-link').forEach(function (a) {
    a.setAttribute('data-label', a.getAttribute('title') || a.textContent.trim()); a.removeAttribute('title');
    a.addEventListener('mouseenter', function () {
      if (!body.classList.contains('nav-collapsed')) return;
      var r = a.getBoundingClientRect(); tip.textContent = a.getAttribute('data-label');
      tip.style.left = (r.right + 10) + 'px'; tip.style.top = (r.top + r.height / 2 - 15) + 'px'; tip.classList.add('on');
    });
    a.addEventListener('mouseleave', function () { tip.classList.remove('on'); });
    a.addEventListener('focus', function () { a.dispatchEvent(new Event('mouseenter')); });
    a.addEventListener('blur', function () { tip.classList.remove('on'); });
  });
})();
