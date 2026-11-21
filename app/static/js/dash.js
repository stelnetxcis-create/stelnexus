/* Dashboard: greeting + date in the viewer's own timezone (the server only knows UTC). */
(function () {
  'use strict';
  var h = new Date().getHours();
  var greeting = h < 5 ? 'Good evening' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  document.querySelectorAll('[data-greet]').forEach(function (el) { el.textContent = greeting; });
  document.querySelectorAll('[data-today]').forEach(function (el) {
    try { el.textContent = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }); } catch (e) { /* keep server date */ }
  });
})();

/* Dashboard FX: hero aurora + embers, and a little animated world inside every game tile.
   Purely decorative (aria-hidden, pointer-events none). Skipped entirely under reduced-motion. */
(function () {
  'use strict';
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (document.documentElement.classList.contains('fx-lite')) return;   // phones / weak devices: no particle layers at all
  var R = function (a, b) { return a + Math.random() * (b - a); };
  function mk(tag, cls, vars, text) {
    var e = document.createElement(tag); if (cls) e.className = cls;
    if (vars) for (var k in vars) e.style.setProperty('--' + k, vars[k]);
    if (text != null) e.textContent = text; return e;
  }

  var hero = document.querySelector('.hero');
  if (hero) {
    var hf = mk('span', 'hero-fx'); hf.setAttribute('aria-hidden', 'true');
    ['b1', 'b2', 'b3'].forEach(function (c) { hf.appendChild(mk('i', 'blob ' + c)); });
    for (var i = 0; i < 16; i++) hf.appendChild(mk('i', 'em', { x: R(38, 98).toFixed(1) + '%', s: R(2, 4.5).toFixed(1) + 'px', d: R(5, 9).toFixed(1) + 's', dl: (-R(0, 9)).toFixed(1) + 's', sw: R(-40, 40).toFixed(0) + 'px' }));
    hero.insertBefore(hf, hero.firstChild);
  }

  var builders = {
    sabong: function (fx) {
      fx.appendChild(mk('i', 'orb m')); fx.appendChild(mk('i', 'orb w')); fx.appendChild(mk('i', 'clash'));
      var cols = ['rgba(229,56,79,.7)', 'rgba(74,163,255,.7)', 'rgba(247,222,147,.7)'];
      for (var i = 0; i < 9; i++) fx.appendChild(mk('i', 'ft', { x: R(48, 96).toFixed(0) + '%', c: cols[i % 3], d: R(4.5, 8).toFixed(1) + 's', dl: (-R(0, 8)).toFixed(1) + 's', sw: R(-50, 50).toFixed(0) + 'px', r: R(-200, 200).toFixed(0) + 'deg' }));
    },
    number: function (fx) {
      for (var i = 0; i < 14; i++) fx.appendChild(mk('i', 'dg', { x: R(42, 96).toFixed(0) + '%', s: R(13, 26).toFixed(0) + 'px', d: R(5, 9).toFixed(1) + 's', dl: (-R(0, 9)).toFixed(1) + 's' }, Math.floor(R(0, 10))));
    },
    esports: function (fx) {
      fx.appendChild(mk('i', 'scan'));
      for (var i = 0; i < 8; i++) fx.appendChild(mk('i', 'px', { x: R(45, 96).toFixed(0) + '%', y: R(8, 88).toFixed(0) + '%', d: R(1.2, 3).toFixed(1) + 's', dl: (-R(0, 3)).toFixed(1) + 's' }));
    },
    nba: function (fx) { fx.appendChild(mk('i', 'sh')); fx.appendChild(mk('i', 'ball')); }
  };
  document.querySelectorAll('.games .game-tile[data-game]').forEach(function (tile) {
    var b = builders[tile.getAttribute('data-game')]; if (!b) return;
    var fx = mk('span', 'game-fx fx-' + tile.getAttribute('data-game')); fx.setAttribute('aria-hidden', 'true');
    b(fx); tile.insertBefore(fx, tile.firstChild);
  });
})();

/* Game tiles: a live tile opens a floating menu ("where to?") instead of jumping to one page.
   Click = shockwave ring + sparks from the click point, tile squash-pop, the rest of the page dims, the menu
   unfolds in 3D and its items fly out one by one. Picking an item plays a short "launch" before navigating.
   The tile is still a real link, so without JS / with a modified click it goes to its main page. */
