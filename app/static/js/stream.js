/* Live video player (dummy feed).
   Models a live stream with a 2-minute rewind window:
     behind = seconds behind the live edge (0 = live).
   Everyone: play / pause. Pausing lets you fall behind live; without a Battle Pass, Play snaps back to live.
   Battle Pass (data-pass="1"): Previous (-10s), Forward (+10s, never past live), click the bar to seek.
   Replace .vp-scene with a real <video> later; this logic stays. */
(function () {
  const WINDOW = 120, STEP = 10;
  const snx = window.snx || {};
  const fmt = (s) => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');

  document.querySelectorAll('[data-player]').forEach((root) => {
    const $ = (s) => root.querySelector(s);
    const pass = root.dataset.pass === '1';
    let ended = root.dataset.ended === '1';
    const btnPlay = $('[data-vp-play]'), big = $('[data-vp-big]'), prev = $('[data-vp-prev]'), next = $('[data-vp-next]');
    const live = $('[data-vp-live]'), time = $('[data-vp-time]'), badge = $('[data-vp-badge-text]');
    const fill = $('[data-vp-fill]'), knob = $('[data-vp-knob]'), track = $('[data-vp-track]');
    const icoPlay = $('[data-vp-ico-play]'), icoPause = $('[data-vp-ico-pause]'), screen = $('[data-vp-screen]');
    let playing = false, behind = 0, tick = 0;

    const say = (m, level) => (snx.toast ? snx.toast(m, { level: level || 'warning' }) : null);
    const needPass = () => {
      say('Previous and forward are Battle Pass features. Get one in the Store.', 'warning');
      if (snx.Sound) snx.Sound.play('err');
    };

    function paint() {
      root.classList.toggle('is-playing', playing);
      root.classList.toggle('is-behind', behind > 0);
      icoPlay.toggleAttribute('hidden', playing); icoPause.toggleAttribute('hidden', !playing);   // SVG elements have no .hidden property
      const label = playing ? 'Pause' : 'Play';
      btnPlay.setAttribute('aria-label', label); big.setAttribute('aria-label', label);
      const atLive = behind === 0;
      live.hidden = atLive || ended;
      time.textContent = ended ? 'ENDED' : atLive ? 'LIVE' : '-' + fmt(behind) + ' behind';
      badge.textContent = ended ? 'ENDED' : atLive ? 'LIVE' : 'DELAYED';
      const pct = ((WINDOW - behind) / WINDOW) * 100;
      fill.style.width = pct + '%'; knob.style.left = pct + '%';
      track.setAttribute('aria-valuenow', String(WINDOW - behind));
      [prev, next].forEach((b) => b.setAttribute('aria-disabled', pass ? 'false' : 'true'));
    }

    function start() {
      if (!pass) behind = 0;                         // no pass: no rewind, playing means live
      playing = true; clearInterval(tick);
      // while paused you fall behind live, while playing the gap stays the same
      tick = setInterval(() => paint(), 1000);
      paint();
    }
    function stop() {
      playing = false; clearInterval(tick);
      if (pass) {                                     // with a pass, the time you paused for is kept
        tick = setInterval(() => { behind = Math.min(WINDOW, behind + 1); paint(); if (behind >= WINDOW) clearInterval(tick); }, 1000);
      }
      paint();
    }
    const toggle = () => { if (snx.Sound) snx.Sound.play('tick'); (playing ? stop : start)(); };
    btnPlay.addEventListener('click', toggle);
    big.addEventListener('click', toggle);
    screen.addEventListener('click', (ev) => { if (ev.target === screen || ev.target.closest('.vp-scene')) toggle(); });

    prev.addEventListener('click', () => { if (!pass) return needPass(); behind = Math.min(WINDOW, behind + STEP); if (snx.Sound) snx.Sound.play('tick'); paint(); });
    next.addEventListener('click', () => { if (!pass) return needPass(); behind = Math.max(0, behind - STEP); if (snx.Sound) snx.Sound.play('tick'); paint(); });
    live.addEventListener('click', () => { behind = 0; if (!playing) start(); else paint(); });

    function seek(x) {
      if (!pass) return needPass();
      const r = track.getBoundingClientRect(), p = Math.min(1, Math.max(0, (x - r.left) / r.width));
      behind = Math.round(WINDOW - p * WINDOW); paint();
    }
    track.addEventListener('click', (ev) => seek(ev.clientX));
    track.addEventListener('keydown', (ev) => {
      if (ev.key === 'ArrowLeft') { ev.preventDefault(); prev.click(); }
      if (ev.key === 'ArrowRight') { ev.preventDefault(); next.click(); }
    });

    // Full screen the whole stage (video + floating panels) so the panels stay pinned on top.
    // Browsers without element fullscreen (iPhone Safari) get a fixed full-viewport fallback.
    //
    // Orientation button (phones): one tap goes full screen and locks LANDSCAPE; another tap while full screen
    // flips between landscape and portrait. Android Chrome can lock the screen, but only while full screen.
    // iPhone Safari cannot lock at all: there the player stays full screen and asks you to turn the phone, so the
    // whole screen (status bar and notifications too) goes landscape. Leaving full screen always undoes the lock.
    const full = $('[data-vp-full]'), orient = $('[data-vp-orient]');
    const stage = root.closest('[data-stage]') || root;
    const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
    const scr = window.screen || {};
    const canLock = !!(scr.orientation && typeof scr.orientation.lock === 'function');
    const isFs = () => fsEl() === stage || stage.classList.contains('is-pseudo');
    let fsWhy = '';
    let want = '';                                   // '' (device decides) | 'landscape' | 'portrait'
    const portraitNow = () => window.innerHeight > window.innerWidth;

    async function lockTo(mode) {
      want = mode;
      stage.classList.remove('is-native-lock');
      let why = canLock ? '' : 'no screen.orientation.lock in this browser';
      if (canLock) {
        try { await scr.orientation.lock(mode); stage.classList.add('is-native-lock'); stage.classList.remove('is-rot'); }
        catch (e) { why = (e && e.name) || 'refused'; }
      }
      if (!stage.classList.contains('is-native-lock')) {
        // No real lock here (iPhone Safari, or a browser that refuses). We no longer fake it by turning only the video
        // with CSS: that leaves the status bar and notifications upright. Real landscape = the whole phone turned, so
        // stay in full screen and ask for that; the stage fills the screen as soon as the phone is sideways.
        stage.classList.remove('is-rot');
        if (mode === 'landscape' && portraitNow() && snx.toast)
          snx.toast('Turn your phone sideways for full landscape. If the screen stays upright, switch off Rotation Lock: '
            + 'iPhone, Control Center \u2192 lock icon. Android, Auto-rotate in the quick settings.', { level: 'info', ttl: 9000 });
      }
      // (why / fsWhy hold the browser's refusal reason, e.g. NotSupportedError; handy in the console when debugging)
      if (why && window.console) console.info('[stream] rotation lock refused:', why, fsWhy);
      stage.dispatchEvent(new CustomEvent('snx:fs', { detail: true }));
      syncOrient();
    }
    function unlock() {
      want = '';
      if (canLock) { try { scr.orientation.unlock(); } catch (e) { /* ignore */ } }
      stage.classList.remove('is-native-lock', 'is-rot');
    }
    function syncOrient() {
      if (!orient) return;
      const landscape = want === 'landscape';
      orient.setAttribute('aria-pressed', String(isFs() && landscape));
      orient.setAttribute('aria-label', !isFs() ? 'Full screen, landscape' : landscape ? 'Switch to portrait' : 'Switch to landscape');
      orient.title = orient.getAttribute('aria-label');
    }
    const sync = () => {
      const on = isFs();
      if (!on) unlock();
      stage.classList.toggle('is-fs', on);
      full.setAttribute('aria-label', on ? 'Exit full screen' : 'Full screen');
      syncOrient();
      stage.dispatchEvent(new CustomEvent('snx:fs', { detail: on }));
    };
    function enter() {
      const req = stage.requestFullscreen || stage.webkitRequestFullscreen;
      if (req) return Promise.resolve(req.call(stage)).catch((e) => { fsWhy = (e && e.name) || 'refused'; stage.classList.add('is-pseudo'); document.documentElement.style.overflow = 'hidden'; });
      stage.classList.add('is-pseudo'); document.documentElement.style.overflow = 'hidden';
      return Promise.resolve();
    }
    function leave() {
      if (fsEl()) { (document.exitFullscreen || document.webkitExitFullscreen).call(document); return; }
      if (stage.classList.contains('is-pseudo')) { stage.classList.remove('is-pseudo'); document.documentElement.style.overflow = ''; sync(); }
    }
    full.addEventListener('click', () => {
      if (isFs()) { leave(); return; }
      const req = stage.requestFullscreen || stage.webkitRequestFullscreen;
      if (req) { Promise.resolve(req.call(stage)).catch(() => {}); }
      else { stage.classList.add('is-pseudo'); document.documentElement.style.overflow = 'hidden'; sync(); }
    });
    // (note: when native full screen is refused the orientation button falls back to the pseudo mode via enter())
    if (orient) orient.addEventListener('click', async () => {
      if (isFs()) { await lockTo(want === 'landscape' ? 'portrait' : 'landscape'); return; }
      await enter();
      sync();                                        // the stage is full screen now (native or pseudo): lock needs that
      await lockTo('landscape');
    });
    // The feed plays by itself: on load, when maximized, when you switch game and when the stream goes live.
    // (It is a sample feed, so there is no browser autoplay block to work around.) Pause still works: it is only
    // started for you at those moments, never forced back on while you watch.
    const autoStart = (snapLive) => { if (ended) return; if (snapLive) behind = 0; if (!playing) start(); else if (snapLive) paint(); };
    stage.addEventListener('snx:fs', (ev) => { if (ev.detail) autoStart(false); });
    document.addEventListener('snx:live', (ev) => { if (ev.detail && ev.detail.on) autoStart(true); });
    document.addEventListener('fullscreenchange', sync);
    document.addEventListener('webkitfullscreenchange', sync);
    window.addEventListener('orientationchange', () => { setTimeout(() => { stage.dispatchEvent(new CustomEvent('snx:fs', { detail: true })); }, 250); });
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && stage.classList.contains('is-pseudo')) full.click(); });

    // The game strip switches game without reloading the page: new names, new ENDED / LIVE state.
    root.addEventListener('snx:game', (ev) => {
      const d = ev.detail || {};
      const l = $('.vp-l'), r = $('.vp-r'), t = $('.vp-title');
      if (l && d.left != null) l.textContent = d.left;
      if (r && d.right != null) r.textContent = d.right;
      if (t && d.n != null) t.textContent = 'Game #' + d.n + ' \u00B7 Live match video';
      if (d.ended != null) { ended = !!d.ended; root.dataset.ended = ended ? '1' : '0'; root.classList.toggle('is-ended', ended); }
      behind = 0; paint();
      autoStart(false);
    });

    paint();
    autoStart(false);
  });
})();
