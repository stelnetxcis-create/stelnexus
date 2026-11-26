/* StelNexus ARENA behaviour: pointer tilt + glare on game boxes, press ripple + sparks, scroll reveal,
   count-up on box numbers, navigation progress bar. No dependencies. Does nothing under reduced-motion. */
(function () {
  'use strict';
  if (matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('fx-min')) return;
  var lite = document.documentElement.classList.contains('fx-lite');   // lite / min: no cursor glow, tilt, sparks or count-up
  var fine = matchMedia('(pointer: fine)').matches && !lite;
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* 1) tilt + glare + picture drift. One delegated listener, rAF-throttled. */
  var TILT = '.lobby-card,.game-tiles .game-tile,.games .game-tile.is-live,.kpi';
  if (fine) {
    var cur = null, raf = 0, ev = null;
    var reset = function (el) {
      if (!el) return;
      el.style.setProperty('--rx', '0deg'); el.style.setProperty('--ry', '0deg');
      el.style.setProperty('--px', '0'); el.style.setProperty('--py', '0');
      el.classList.remove('is-tilting');
    };
    document.addEventListener('pointermove', function (e) {
      var c = e.target.closest ? e.target.closest(TILT) : null;
      if (cur && cur !== c) reset(cur);
      cur = c; ev = e;
      if (!c || raf) return;
      raf = requestAnimationFrame(function () {
        raf = 0; if (!cur) return;
        var r = cur.getBoundingClientRect(), nx = (ev.clientX - r.left) / r.width - .5, ny = (ev.clientY - r.top) / r.height - .5;
        var k = cur.matches('.game-tile') && !cur.closest('.games') ? 14 : 8;
        cur.classList.add('is-tilting');
        cur.style.setProperty('--ry', (nx * k).toFixed(2) + 'deg');
        cur.style.setProperty('--rx', (-ny * k).toFixed(2) + 'deg');
        cur.style.setProperty('--px', (nx * 2).toFixed(2));
        cur.style.setProperty('--py', (ny * 2).toFixed(2));
        cur.style.setProperty('--mx', (ev.clientX - r.left) + 'px');
        cur.style.setProperty('--my', (ev.clientY - r.top) + 'px');
      });
    }, { passive: true });
    document.addEventListener('pointerleave', function () { reset(cur); cur = null; }, true);
    window.addEventListener('blur', function () { reset(cur); cur = null; });

    /* soft gold light that follows the cursor behind the page */
    var glow = document.createElement('div'); glow.className = 'cursor-glow'; glow.setAttribute('aria-hidden', 'true'); document.body.appendChild(glow);
    var gx = 0, gy = 0, gr = 0;
    document.addEventListener('pointermove', function (e) {
      gx = e.clientX; gy = e.clientY; glow.classList.add('on');
      if (!gr) gr = requestAnimationFrame(function () { glow.style.transform = 'translate3d(' + gx + 'px,' + gy + 'px,0)'; gr = 0; });
    }, { passive: true });
    document.documentElement.addEventListener('pointerleave', function () { glow.classList.remove('on'); });
  }

  /* 2) press feedback: ripple inside the box, a few gold sparks for the big boxes */
  var PRESS = '.lobby-card,.game-tile,.btn,.chip-btn';
  var BIG = '.lobby-card,.game-tile';
  document.addEventListener('pointerdown', function (e) {
    var t = e.target.closest ? e.target.closest(PRESS) : null;
    if (!t || e.button > 0) return;
    var r = t.getBoundingClientRect(), s = document.createElement('span');
    s.className = 'rpl'; s.style.left = (e.clientX - r.left) + 'px'; s.style.top = (e.clientY - r.top) + 'px';
    if (getComputedStyle(t).position === 'static') t.style.position = 'relative';
    t.appendChild(s); s.addEventListener('animationend', function () { s.remove(); });
    if (lite || !t.matches(BIG)) return;
    for (var i = 0; i < 7; i++) {
      var p = document.createElement('i'), a = Math.random() * 6.283, d = 34 + Math.random() * 46;
      p.className = 'spk'; p.style.setProperty('--x', e.clientX + 'px'); p.style.setProperty('--y', e.clientY + 'px');
      p.style.setProperty('--dx', (Math.cos(a) * d).toFixed(0) + 'px'); p.style.setProperty('--dy', (Math.sin(a) * d - 16).toFixed(0) + 'px');
      document.body.appendChild(p); p.addEventListener('animationend', function () { this.remove(); });
    }
  }, { passive: true });

  /* 3) count-up on the numbers inside event boxes */
  $$('.lobby-nums .n-done b').forEach(function (el, idx) {
    var n = parseInt(el.textContent, 10);
    if (lite || !isFinite(n) || n <= 0) return;
    var t0 = null, delay = 250 + (idx % 40) * 12;
    el.textContent = '0';
    (function tick(t) {
      if (t0 === null) t0 = t + delay;
      var p = Math.min(Math.max((t - t0) / 800, 0), 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(n * e);
      if (p < 1) requestAnimationFrame(tick);
    })(performance.now());
  });

  /* 4) rows of tables glide in one after another (first 14 only) */
  $$('.table tbody').forEach(function (tb) {
    Array.prototype.slice.call(tb.children, 0, 14).forEach(function (tr, i) { tr.style.setProperty('--r', i); });
  });

  /* 5) scroll reveal: only boxes that start below the fold are hidden, so nothing is ever lost */
  if ('IntersectionObserver' in window) {
    var vh = innerHeight, targets = $$('.card,.kpi,.lobby-card,.game-tiles .game-tile,.games .game-tile').filter(function (el) {
      return !el.closest('dialog') && el.getBoundingClientRect().top > vh + 20;
    });
    var io = new IntersectionObserver(function (list) {
      list.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add('rv-in'); io.unobserve(en.target); } });
    }, { rootMargin: '0px 0px -6% 0px', threshold: .05 });
    targets.forEach(function (el) { el.classList.add('rv'); io.observe(el); });
    setTimeout(function () { targets.forEach(function (el) { el.classList.add('rv-in'); }); }, 4000); // safety net
  }

  /* 6) lobby filter: boxes that stay visible replay their entrance */
  var bar = document.querySelector('[data-lobby-filter]');
  if (bar) bar.addEventListener('click', function () {
    var i = 0;
    $$('.lobby-card').forEach(function (c) {
      if (c.hidden) return;
      c.style.setProperty('--i', i++);
      c.style.animation = 'none'; void c.offsetWidth; c.style.animation = '';
    });
  });

  /* 7) navigation progress bar + soft fade when leaving the page */
  var prog = document.createElement('div'); prog.className = 'nav-progress'; prog.setAttribute('aria-hidden', 'true'); document.body.appendChild(prog);
  var go = function () { document.body.classList.add('is-leaving'); prog.classList.add('on'); };
  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target && a.target !== '_self') return;
    var h = a.getAttribute('href');
    if (!h || h.charAt(0) === '#' || /^(mailto:|tel:|javascript:)/i.test(h) || a.hasAttribute('download')) return;
    try { if (new URL(a.href, location.href).origin !== location.origin) return; } catch (x) { return; }
    go();
  });
  window.addEventListener('pageshow', function () { document.body.classList.remove('is-leaving'); prog.classList.remove('on'); });
})();
