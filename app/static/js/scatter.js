/* Scatter: the player. The server decides every round (grids, tumbles, orbs, free spins, the money) and sends
   it whole; this file only PLAYS IT BACK: tiles fall, winners burst, orbs fly into the multiplier, the win counts
   up. Nothing here can change an outcome. Tuned to feel good, honest to be fair: a win smaller than the bet is
   shown quietly, because it is not a win. */
(function () {
  "use strict";
  var root = document.getElementById("sx");
  var ART = window.SCATTER_ART;
  if (!root || !ART) return;

  var COLS = 6, ROWS = 5, ORB = 100, SCATTER = 8;
  var $ = function (s, r) { return (r || root).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || root).querySelectorAll(s)); };

  var el = {
    stage: $("[data-stage]"), board: $("[data-board]"), fx: $("[data-fx]"), pops: $("[data-pops]"),
    banner: $("[data-banner]"), big: $("[data-big]"), bigTier: $("[data-big-tier]"), bigAmount: $("[data-big-amount]"),
    bigX: $("[data-big-x]"), win: $("[data-win]"), winLabel: $("[data-win-label]"), winline: $(".sx-winline"),
    balance: $("[data-balance]"), bet: $("[data-bet]"), session: $("[data-session]"),
    spin: $("[data-spin]"), spinLabel: $("[data-spin-label]"), dn: $("[data-bet-dn]"), up: $("[data-bet-up]"),
    turbo: $("[data-turbo]"), auto: $("[data-auto]"), autoLabel: $("[data-auto-label]"), autoMenu: $("[data-automenu]"),
    sound: $("[data-sound]"), free: $("[data-free]"), freeLeft: $("[data-free-left]"), freeTotal: $("[data-free-total]"),
    multBox: $("[data-multbox]"), mult: $("[data-mult]"), recent: $("[data-recent]"), info: $("[data-info]")
  };

  var cfg = {
    bets: (root.dataset.bets || "").split(",").map(Number).filter(Boolean),
    canPlay: root.dataset.canPlay === "1",
    minGap: +root.dataset.minGap || 0,
    url: root.dataset.url || "/scatter/spin"
  };
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrf = csrfMeta ? csrfMeta.content : "";
  // the OS setting, or the site's own "minimal effects" level (html.fx-min, set from the account's effects preference)
  var reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) ||
    document.documentElement.classList.contains("fx-min");

  var state = {
    balance: +root.dataset.startBalance || 0, betIdx: 0, busy: false, turbo: false, skip: false, auto: 0,
    net: 0, sound: true, startedAt: Date.now(), betNow: 0
  };
  try {
    var saved = JSON.parse(localStorage.getItem("sx.prefs") || "{}");
    if (typeof saved.bet === "number" && cfg.bets.indexOf(saved.bet) >= 0) state.betIdx = cfg.bets.indexOf(saved.bet);
    state.turbo = !!saved.turbo;
    state.sound = saved.sound !== false;
  } catch (e) { /* private mode: defaults */ }
  function savePrefs() {
    try { localStorage.setItem("sx.prefs", JSON.stringify({ bet: cfg.bets[state.betIdx], turbo: state.turbo, sound: state.sound })); } catch (e) { /* ignore */ }
  }

  // ---- small helpers ------------------------------------------------------------------------------------
  var money = new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP", minimumFractionDigits: 2 });
  function peso(cents) { return money.format(cents / 100); }
  function speed() { return state.skip ? 0.1 : (state.turbo ? 0.55 : 1) * (reduce ? 0.6 : 1); }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, Math.max(0, ms * speed())); }); }
  function anim(node, kf, o) {
    o = o || {};
    try {
      var a = node.animate(kf, { duration: Math.max((o.duration || 300) * speed(), 1), delay: (o.delay || 0) * speed(),
        easing: o.easing || "ease-out", fill: o.fill || "both" });
      return a.finished.catch(function () { /* cancelled */ });
    } catch (e) { return Promise.resolve(); }
  }
  function rand(a, b) { return a + Math.random() * (b - a); }
  function toast(msg, level) { if (window.snx && window.snx.toast) window.snx.toast(msg, { level: level || "info" }); }
  function bump(node) { node.classList.remove("bump"); void node.offsetWidth; node.classList.add("bump"); }
  function fxScale() { return state.skip ? 0.25 : (reduce ? 0.4 : 1); }

  // ---- sound: synthesized, nothing to download ----------------------------------------------------------
  var Snd = (function () {
    var ctx = null, master = null;
    function ensure() {
      if (!state.sound) return null;
      if (!ctx) {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        try { ctx = new AC(); master = ctx.createGain(); master.gain.value = 0.32; master.connect(ctx.destination); } catch (e) { return null; }
      }
      if (ctx.state === "suspended") ctx.resume();
      return ctx;
    }
    function tone(freq, dur, type, vol, at, slideTo) {
      var c = ensure(); if (!c) return;
      var t = c.currentTime + (at || 0), o = c.createOscillator(), g = c.createGain();
      o.type = type || "sine"; o.frequency.setValueAtTime(freq, t);
      if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t + dur);
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(vol || 0.3, t + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(g); g.connect(master); o.start(t); o.stop(t + dur + 0.03);
    }
    function noise(dur, vol, at, hp) {
      var c = ensure(); if (!c) return;
      var n = Math.floor(c.sampleRate * dur), buf = c.createBuffer(1, n, c.sampleRate), d = buf.getChannelData(0);
      for (var i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / n);
      var s = c.createBufferSource(), g = c.createGain(), f = c.createBiquadFilter();
      f.type = "highpass"; f.frequency.value = hp || 1200; s.buffer = buf;
      g.gain.value = vol || 0.2; s.connect(f); f.connect(g); g.connect(master); s.start(c.currentTime + (at || 0));
    }
    var NOTES = [523.25, 659.25, 783.99, 1046.5, 1318.5, 1567.98];
    return {
      unlock: ensure,
      tick: function () { tone(180, 0.09, "triangle", 0.2, 0, 90); },
      land: function (v) { tone(120 + (v || 0) * 6, 0.1, "sine", 0.12, 0, 60); },
      pop: function (step) { var f = 380 * Math.pow(1.09, Math.min(step, 14)); tone(f, 0.16, "sine", 0.24, 0, f * 2.1); noise(0.06, 0.1, 0, 3000); },
      scatter: function (n) { var f = 660 * Math.pow(1.122, n); tone(f, 0.7, "sine", 0.3); tone(f * 2.01, 0.5, "sine", 0.12, 0.02); tone(f * 3.02, 0.3, "triangle", 0.06, 0.04); },
      orb: function (n) { var f = 500 * Math.pow(1.1, n); tone(f, 0.18, "square", 0.08, 0, f * 2); tone(f * 1.5, 0.22, "sine", 0.18, 0.04); },
      tickUp: function () { tone(1400 + Math.random() * 300, 0.04, "square", 0.05); },
      small: function () { tone(520, 0.12, "sine", 0.14); tone(660, 0.14, "sine", 0.12, 0.07); },
      win: function (level) {
        var n = Math.min(3 + level, NOTES.length);
        for (var i = 0; i < n; i++) { tone(NOTES[i], 0.32, "triangle", 0.2, i * 0.075); tone(NOTES[i] * 2, 0.22, "sine", 0.07, i * 0.075); }
      },
      bonus: function () {
        [523.25, 659.25, 783.99, 1046.5].forEach(function (f, i) { tone(f, 0.9, "sawtooth", 0.09, i * 0.09); tone(f / 2, 1.1, "sine", 0.16, i * 0.09); });
        tone(1568, 1.2, "sine", 0.18, 0.4);
      },
      big: function () {
        [392, 523.25, 659.25, 783.99, 1046.5].forEach(function (f, i) { tone(f, 0.7, "triangle", 0.2, i * 0.11); tone(f * 2, 0.5, "sine", 0.08, i * 0.11); });
        noise(0.5, 0.12, 0.1, 5000);
      },
      stamp: function () { tone(90, 0.35, "sine", 0.4, 0, 40); noise(0.12, 0.18, 0, 800); },
      error: function () { tone(180, 0.25, "sawtooth", 0.12, 0, 110); }
    };
  })();

  // ---- the effects canvas -------------------------------------------------------------------------------
  var fx = { ctx: el.fx.getContext("2d"), w: 0, h: 0, dpr: 1, parts: [], raf: 0, last: 0 };
  function fxResize() {
    var r = el.board.getBoundingClientRect();
    fx.dpr = Math.min(window.devicePixelRatio || 1, 2);
    fx.w = r.width; fx.h = r.height;
    el.fx.width = Math.round(r.width * fx.dpr); el.fx.height = Math.round(r.height * fx.dpr);
  }
  function spawn(p) { fx.parts.push(p); if (!fx.raf) { fx.last = performance.now(); fx.raf = requestAnimationFrame(fxStep); } }
  function burst(x, y, color, n, power) {
    n = Math.round(n * fxScale());
    for (var i = 0; i < n; i++) {
      var a = rand(0, Math.PI * 2), s = rand(0.4, 1) * (power || 260);
      spawn({ t: "spark", x: x, y: y, vx: Math.cos(a) * s, vy: Math.sin(a) * s - 60, g: 520, life: 0, max: rand(0.45, 0.95), size: rand(1.6, 3.6), color: color });
    }
    for (var j = 0; j < Math.round(n / 4); j++) {
      spawn({ t: "star", x: x, y: y, vx: rand(-140, 140), vy: rand(-220, -40), g: 260, life: 0, max: rand(0.6, 1.1), size: rand(5, 11), color: "#fff6c8", rot: rand(0, 6) });
    }
  }
  function ring(x, y, color, size) {
    spawn({ t: "ring", x: x, y: y, vx: 0, vy: 0, g: 0, life: 0, max: 0.55, size: size || 90, color: color });
  }
  function coins(n, power) {
    n = Math.round(n * fxScale());
    for (var i = 0; i < n; i++) {
      spawn({ t: "coin", x: rand(0, fx.w), y: rand(-fx.h * 0.5, -10), vx: rand(-40, 40), vy: rand(40, 220) * (power || 1), g: 700, life: 0,
        max: rand(1.6, 2.6), size: rand(9, 17), color: "#f4c542", rot: rand(0, 6), spin: rand(4, 11) });
    }
  }
  function fxStep(now) {
    var dt = Math.min((now - fx.last) / 1000, 0.05); fx.last = now;
    var c = fx.ctx;
    c.setTransform(fx.dpr, 0, 0, fx.dpr, 0, 0);
    c.clearRect(0, 0, fx.w, fx.h);
    var alive = [];
    for (var i = 0; i < fx.parts.length; i++) {
      var p = fx.parts[i]; p.life += dt;
      if (p.life >= p.max) continue;
      var k = p.life / p.max;
      p.vy += p.g * dt; p.x += p.vx * dt; p.y += p.vy * dt;
      c.globalAlpha = Math.max(0, 1 - k * k);
      if (p.t === "spark") {
        c.globalCompositeOperation = "lighter"; c.strokeStyle = p.color; c.lineWidth = p.size * (1 - k * 0.6); c.lineCap = "round";
        c.beginPath(); c.moveTo(p.x, p.y); c.lineTo(p.x - p.vx * 0.035, p.y - p.vy * 0.035); c.stroke();
      } else if (p.t === "ring") {
        c.globalCompositeOperation = "lighter"; c.strokeStyle = p.color; c.lineWidth = 5 * (1 - k);
        c.beginPath(); c.arc(p.x, p.y, p.size * (0.2 + k * 0.9), 0, 6.283); c.stroke();
      } else if (p.t === "star") {
        c.globalCompositeOperation = "lighter"; c.fillStyle = p.color; c.save(); c.translate(p.x, p.y); c.rotate(p.rot + p.life * 3);
        var s = p.size * (1 - k * 0.5); c.beginPath();
        c.moveTo(0, -s); c.lineTo(s * 0.28, -s * 0.28); c.lineTo(s, 0); c.lineTo(s * 0.28, s * 0.28);
        c.lineTo(0, s); c.lineTo(-s * 0.28, s * 0.28); c.lineTo(-s, 0); c.lineTo(-s * 0.28, -s * 0.28); c.closePath(); c.fill(); c.restore();
      } else if (p.t === "coin") {
        c.globalCompositeOperation = "source-over"; c.save(); c.translate(p.x, p.y);
        var sx = Math.cos(p.rot + p.life * p.spin);
        c.scale(Math.max(Math.abs(sx), 0.12), 1);
        var gr = c.createLinearGradient(0, -p.size, 0, p.size); gr.addColorStop(0, "#fff2b0"); gr.addColorStop(0.5, "#f4c542"); gr.addColorStop(1, "#a8701a");
        c.fillStyle = gr; c.beginPath(); c.arc(0, 0, p.size, 0, 6.283); c.fill();
        c.strokeStyle = "rgba(120,70,10,.7)"; c.lineWidth = 1.5; c.beginPath(); c.arc(0, 0, p.size * 0.66, 0, 6.283); c.stroke(); c.restore();
        if (p.y > fx.h + 30) continue;
      }
      alive.push(p);
    }
    c.globalAlpha = 1; c.globalCompositeOperation = "source-over";
    fx.parts = alive;
    if (alive.length) fx.raf = requestAnimationFrame(fxStep);
    else { fx.raf = 0; c.clearRect(0, 0, fx.w, fx.h); }
  }

  // ---- the board ----------------------------------------------------------------------------------------
  var tiles = [], cur = [];          // tiles[c][r] DOM nodes; cur[c][r] the cell values
  var cw = 0, ch = 0;
  function measure() { var r = el.board.getBoundingClientRect(); cw = r.width / COLS; ch = r.height / ROWS; fxResize(); }
  function tileHTML(v) {
    if (v >= ORB) {
      var val = v - ORB, tier = ART.orbTier(val);
      return '<div class="sx-t is-orb tier-' + tier + '"><svg viewBox="0 0 100 100"><use href="#sx-orb-' + tier + '"/></svg><span class="sx-orbv">x' + val + "</span></div>";
    }
    return '<div class="sx-t' + (v === SCATTER ? " is-scatter" : "") + '"><svg viewBox="0 0 100 100"><use href="#sx-' + ART.names[v] + '"/></svg></div>';
  }
  function makeTile(v, c, r) {
    var t = document.createElement("div");
    t.className = "sx-tile"; t.style.setProperty("--c", c); t.style.setProperty("--r", r);
    t.innerHTML = tileHTML(v); t.dataset.v = v;
    el.board.insertBefore(t, el.fx);
    return t;
  }
  function emptyGrid() { var g = []; for (var c = 0; c < COLS; c++) { g.push([]); for (var r = 0; r < ROWS; r++) g[c].push(null); } return g; }
  function center(c, r) { return { x: (c + 0.5) * cw, y: (r + 0.5) * ch }; }
  function colorOf(v) { return v >= ORB ? "#c9a0ff" : ART.colors[v] || "#fff"; }

  // a pretty resting board for before the first spin; decorative only (it is never sent anywhere)
  function idleBoard() {
    tiles = emptyGrid(); cur = emptyGrid();
    for (var c = 0; c < COLS; c++) for (var r = 0; r < ROWS; r++) {
      var v = Math.floor(Math.random() * 8); cur[c][r] = v; tiles[c][r] = makeTile(v, c, r);
    }
  }

  function exitTiles() {
    var jobs = [];
    for (var c = 0; c < COLS; c++) for (var r = 0; r < ROWS; r++) {
      var t = tiles[c] && tiles[c][r];
      if (!t) continue;
      jobs.push(anim(t, [{ transform: "translateY(0)", opacity: 1 }, { transform: "translateY(" + (ROWS * ch + 40) + "px)", opacity: 0 }],
        { duration: 300, delay: c * 25 + (ROWS - r) * 12, easing: "cubic-bezier(.5,0,.9,.4)" }).then((function (n) { return function () { n.remove(); }; })(t)));
    }
    tiles = emptyGrid();
    return Promise.all(jobs);
  }

  var landed = { scatter: 0 };
  function dealGrid(grid) {
    cur = grid.map(function (col) { return col.slice(); });
    tiles = emptyGrid(); landed.scatter = 0;
    var jobs = [], sc = [];
    for (var c = 0; c < COLS; c++) for (var r = ROWS - 1; r >= 0; r--) {
      var v = grid[c][r], t = makeTile(v, c, r); tiles[c][r] = t;
      var delay = c * 70 + (ROWS - 1 - r) * 28, dur = 380 + (r + 1) * 40;
      jobs.push(anim(t, [{ transform: "translateY(" + (-(r + 2) * ch - 40) + "px)" }, { transform: "translateY(0)" }],
        { duration: dur, delay: delay, easing: "cubic-bezier(.3,.75,.4,1.08)", fill: "backwards" }));
      if (v === SCATTER || v >= ORB) sc.push({ t: delay + dur, tile: t, v: v });
    }
    sc.sort(function (a, b) { return a.t - b.t; });
    var si = 0, oi = 0;
    sc.forEach(function (s) {
      setTimeout(function () {
        s.tile.firstChild.classList.add("glint");
        if (s.v === SCATTER) { Snd.scatter(si++); } else { Snd.orb(oi++); }
        if (s.v === SCATTER) { var p = center(+s.tile.style.getPropertyValue("--c"), +s.tile.style.getPropertyValue("--r")); ring(p.x, p.y, "#c9a0ff", 70); }
      }, s.t * speed());
    });
    setTimeout(function () { Snd.land(0); }, 380 * speed());
    return Promise.all(jobs);
  }

  // ---- numbers and banners ------------------------------------------------------------------------------
  function setBalance(v) { state.balance = v; el.balance.textContent = peso(v); }
  function setSession() {
    el.session.textContent = (state.net > 0 ? "+" : state.net < 0 ? "−" : "") + peso(Math.abs(state.net));
    el.session.className = state.net > 0 ? "up" : (state.net < 0 ? "down" : "");
  }
  function roll(node, from, to, ms, fmt, tickSound) {
    return new Promise(function (resolve) {
      ms = Math.max(ms * speed(), 30);
      var t0 = performance.now(), lastTick = 0;
      (function step(now) {
        var k = Math.min((now - t0) / ms, 1), e = 1 - Math.pow(1 - k, 3);
        node.textContent = fmt(Math.round(from + (to - from) * e));
        if (tickSound && now - lastTick > 70 && k < 1) { Snd.tickUp(); lastTick = now; }
        if (k < 1) requestAnimationFrame(step); else resolve();
      })(t0);
    });
  }
  function showWin(cents, label, quiet) {
    el.winLabel.textContent = label; el.win.textContent = cents ? peso(cents) : "\u00a0";
    el.winline.classList.toggle("quiet", !!quiet);
    el.winline.classList.remove("pulse"); void el.winline.offsetWidth; if (cents) el.winline.classList.add("pulse");
  }
  async function banner(html, ms) {
    el.banner.innerHTML = html; el.banner.hidden = false;
    el.banner.classList.remove("is-out"); el.banner.classList.add("is-in");
    await wait(ms);
    el.banner.classList.remove("is-in"); el.banner.classList.add("is-out");
    await wait(300);
    el.banner.hidden = true; el.banner.classList.remove("is-out");
  }
  function pop(x, y, html, quiet) {
    var p = document.createElement("div"); p.className = "sx-pop" + (quiet ? " quiet" : "");
    p.style.left = x + "px"; p.style.top = y + "px"; p.innerHTML = html;
    el.pops.appendChild(p); setTimeout(function () { p.remove(); }, 1300);
  }
  function shake(big) {
    if (reduce) return;
    el.board.classList.remove("shake", "shake-big"); void el.board.offsetWidth; el.board.classList.add(big ? "shake-big" : "shake");
  }

  // ---- one tumble ---------------------------------------------------------------------------------------
  function cents(x100) { return Math.floor(state.betNow * x100 / 100); }

  async function tumble(t, step, shown) {
    var winning = {}, clusters = t.wins;
    clusters.forEach(function (w) { w.cells.forEach(function (p) { winning[p[0] + "," + p[1]] = w.sym; }); });
    var cel = state.lastCelebrate;
    // 1. light up the winners, quiet everything else
    for (var c = 0; c < COLS; c++) for (var r = 0; r < ROWS; r++) {
      var tl = tiles[c][r], key = c + "," + r;
      if (winning[key] !== undefined) { tl.firstChild.classList.add("win"); tl.firstChild.style.setProperty("--glow", colorOf(winning[key])); }
      else if (cur[c][r] < ORB && cur[c][r] !== SCATTER) tl.firstChild.classList.add("dim");
    }
    clusters.forEach(function (w) {
      var sx = 0, sy = 0; w.cells.forEach(function (p) { var q = center(p[0], p[1]); sx += q.x; sy += q.y; });
      var amt = cents(w.pay), quiet = !cel;
      pop(sx / w.cells.length, sy / w.cells.length, "+" + peso(amt) + "<small>" + w.count + " " + ART.names[w.sym] + "</small>", quiet);
    });
    var stepWin = t.win;
    shown.raw += stepWin;
    var from = cents(shown.raw - stepWin), to = cents(shown.raw);
    (cel ? Snd.win(Math.min(step, 3)) : Snd.small());
    roll(el.win, from, to, 450, peso, false); showWin(from, "Win", !cel);
    if (cel && clusters.some(function (w) { return w.count >= 12; })) shake(true); else if (cel && stepWin >= 100) shake(false);
    await wait(cel ? 580 : 440);

    // 2. burst
    Snd.pop(step);
    var jobs = [];
    clusters.forEach(function (w) {
      w.cells.forEach(function (p, i) {
        var tl = tiles[p[0]][p[1]], q = center(p[0], p[1]);
        burst(q.x, q.y, colorOf(w.sym), cel ? 9 : 4, 250);
        if (cel && i % 3 === 0) ring(q.x, q.y, colorOf(w.sym), 58);
        jobs.push(anim(tl, [{ transform: "scale(1)", opacity: 1 }, { transform: "scale(1.4)", opacity: 1, offset: 0.35 }, { transform: "scale(.2)", opacity: 0 }],
          { duration: 300, delay: i * 14, easing: "ease-in" }).then(function () { tl.remove(); }));
      });
    });
    await Promise.all(jobs);

    // 3. everyone else falls, new symbols arrive
    var fall = [], newCur = [], newTiles = emptyGrid();
    for (var c2 = 0; c2 < COLS; c2++) {
      var keep = [], keepTiles = [];
      for (var r2 = 0; r2 < ROWS; r2++) {
        if (winning[c2 + "," + r2] === undefined) { keep.push(cur[c2][r2]); keepTiles.push({ t: tiles[c2][r2], from: r2 }); }
      }
      var fresh = t.fill[c2], k = fresh.length, col = fresh.concat(keep);
      newCur.push(col);
      keepTiles.forEach(function (kt, i) {
        var nr = k + i; kt.t.style.setProperty("--r", nr); newTiles[c2][nr] = kt.t;
        var d = nr - kt.from;
        if (d > 0) fall.push(anim(kt.t, [{ transform: "translateY(" + (-d * ch) + "px)" }, { transform: "translateY(0)" }],
          { duration: 240 + d * 70, delay: c2 * 25, easing: "cubic-bezier(.3,.8,.4,1.06)", fill: "backwards" }));
      });
      fresh.forEach(function (v, i) {
        var nt = makeTile(v, c2, i); newTiles[c2][i] = nt;
        fall.push(anim(nt, [{ transform: "translateY(" + (-(k + 1) * ch - 30) + "px)" }, { transform: "translateY(0)" }],
          { duration: 300 + (k - i) * 60, delay: c2 * 25 + (k - 1 - i) * 30, easing: "cubic-bezier(.3,.8,.4,1.06)", fill: "backwards" }));
        if (v === SCATTER || v >= ORB) {
          setTimeout(function () { nt.firstChild.classList.add("glint"); if (v === SCATTER) { Snd.scatter(landed.scatter++); } else { Snd.orb(0); } }, (300 + (k - i) * 60 + c2 * 25 + (k - 1 - i) * 30) * speed());
        }
      });
    }
    cur = newCur; tiles = newTiles;
    Snd.land(step);
    await Promise.all(fall);
    for (var cc = 0; cc < COLS; cc++) for (var rr = 0; rr < ROWS; rr++) if (tiles[cc][rr]) tiles[cc][rr].firstChild.classList.remove("dim");
  }

  // ---- orbs fly into the multiplier ---------------------------------------------------------------------
  function showMultBox(text) { el.multBox.hidden = false; el.mult.textContent = text; bump(el.multBox); }
  async function collectOrbs(spin, free) {
    var orbs = spin.orbs.slice().sort(function (a, b) { return a.c - b.c || a.r - b.r; });
    var base = free ? Math.max(spin.mult - spin.orb_sum, 0) : 0, run = base;
    el.multBox.hidden = false; if (!free) el.mult.textContent = "x0";
    var target = el.multBox.getBoundingClientRect();        // measure AFTER it is shown: a hidden box has no position
    for (var i = 0; i < orbs.length; i++) {
      var o = orbs[i], tl = tiles[o.c][o.r]; if (!tl) continue;
      var from = tl.getBoundingClientRect(), clone = tl.cloneNode(true);
      clone.style.cssText = "position:fixed;left:" + from.left + "px;top:" + from.top + "px;width:" + from.width + "px;height:" + from.height + "px;z-index:60;pointer-events:none;margin:0;--c:0;--r:0";
      document.body.appendChild(clone);
      tl.firstChild.style.opacity = "0.25";
      var dx = target.left + target.width / 2 - (from.left + from.width / 2), dy = target.top + target.height / 2 - (from.top + from.height / 2);
      var q = center(o.c, o.r); burst(q.x, q.y, "#c9a0ff", 10, 200); ring(q.x, q.y, "#ffd75e", 60);
      Snd.orb(i + 3);
      var done = anim(clone, [{ transform: "translate(0,0) scale(1)", opacity: 1 }, { transform: "translate(" + dx * 0.5 + "px," + (dy * 0.5 - 40) + "px) scale(1.25)", opacity: 1, offset: 0.45 },
        { transform: "translate(" + dx + "px," + dy + "px) scale(.35)", opacity: 0.9 }], { duration: 520, easing: "cubic-bezier(.5,0,.3,1)" });
      await done; clone.remove();
      run += o.v; el.mult.textContent = "x" + Math.max(run, 1); bump(el.multBox);
      await wait(50);
    }
    await wait(180);
  }

  // ---- one spin (base or free) --------------------------------------------------------------------------
  async function playSpin(spin, info) {
    state.lastCelebrate = true;
    if (info.again) await exitTiles();          // the base spin's old board was already cleared while the server worked
    await dealGrid(spin.grid);
    await wait(120);
    var shown = { raw: 0 };
    // would the whole spin count as a real win? decide once, up front, so the show does not flip half way
    var finalCents = cents(spin.win), celebrate = finalCents >= state.betNow;
    state.lastCelebrate = celebrate;
    for (var i = 0; i < spin.tumbles.length; i++) { await tumble(spin.tumbles[i], i, shown); await wait(70); }

    var spinCents = cents(spin.win), rawCents = cents(spin.raw);
    if (spin.raw > 0) {
      if (spin.collected) {
        await collectOrbs(spin, info.free);
        if (spin.factor > 1) {
          var mid = { x: el.board.clientWidth / 2, y: el.board.clientHeight / 2 };
          Snd.stamp(); shake(spin.factor >= 10);
          pop(mid.x, mid.y, "×" + spin.factor, false);
          burst(mid.x, mid.y, "#ffd75e", 30, 380); ring(mid.x, mid.y, "#ffe9a6", 160);
          await roll(el.win, rawCents, spinCents, 600, peso, true);
          await wait(200);
        }
      }
      showWin(spinCents, spinCents >= state.betNow ? "Win" : "Returned", spinCents < state.betNow);
      el.win.textContent = peso(spinCents);
    } else if (!info.free) {
      showWin(0, "No win", true);
    }
    return spinCents;
  }

  // ---- big wins -----------------------------------------------------------------------------------------
  function tierOf(x, capped) {
    if (capped) return { name: "MAX WIN", ms: 5200 };
    if (x >= 100) return { name: "EPIC WIN", ms: 4800 };
    if (x >= 40) return { name: "MEGA WIN", ms: 3800 };
    if (x >= 15) return { name: "BIG WIN", ms: 3000 };
    return null;
  }
  async function bigWin(totalCents, x100, capped, bet) {
    var x = x100 / 100, tier = tierOf(x, capped);
    if (!tier) return false;
    el.bigTier.textContent = tier.name; el.bigAmount.textContent = peso(0); el.bigX.textContent = (Math.round(x * 100) / 100) + "x your bet";
    el.big.hidden = false; Snd.big();
    var skipped = false, onClick = function () { skipped = true; };
    el.big.addEventListener("click", onClick);
    var stop = false;
    (function shower() { if (stop) return; coins(18, 1); setTimeout(shower, 280); })();
    await new Promise(function (resolve) {
      var t0 = performance.now(), dur = state.skip ? 400 : tier.ms, last = 0;
      (function step(now) {
        var k = skipped ? 1 : Math.min((now - t0) / dur, 1), e = 1 - Math.pow(1 - k, 3);
        el.bigAmount.textContent = peso(Math.round(totalCents * e));
        if (now - last > 75 && k < 1) { Snd.tickUp(); last = now; }
        if (k < 1) requestAnimationFrame(step); else resolve();
      })(t0);
    });
    stop = true;
    await new Promise(function (r) { var done = false, fin = function () { if (!done) { done = true; r(); } }; el.big.addEventListener("click", fin, { once: true }); setTimeout(fin, state.skip ? 200 : 1600); });
    el.big.removeEventListener("click", onClick);
    el.big.hidden = true;
    return true;
  }

  // ---- a whole round ------------------------------------------------------------------------------------
  function setBonusUI(on, total, left) {
    el.stage.classList.toggle("is-free", on);
    el.free.hidden = !on;
    if (on) { el.freeTotal.textContent = total; el.freeLeft.textContent = left; }
  }
  async function playRound(res, bet, balanceBefore) {
    var spins = res.result.spins, bonus = res.result.bonus, total = 0, freeTotal = 0, freeDone = 0;
    state.betNow = bet;
    el.multBox.hidden = true;
    var base = spins[0];
    var baseCents = await playSpin(base, { free: false });
    total += baseCents;

    if (bonus) {
      var scatterPay = base.scatter_pay || 0, scatterCents = cents(scatterPay);
      Snd.bonus();
      await banner("<div><h4>Free spins</h4><p><b>" + bonus.awarded + "</b> free spins" + (scatterCents ? " · scatter pays <b>" + peso(scatterCents) + "</b>" : "") + "</p></div>", 1900);
      total += scatterCents; freeTotal = bonus.awarded;
      if (scatterCents) { showWin(total, "Win", false); }
      setBonusUI(true, freeTotal, freeTotal);
      showMultBox("x1");
      var bonusCents = 0;
      for (var i = 1; i < spins.length; i++) {
        var sp = spins[i];
        if (sp.retrigger) freeTotal += sp.retrigger;
        el.freeTotal.textContent = freeTotal;
        el.freeLeft.textContent = Math.max(freeTotal - freeDone, 0); bump(el.free);
        await wait(380);
        var got = await playSpin(sp, { free: true, again: true });
        freeDone += 1; bonusCents += got; total += got;
        el.freeLeft.textContent = Math.max(freeTotal - freeDone, 0);
        if (sp.mult > 0) el.mult.textContent = "x" + Math.max(sp.mult, 1);
        if (sp.retrigger) { Snd.bonus(); await banner("<div><h4>+" + sp.retrigger + " free spins</h4></div>", 1500); }
        showWin(total, "Bonus total", false);
        await wait(300);
      }
      await banner("<div><h4>Bonus complete</h4><p>Total <b>" + peso(res.win) + "</b></p></div>", 1800);
      setBonusUI(false); el.multBox.hidden = true;
    }
    // the server's figure is the truth; the sum above is only the show
    total = res.win;
    var x100 = res.result.win, celebrate = total >= bet;
    if (total > 0) {
      showWin(total, celebrate ? "Round win" : "Returned", !celebrate);
      if (celebrate) {
        Snd.win(Math.min(Math.floor(x100 / 500), 3));
        var mid = { x: el.board.clientWidth / 2, y: el.board.clientHeight / 2 };
        coins(Math.min(10 + Math.round(x100 / 100), 40), 1);
        burst(mid.x, mid.y, "#ffd75e", 24, 360);
      }
    }
    var showed = await bigWin(total, x100, res.result.capped, bet);
    var from = balanceBefore - bet;
    if (total > 0) await roll(el.balance, from, from + total, showed ? 500 : 900, peso, false);
    setBalance(res.balance);
    return total;
  }

  // ---- talking to the server ----------------------------------------------------------------------------
  function uid() { return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2); }
  async function post(bet) {
    var key = uid(), lastErr = null;
    for (var attempt = 0; attempt < 3; attempt++) {
      try {
        var r = await fetch(cfg.url, {
          method: "POST", credentials: "same-origin",
          headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf },
          body: new URLSearchParams({ bet: String(bet), key: key, csrf_token: csrf })
        });
        var data = null;
        try { data = await r.json(); } catch (e) { data = null; }
        if (r.ok && data && data.ok) return data;
        if (r.status >= 500 || !data) { lastErr = new Error("Server hiccup"); await new Promise(function (s) { setTimeout(s, 900); }); continue; }
        var err = new Error(data.detail || "That spin could not be played."); err.fatal = true; throw err;
      } catch (e) {
        if (e.fatal) throw e;
        lastErr = e; await new Promise(function (s) { setTimeout(s, 900); });
      }
    }
    throw lastErr || new Error("No connection.");
  }

  function setBusy(b) {
    state.busy = b;
    var stopping = state.auto > 0;
    el.spin.classList.toggle("is-busy", b && !stopping);
    el.spin.classList.toggle("is-stop", stopping);
    el.spinLabel.textContent = stopping ? "Stop" : (b ? "Skip" : "Spin");
    el.spin.disabled = !cfg.canPlay;
    el.dn.disabled = b || state.betIdx <= 0; el.up.disabled = b || state.betIdx >= cfg.bets.length - 1;
  }
  function addRecent(bet, win, free) {
    var li = document.createElement("li");
    li.className = "fresh " + (win >= bet ? "is-win" : (win ? "is-part" : ""));
    li.innerHTML = "<span>" + (bet / 100).toFixed(2) + "</span><b>" + (win ? "+" + (win / 100).toFixed(2) : "—") + "</b>" + (free ? "<i>" + free + " free</i>" : "");
    var empty = $(".sx-empty", el.recent); if (empty) empty.remove();
    el.recent.insertBefore(li, el.recent.firstChild);
    while (el.recent.children.length > 10) el.recent.lastChild.remove();
  }

  var lastSpinAt = 0;
  async function spinOnce() {
    if (!cfg.canPlay) return;
    if (state.busy) { state.skip = true; return; }
    var bet = cfg.bets[state.betIdx];
    if (state.balance < bet) { toast("Not enough credit for that bet.", "warning"); Snd.error(); stopAuto(); return; }
    var gap = cfg.minGap * 1000 - (Date.now() - lastSpinAt);
    if (gap > 0) { toast("Easy does it: next spin in " + Math.ceil(gap / 1000) + "s.", "info"); stopAuto(); return; }
    Snd.unlock(); Snd.tick();
    state.skip = false; setBusy(true); state.betNow = bet;
    var before = state.balance;
    setBalance(before - bet); showWin(0, "Good luck", true);
    el.multBox.hidden = true;
    var res;
    try {
      var pending = post(bet);
      pending.catch(function () { /* handled below, once the old board is cleared */ });
      await exitTiles();
      res = await pending;
    } catch (e) {
      setBalance(before); setBusy(false); stopAuto(); Snd.error();
      toast(e.message || "That spin could not be played.", "danger"); showWin(0, "Try again", true);
      if (!tiles[0] || !tiles[0][0]) idleBoard();
      return;
    }
    lastSpinAt = Date.now();
    var won = 0;
    try { won = await playRound(res, bet, before); }
    catch (e) { if (window.console) console.error(e); setBalance(res.balance); won = res.win; }
    state.net += won - bet; setSession();
    addRecent(bet, won, res.result.bonus ? res.result.bonus.spins : 0);
    state.skip = false; setBusy(false);
    if (res.result.bonus || (won > 0 && res.result.win >= 1500)) stopAuto();       // let a big moment land
    if (state.auto > 0) {
      state.auto -= 1; syncAuto();
      if (state.auto > 0) setTimeout(function () { if (!state.busy && state.auto > 0) spinOnce(); }, 650 * (state.turbo ? 0.5 : 1));
      else setBusy(false);
    }
  }

  // ---- controls -----------------------------------------------------------------------------------------
  function showBet() { el.bet.textContent = peso(cfg.bets[state.betIdx]); el.dn.disabled = state.busy || state.betIdx <= 0; el.up.disabled = state.busy || state.betIdx >= cfg.bets.length - 1; }
  function syncAuto() {
    el.auto.setAttribute("aria-pressed", state.auto > 0 ? "true" : "false");
    el.autoLabel.textContent = state.auto > 0 ? "Auto " + state.auto : "Auto";
    el.spinLabel.textContent = state.auto > 0 ? "Stop" : (state.busy ? "Skip" : "Spin");
    el.spin.classList.toggle("is-stop", state.auto > 0);
  }
  function stopAuto() { state.auto = 0; syncAuto(); }
  function syncToggles() {
    el.turbo.setAttribute("aria-pressed", state.turbo ? "true" : "false");
    el.sound.setAttribute("aria-pressed", state.sound ? "true" : "false");
  }

  el.spin.addEventListener("click", function () {
    if (state.auto > 0) { stopAuto(); if (!state.busy) setBusy(false); return; }
    spinOnce();
  });
  el.dn.addEventListener("click", function () { if (state.betIdx > 0) { state.betIdx--; showBet(); savePrefs(); Snd.unlock(); Snd.tick(); } });
  el.up.addEventListener("click", function () { if (state.betIdx < cfg.bets.length - 1) { state.betIdx++; showBet(); savePrefs(); Snd.unlock(); Snd.tick(); } });
  el.turbo.addEventListener("click", function () { state.turbo = !state.turbo; syncToggles(); savePrefs(); });
  el.sound.addEventListener("click", function () { state.sound = !state.sound; syncToggles(); savePrefs(); if (state.sound) { Snd.unlock(); Snd.tick(); } });
  el.auto.addEventListener("click", function (e) {
    e.stopPropagation();
    if (state.auto > 0) { stopAuto(); return; }
    el.autoMenu.hidden = !el.autoMenu.hidden;
  });
  $$("[data-auto-n]").forEach(function (b) {
    b.addEventListener("click", function () {
      el.autoMenu.hidden = true; state.auto = +b.dataset.autoN; syncAuto();
      if (!state.busy) { state.auto -= 0; spinOnce(); }
    });
  });
  document.addEventListener("click", function (e) { if (!el.autoMenu.hidden && !el.autoMenu.contains(e.target)) el.autoMenu.hidden = true; });
  var infoBtn = document.querySelector("[data-sx-info]");
  function openInfo() { el.info.hidden = false; }
  function closeInfo() { el.info.hidden = true; }
  if (infoBtn) infoBtn.addEventListener("click", openInfo);
  $$("[data-info-close]").forEach(function (b) { b.addEventListener("click", closeInfo); });
  el.info.addEventListener("click", function (e) { if (e.target === el.info) closeInfo(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeInfo(); el.autoMenu.hidden = true; return; }
    if ((e.key === " " || e.code === "Space") && !/INPUT|TEXTAREA|SELECT|BUTTON/.test((document.activeElement || {}).tagName || "") && el.info.hidden) {
      e.preventDefault(); if (state.auto > 0) stopAuto(); else spinOnce();
    }
  });
  window.addEventListener("resize", measure);
  if (window.ResizeObserver) new ResizeObserver(measure).observe(el.board);

  // a gentle reality check every 30 minutes
  setInterval(function () {
    var mins = Math.round((Date.now() - state.startedAt) / 60000);
    toast("You have been playing for " + mins + " minutes. This session: " + (state.net >= 0 ? "+" : "−") + peso(Math.abs(state.net)) + ".", "info");
  }, 30 * 60 * 1000);

  // ---- go -----------------------------------------------------------------------------------------------
  measure();
  idleBoard();
  setBalance(state.balance); showBet(); syncToggles(); setSession(); setBusy(false);
  if (!cfg.canPlay) showWin(0, "Look around", true); else showWin(0, "Place your bet", true);
  window.__scatter = { state: state, playRound: playRound, tierOf: tierOf };
})();