(function () {
  'use strict';
  var wraps = Array.prototype.slice.call(document.querySelectorAll('[data-game-wrap]'));
  if (!wraps.length) return;
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('fx-min');   // minimal level: no burst, no staged open/close
  var lite = document.documentElement.classList.contains('fx-lite');
  var games = document.querySelector('.games');
  var HIDE_MS = reduce ? 0 : 480;
  var hideT = {};
  function parts(w) { return { tile: w.querySelector('[data-menu]'), menu: w.querySelector('.game-menu') }; }
  function items(w) { return Array.prototype.slice.call(w.querySelectorAll('.gm-item')); }
  function sync() {
    var any = wraps.some(function (w) { return w.classList.contains('is-open'); });
    document.body.classList.toggle('gm-open', any);
    if (games) games.classList.toggle('has-open', any);
  }

  function burst(w, tile, ev) {
    var tr = tile.getBoundingClientRect(), wr = w.getBoundingClientRect();
    var pointer = ev && ev.detail > 0 && ev.clientX;
    var cx = pointer ? ev.clientX : tr.left + tr.width * .5, cy = pointer ? ev.clientY : tr.top + tr.height * .5;
    var cs = getComputedStyle(w), c1 = cs.getPropertyValue('--ac').trim() || '#f7de93', c2 = cs.getPropertyValue('--ac2').trim() || '#d9b155';
    ['gm-flood', 'gm-ring'].forEach(function (cls) {
      var r = document.createElement('span'); r.className = cls;
      r.style.setProperty('--x', (cx - tr.left) + 'px'); r.style.setProperty('--y', (cy - tr.top) + 'px');
      tile.appendChild(r); r.addEventListener('animationend', function () { r.remove(); });
    });
    for (var i = 0, n = lite ? 8 : 20; i < n; i++) {
      var p = document.createElement('i'), a = Math.random() * 6.283, d = 50 + Math.random() * 90, star = i % 3 === 0;
      p.className = 'gm-spark' + (star ? ' st' : '');
      p.style.setProperty('--x', (cx - wr.left) + 'px'); p.style.setProperty('--y', (cy - wr.top) + 'px');
      p.style.setProperty('--s', (star ? 9 + Math.random() * 7 : 3 + Math.random() * 4).toFixed(1) + 'px');
      p.style.setProperty('--c', i % 2 ? c1 : c2);
      p.style.setProperty('--dx', (Math.cos(a) * d).toFixed(0) + 'px'); p.style.setProperty('--dy', (Math.sin(a) * d - 20).toFixed(0) + 'px');
      p.style.setProperty('--r', ((Math.random() - .5) * 540).toFixed(0) + 'deg');
      w.appendChild(p); p.addEventListener('animationend', function () { this.remove(); });
    }
    if (tile.animate) tile.animate([{ scale: '1' }, { scale: '.94', offset: .22 }, { scale: '1.04', offset: .58 }, { scale: '1' }], { duration: 560, easing: 'cubic-bezier(.34,1.56,.64,1)' });
  }

  function close(w, refocus) {
    var p = parts(w); if (!p.menu || p.menu.hidden || !w.classList.contains('is-open')) return;
    w.classList.remove('is-open', 'is-launch'); p.tile.setAttribute('aria-expanded', 'false');
    items(w).forEach(function (it) { it.classList.remove('is-picked'); });
    if (!reduce) w.classList.add('is-closing');
    sync();
    clearTimeout(hideT[p.menu.id]);
    hideT[p.menu.id] = setTimeout(function () { w.classList.remove('is-closing'); if (!w.classList.contains('is-open')) p.menu.hidden = true; }, HIDE_MS);
    if (refocus) p.tile.focus();
  }

  function open(w, ev) {
    wraps.forEach(function (o) { if (o !== w) close(o); });
    var p = parts(w); if (!p.menu) return;
    clearTimeout(hideT[p.menu.id]);
    w.classList.remove('is-closing');
    p.menu.hidden = false;
    // the little notch (and the 3D pivot) sits under the click point
    var tr = p.tile.getBoundingClientRect(), x = ev && ev.detail > 0 && ev.clientX ? ev.clientX - tr.left : 40;
    p.menu.style.setProperty('--nx', Math.max(28, Math.min(x, tr.width - 28)).toFixed(0) + 'px');
    void p.menu.offsetWidth;                          // let the closed state paint so the transition runs
    w.classList.add('is-open'); p.tile.setAttribute('aria-expanded', 'true');
    sync();
    if (!reduce && ev) burst(w, p.tile, ev);
  }

  wraps.forEach(function (w) {
    var p = parts(w);
    if (!p.tile || !p.menu || !items(w).length) return;   // no destinations for this role: plain link
    p.tile.addEventListener('click', function (ev) {
      if (ev.button > 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      if (w.classList.contains('is-open')) close(w); else { open(w, ev); var f = items(w)[0]; if (f && ev.detail === 0) f.focus(); }
    });
    p.tile.addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowDown') { ev.preventDefault(); open(w, null); var f = items(w)[0]; if (f) f.focus(); }
    });
    p.menu.addEventListener('keydown', function (ev) {
      var list = items(w), i = list.indexOf(document.activeElement);
      if (ev.key === 'ArrowDown') { ev.preventDefault(); list[(i + 1) % list.length].focus(); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); list[(i - 1 + list.length) % list.length].focus(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); close(w, true); }
    });

    // light that follows the pointer: over the panel and over the hovered row
    var raf = 0, last = null;
    p.menu.addEventListener('pointermove', function (ev) {
      last = ev; if (raf) return;
      raf = requestAnimationFrame(function () {
        raf = 0; if (!last) return;
        var mr = p.menu.getBoundingClientRect(); p.menu.style.setProperty('--mx', (last.clientX - mr.left) + 'px');
        var it = last.target.closest ? last.target.closest('.gm-item') : null;
        if (it) { var r = it.getBoundingClientRect(); it.style.setProperty('--ix', (last.clientX - r.left) + 'px'); it.style.setProperty('--iy', (last.clientY - r.top) + 'px'); }
      });
    }, { passive: true });

    // picking a destination: the chosen row lights up, the others fade, then we go
    p.menu.addEventListener('click', function (ev) {
      var a = ev.target.closest ? ev.target.closest('.gm-item') : null;
      if (!a || reduce || ev.button > 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      a.classList.add('is-picked'); w.classList.add('is-launch');
      setTimeout(function () { document.body.classList.add('is-leaving'); location.href = a.href; }, 230);
    });
  });
  document.addEventListener('click', function (ev) { wraps.forEach(function (w) { if (!w.contains(ev.target)) close(w); }); });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') wraps.forEach(function (w) { close(w); }); });
  window.addEventListener('pageshow', function (ev) { if (ev.persisted) wraps.forEach(function (w) { close(w); w.classList.remove('is-launch'); }); });
})();
