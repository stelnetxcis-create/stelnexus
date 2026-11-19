/* StelNexus ROYALE: toasts, live polling, tile flips, winner reveal, skeleton on navigation.
   No inline scripts (CSP). Everything degrades: without JS the pages simply behave as before. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const csrf = ($('meta[name="csrf-token"]') || {}).content || "";
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const money = (cents) => "\u20B1" + (Number(cents || 0) / 100).toLocaleString("en-PH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const parseUTC = (s) => (s ? new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z") : null);

  /* ------------------------------------------------------------ sound (synthesised, no audio files) */
  const Sound = (() => {
    let ctx = null;
    let on = store.get("snx.sound") !== "off";
    const AC = window.AudioContext || window.webkitAudioContext;
    const ensure = () => {
      if (!AC) return null;
      if (!ctx) ctx = new AC();
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      return ctx;
    };
    const tone = (c, f, at, dur, type, vol) => {
      const o = c.createOscillator(), g = c.createGain(), t = c.currentTime + at;
      o.type = type; o.frequency.setValueAtTime(f, t);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(vol, t + 0.015);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(g); g.connect(c.destination); o.start(t); o.stop(t + dur + 0.05);
    };
    const patterns = {
      open: (c) => { tone(c, 523, 0, .16, "sine", .08); tone(c, 784, .09, .24, "sine", .08); },
      lock: (c) => { tone(c, 440, 0, .1, "square", .035); tone(c, 330, .1, .16, "square", .035); },
      win:  (c) => { [523, 659, 784, 1047].forEach((f, i) => tone(c, f, i * .11, .42, "triangle", .09)); tone(c, 1568, .5, .6, "sine", .05); },
      ok:   (c) => { tone(c, 880, 0, .09, "sine", .07); tone(c, 1175, .07, .12, "sine", .06); },
      err:  (c) => { tone(c, 190, 0, .22, "sawtooth", .05); },
      tick: (c) => { tone(c, 1200, 0, .03, "square", .02); },
    };
    const api = {
      get on() { return on; },
      play(name) { if (!on || !patterns[name]) return; const c = ensure(); if (c) patterns[name](c); },
      toggle() { on = !on; store.set("snx.sound", on ? "on" : "off"); sync(); if (on) api.play("ok"); },
    };
    const sync = () => $$("[data-sound-toggle]").forEach((b) => { b.classList.toggle("is-on", on); b.setAttribute("aria-pressed", String(on)); });
    document.addEventListener("pointerdown", () => ensure(), { once: true });
    document.addEventListener("keydown", () => ensure(), { once: true });
    document.addEventListener("click", (e) => { if (e.target.closest("[data-sound-toggle]")) api.toggle(); });
    document.addEventListener("DOMContentLoaded", sync);
    return api;
  })();

  /* ------------------------------------------------------------ toasts */
  const ICON = { success: "i-check", danger: "i-x", warning: "i-flame", info: "i-bell" };
  function toast(message, opts = {}) {
    const host = $("#toasts");
    if (!host) return null;
    const level = ({ error: "danger" })[opts.level] || opts.level || "info";
    const ttl = opts.ttl || (opts.undo || opts.action ? 8000 : 5600);
    const el = document.createElement("div");
    el.className = "toast t-" + level;
    el.setAttribute("role", level === "danger" ? "alert" : "status");
    el.style.setProperty("--ttl", ttl + "ms");
    const ico = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    ico.setAttribute("class", "ico");
    ico.innerHTML = '<use href="#' + (ICON[level] || "i-bell") + '"/>';
    const badge = document.createElement("span"); badge.className = "toast-ico"; badge.appendChild(ico);
    const msg = document.createElement("div"); msg.className = "toast-msg"; msg.textContent = message;
    if (opts.sub) { const s = document.createElement("small"); s.textContent = opts.sub; msg.appendChild(s); }
    const acts = document.createElement("div"); acts.className = "toast-acts";
    const act = opts.undo || opts.action;
    if (act) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "toast-undo"; b.textContent = act.label || (opts.undo ? "Undo" : "OK");
      b.addEventListener("click", () => {
        b.disabled = true;
        if (typeof act.run === "function") { Promise.resolve(act.run()).finally(close); return; }
        if (act.action) postForm(act.action, act.fields || {});
      });
      acts.appendChild(b);
    }
    const x = document.createElement("button");
    x.type = "button"; x.className = "toast-x"; x.setAttribute("aria-label", "Dismiss");
    x.innerHTML = '<svg class="ico"><use href="#i-x"/></svg>';
    x.addEventListener("click", () => close());
    acts.appendChild(x);
    const bar = document.createElement("div"); bar.className = "toast-time"; bar.innerHTML = "<i></i>";
    el.append(badge, msg, acts, bar);
    host.appendChild(el);
    while (host.children.length > 4) host.firstElementChild.remove();
    let left = ttl, started = Date.now(), timer = setTimeout(close, left);
    const pause = () => { clearTimeout(timer); left -= Date.now() - started; };
    const resume = () => { started = Date.now(); timer = setTimeout(close, Math.max(left, 1200)); };
    el.addEventListener("mouseenter", pause); el.addEventListener("mouseleave", resume);
    el.addEventListener("focusin", pause); el.addEventListener("focusout", resume);
    function close() {
      clearTimeout(timer);
      if (!el.isConnected) return;
      el.classList.add("is-out");
      setTimeout(() => el.remove(), reduce ? 0 : 340);
    }
    if (level === "success") Sound.play("ok"); else if (level === "danger") Sound.play("err");
    return { close };
  }
  function postForm(action, fields) {
    const f = document.createElement("form");
    f.method = "post"; f.action = action; f.hidden = true;
    const add = (n, v) => { const i = document.createElement("input"); i.type = "hidden"; i.name = n; i.value = v; f.appendChild(i); };
    add("csrf_token", csrf);
    Object.keys(fields).forEach((k) => add(k, fields[k]));
    document.body.appendChild(f); f.submit();
  }
  // server flash banners become toasts (the hidden <div data-flash> stays for no-JS / screen readers' noscript)
  document.addEventListener("DOMContentLoaded", () => {
    $$("[data-flash]").forEach((n, i) => {
      let undo = null;
      if (n.dataset.undoAction) {
        let fields = {};
        try { fields = JSON.parse(n.dataset.undoFields || "{}"); } catch (e) { /* ignore */ }
        undo = { action: n.dataset.undoAction, fields };
      }
      setTimeout(() => toast(n.textContent.trim(), { level: n.dataset.level, undo }), 120 + i * 160);
    });
    if ($(".sell")) document.body.classList.add("is-seller");
  });

  /* ------------------------------------------------------------ skeleton while the next page loads */
  const SKEL = '<div class="page-skel" aria-hidden="true"><div class="skel sk-h"></div><div class="sk-row">' +
    '<div class="skel-block"></div><div class="skel-block"></div><div class="skel-block"></div><div class="skel-block"></div></div>' +
    '<div class="sk-wide"><div class="skel-block"></div><div class="skel-block"></div></div></div>';
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const href = a.getAttribute("href") || "";
    if (a.target === "_blank" || a.hasAttribute("download") || href.startsWith("#") || /^(javascript|mailto|tel):/i.test(href)) return;
    if (a.origin !== location.origin || (a.pathname === location.pathname && a.search === location.search)) return;
    setTimeout(() => {
      if (e.defaultPrevented) return;
      const c = $(".content");
      if (c && !$(".page-skel", c)) c.insertAdjacentHTML("beforeend", SKEL);
    }, 220);
  });
  window.addEventListener("pageshow", () => $$(".page-skel").forEach((n) => n.remove()));

  /* ------------------------------------------------------------ polling engine */
  const dots = new Set();
  const setLive = (ok) => dots.forEach((d) => { if (d.isConnected) d.classList.toggle("is-off", !ok); else dots.delete(d); });
  function mountDot(anchor, where) {
    if (!anchor) return;
    const d = document.createElement("span");
    d.className = "live-dot"; d.textContent = "LIVE"; d.title = "Updates automatically";
    d.style.marginLeft = "auto";
    (where || anchor).appendChild(d); dots.add(d);
  }
  /* One poll loop per live widget. Quiet by design:
     - the server sends an ETag; we send it back as If-None-Match and get "304 Not Modified" (no body) while nothing
       changed, so an idle poll is a few header bytes and no DOM work. (Set by hand because the request is no-store.)
     - no timer runs while the tab is hidden; coming back polls at once.
     - `owner` (optional): when that element leaves the page (a region was swapped) this loop ends by itself.
     - errors slow the loop down (up to 6x) instead of hammering a struggling server. */
  function poller(url, onData, every = 5000, owner = null) {
    let etag = "", failures = 0, timer = 0, stopped = false, first = true;
    async function tick() {
      clearTimeout(timer);
      if (stopped) return;
      if (owner && !owner.isConnected) { stop(); return; }
      if (document.hidden) return;                         // no timer while hidden; visibilitychange restarts the loop
      try {
        const headers = { Accept: "application/json" };
        if (etag) headers["If-None-Match"] = etag;
        const r = await fetch(url, { headers, credentials: "same-origin", cache: "no-store" });
        if (r.redirected || r.status === 401 || r.status === 403) { stop(); return; }
        if (r.status === 304) { failures = 0; setLive(true); }
        else {
          if (!r.ok) throw new Error(String(r.status));
          const d = await r.json();
          failures = 0; setLive(true);
          etag = r.headers.get("ETag") || "";
          if (!d.same) { onData(d, first); first = false; }
        }
      } catch (err) { failures++; if (failures > 1) setLive(false); }
      if (!stopped && !document.hidden) timer = setTimeout(tick, every * Math.min(1 + failures, 6));
    }
    const onVisible = () => { if (!document.hidden) tick(); };
    function stop() { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", onVisible); }
    document.addEventListener("visibilitychange", onVisible);
    tick();
    return { stop };
  }
  const typing = () => { const a = document.activeElement; return a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName); };
  /* Refresh the named page boxes (see StelNexus.refreshRegion in app.js) instead of reloading the whole page: the
     scroll position, the video and the chat stay put. Falls back to a real reload when the page has none of those
     boxes or the server could not hand one back (signed out, say). */
  async function applyRegions(names, force, again) {
    const list = names.filter((n) => $('[data-region="' + n + '"]'));
    const api = window.StelNexus;
    if (!list.length || !api || !api.refreshRegion) { location.reload(); return; }
    const results = await Promise.all(list.map((n) => api.refreshRegion(n, { force })));
    if (results.includes("failed")) { location.reload(); return; }
    if (results.includes("skipped") && again) again();      // they started typing meanwhile: offer, do not force
  }
  function softReload(message, names, ms = 1600) {
    const offer = () => toast(message, { level: "info", action: { label: "Refresh", run: () => applyRegions(names, true) }, ttl: 12000 });
    if (typing() || $(".modal.is-open,[data-modal-open],dialog[open]")) { offer(); return; }
    toast(message, { level: "info", ttl: ms + 400 });
    setTimeout(() => applyRegions(names, false, offer), ms);
  }

  /* ------------------------------------------------------------ result reveal (flash, banner, confetti) */
  const SIDE_COLOR = { meron: "#e5384f", wala: "#4aa3ff", draw: "#b79cff" };
  function burst(side) {
    if (reduce) return;
    const cv = document.createElement("canvas"); cv.id = "fx";
    const dpr = Math.min(devicePixelRatio || 1, 2);
    cv.width = innerWidth * dpr; cv.height = innerHeight * dpr;
    document.body.appendChild(cv);
    const g = cv.getContext("2d"); g.scale(dpr, dpr);
    const colors = [SIDE_COLOR[side] || "#d9b155", "#f7de93", "#ffffff", SIDE_COLOR[side] || "#d9b155", "#d9b155"];
    const ps = Array.from({ length: 150 }, () => {
      const a = Math.random() * Math.PI * 2, v = 5 + Math.random() * 9;
      return { x: innerWidth / 2, y: innerHeight * .34, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 5, r: 3 + Math.random() * 4,
        w: Math.random() * 6.28, vw: (Math.random() - .5) * .4, c: colors[(Math.random() * colors.length) | 0] };
    });
    const t0 = performance.now();
    (function frame(now) {
      const t = now - t0;
      g.clearRect(0, 0, innerWidth, innerHeight);
      ps.forEach((p) => {
        p.vx *= .985; p.vy = p.vy * .985 + .24; p.x += p.vx; p.y += p.vy; p.w += p.vw;
        g.save(); g.translate(p.x, p.y); g.rotate(p.w); g.globalAlpha = Math.max(0, 1 - t / 2600);
        g.fillStyle = p.c; g.fillRect(-p.r, -p.r * .45, p.r * 2, p.r * .9); g.restore();
      });
      if (t < 2700) requestAnimationFrame(frame); else cv.remove();
    })(t0);
  }
  function reveal(side, banner) {
    const f = document.createElement("div");
    f.className = "reveal-flash " + side; document.body.appendChild(f);
    setTimeout(() => f.remove(), 1500);
    if (banner) banner.classList.add("is-reveal");
    burst(side); Sound.play("win");
  }
  // winner banner on the fight view: celebrate once, only if the result is fresh.
  // Runs on page load and again whenever the fight panel is refreshed in place (the winner just appeared).
  function initWinner() {
    const b = $(".winner[data-fight]");
    if (!b || b.classList.contains("canceled")) return;
    const at = parseUTC(b.dataset.declared);
    const key = "snx.revealed." + b.dataset.fight;
    let seen = false; try { seen = !!sessionStorage.getItem(key); } catch (e) { /* ignore */ }
    if (at && !seen && Date.now() - at.getTime() < 90000) {
      try { sessionStorage.setItem(key, "1"); } catch (e) { /* ignore */ }
      setTimeout(() => reveal(b.dataset.side, b), 250);
    }
  }
  document.addEventListener("DOMContentLoaded", initWinner);

  /* ------------------------------------------------------------ live game tiles (event page + fight view) */
  const STATE_TEXT = { open: "OPEN", locked: "LOCKED", settled: "SETTLED", void: "VOID", draft: "DRAFT" };
  function tileKey(f) { return f.res ? "res:" + f.res : "st:" + f.st; }
  function paintTile(t, f, hide) {
    t.className = t.className.replace(/\b(st|res)-\S+/g, "").replace(/\s+/g, " ").trim();
    t.classList.add("st-" + f.st);
    if (f.res) t.classList.add("res-" + f.res);
    t.classList.toggle("has-pool", !hide && (f.st === "open" || f.st === "locked"));
    const label = f.res ? f.res.toUpperCase() : (STATE_TEXT[f.st] || f.st.toUpperCase());
    const st = $(".tile-state", t); if (st) st.textContent = label;
    t.title = "Game " + f.n + " \u00B7 " + f.nm + " vs " + f.nw + " \u00B7 " + label;
    const sm = $("small", t); if (sm) sm.textContent = f.nm + " / " + f.nw;
    t.dataset.key = tileKey(f);
  }
  function paintPool(t, f, hide) {
    const bar = $(".tile-pool", t), hot = $(".tile-hot", t);
    if (hide) { if (bar) bar.remove(); if (hot) hot.remove(); return; }
    if (bar && f.pm !== undefined) {
      const total = f.pm + f.pw, m = total ? Math.round(f.pm / total * 100) : 50;
      bar.classList.remove("is-loading");
      $(".m", bar).style.width = m + "%"; $(".w", bar).style.width = (100 - m) + "%";
      bar.style.opacity = total ? "1" : ".35";
      bar.title = total ? "Meron " + m + "% \u00B7 Wala " + (100 - m) + "%" : "No bets yet";
    } else if (bar && f.st !== "open" && f.st !== "locked") bar.classList.remove("is-loading");
    if (hot) {
      const on = !!f.hot;
      hot.hidden = !on;
      if (on) {
        hot.classList.toggle("w", f.hot === "wala");
        $("i", hot).textContent = "HOT " + (f.hot === "wala" ? "W" : "M");
      }
    }
  }
  function flip(t, f, hide) {
    if (reduce) { paintTile(t, f, hide); return; }
    t.classList.add("is-flipping");
    setTimeout(() => paintTile(t, f, hide), 360);
    setTimeout(() => {
      t.classList.remove("is-flipping");
      if (f.res === "meron" || f.res === "wala") {
        const fl = document.createElement("span");
        fl.className = "tile-flash"; fl.style.setProperty("--fc", SIDE_COLOR[f.res]);
        t.appendChild(fl); setTimeout(() => fl.remove(), 1800);
        t.classList.add("flash-" + f.res); setTimeout(() => t.classList.remove("flash-" + f.res), 1800);
      }
    }, 800);
  }
  function initEvent(wrap) {
    const url = "/_live/event/" + wrap.dataset.liveEvent;
    const card = wrap.closest(".card");
    mountDot(card, card && $(".card-head", card));
    let eventSt = null;
    poller(url, (d, first) => {
      const hide = d.hide;
      if (!first && eventSt && d.event.st !== eventSt) toast("Event is now " + d.event.st.toUpperCase(), { level: "info" });
      eventSt = d.event.st;
      let sound = null, active = null;
      d.fights.forEach((f) => {
        const t = $('[data-fight="' + f.id + '"]', wrap);
        if (!t) return;
        const before = t.dataset.key || (function () {
          const r = (t.className.match(/\bres-(\w+)/) || [])[1], s = (t.className.match(/\bst-(\w+)/) || [])[1];
          return r ? "res:" + r : "st:" + s;
        })();
        if (!first && before !== tileKey(f)) {
          flip(t, f, hide);
          sound = f.res ? "win" : f.st === "open" ? "open" : "lock";
          if (t.classList.contains("is-active")) active = f;
          if (f.res && (f.res === "meron" || f.res === "wala")) {
            toast("Game " + f.n + ": " + f.res.toUpperCase() + " wins", { level: "success", sub: f.nm + " vs " + f.nw });
          }
        } else { paintTile(t, f, hide); }
        paintPool(t, f, hide);
      });
      if (sound) Sound.play(sound);
      if (active) {
        const what = active.res ? "was declared " + active.res.toUpperCase() : active.st === "locked" ? "betting is now closed" : active.st === "open" ? "betting is open again" : "changed";
        softReload("Game " + active.n + " " + what + ". Updating this view\u2026", ["event-head", "event-body", "fight-panel"]);
      }
    });
  }

  /* ------------------------------------------------------------ live event cards (lobby) */
  function initLobby(grid) {
    mountDot(grid, $("[data-lobby-filter]"));
    poller("/_live/lobby", (d, first) => {
      let unknown = false;
      d.events.forEach((e) => {
        const c = $('[data-event="' + e.id + '"]', grid);
        if (!c) { unknown = true; return; }
        const prevSt = c.dataset.status;
        const numbers = [e.total, e.open, e.locked, e.done];
        const was = $$(".lobby-nums b", c).map((b) => b.textContent.trim());
        $$(".lobby-nums b", c).forEach((b, i) => { b.textContent = numbers[i]; });
        const bar = $(".lobby-bar i", c); if (bar) { bar.style.setProperty("--w", e.pct + "%"); bar.style.width = e.pct + "%"; }
        const live = $(".lobby-live", c);
        if (e.live && e.st === "open") {
          if (live) live.lastChild.textContent = "Game " + e.live + " up next";
          else { const s = document.createElement("span"); s.className = "lobby-live"; s.innerHTML = "<i></i>Game " + e.live + " up next"; $(".lobby-top", c).appendChild(s); }
        } else if (live) live.remove();
        if (prevSt !== e.st) {
          c.className = c.className.replace(/\bst-\S+/, "st-" + e.st); c.dataset.status = e.st;
          const ls = $(".lobby-status", c); if (ls) ls.innerHTML = "<i></i>" + e.st.toUpperCase();
        }
        const changed = prevSt !== e.st || was.join() !== numbers.map(String).join();
        if (changed && !first && !reduce) { c.classList.remove("is-updated"); void c.offsetWidth; c.classList.add("is-updated"); }
        if (changed && !first && prevSt !== e.st) Sound.play(e.st === "open" ? "open" : "lock");
      });
      if (unknown && !first) softReload("A new event was added.", ["lobby"]);
    }, 5000, grid);
  }

  /* ------------------------------------------------------------ live dashboard KPIs */
  function initDashboard() {
    const kpis = $$("[data-live-kpi]");
    const hasList = () => !!$('[data-region="live-games"]');
    if (!kpis.length && !hasList()) return;
    // read from the page each time: the live-games box is replaced in place when it changes
    const shown = () => $$("[data-live-list] [data-fight]").map((n) => n.dataset.fight + ":" + (n.dataset.st || "")).sort().join();
    poller("/_live/dashboard", (d, first) => {
      kpis.forEach((el) => {
        const key = el.dataset.liveKpi;
        if (d[key] === undefined) return;
        const text = key === "stake" ? money(d[key]) : String(d[key]);
        if (el.textContent.trim() !== text) {
          el.textContent = text;
          if (!first && !reduce) { el.classList.remove("is-ticking"); void el.offsetWidth; el.classList.add("is-ticking"); }
        }
      });
      if (hasList() && !first) {
        const now = d.live.map((r) => r.id + ":" + r.st).sort().join();
        if (now !== shown()) softReload("Games changed. Updating\u2026", ["live-games"]);
      }
    }, 8000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const ev = $("[data-live-event]"); if (ev) initEvent(ev);
    const lb = $("[data-live-lobby]"); if (lb) initLobby(lb);
    initDashboard();
  });

  // a box was refreshed in place: re-attach what was bound to the old HTML
  document.addEventListener("snx:region", (e) => {
    const { name, el } = e.detail || {};
    if (name === "lobby") { const g = $("[data-live-lobby]", el); if (g) initLobby(g); }
    if (name === "fight-panel") initWinner();
  });

  window.snx = { toast, Sound, poller, money, csrf, postForm, burst, reveal, parseUTC, store, reduce };
})();
