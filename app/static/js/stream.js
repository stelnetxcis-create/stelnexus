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
    const pass = root.dataset.pass === '1', ended = root.dataset.ended === '1';
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
      icoPlay.hidden = playing; icoPause.hidden = !playing;
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

    const full = $('[data-vp-full]');
    full.addEventListener('click', () => {
      if (document.fullscreenElement) { document.exitFullscreen(); return; }
      if (root.requestFullscreen) root.requestFullscreen().catch(() => {});
    });

    paint();
  });
})();
