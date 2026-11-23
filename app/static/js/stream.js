/* Live video player.
   Two engines behind the same controls:
     * the SAMPLE feed (nothing taken live on Admin Panel -> Streams): a simulated live edge with a 2-minute window;
     * REAL video from the Streams page: HLS / MP4 in our own <video> (hls.js is lazy-loaded only when needed), or a
       sandboxed iframe for YouTube / Facebook / allowed embeds.
   behind = seconds behind the live edge (0 = live).
   Everyone: play / pause. Without a Battle Pass, Play snaps back to live on a live stream.
   Battle Pass (data-pass="1"): Previous (-10s), Forward (+10s, never past live), click the bar to seek.
   A replay (a finished video) has a normal seek bar for everyone. An iframe cannot be controlled from here, so Pass rules
   are limited on embeds (the admin page says so). The page polls /_live/stream, so a switch needs no reload. */
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
    const mediaHost = $('[data-vp-media]'), msg = $('[data-vp-msg]'), mute = $('[data-vp-mute]');
    // ---- real video state (media === null and embed === false means the sample feed)
    let media = null, embed = false, hls = null, cfg = null, isLive = true, rev = '', failures = 0, retryTimer = 0, mediaFixes = 0;
    const LIVE_SLACK = 12;                           // a real HLS edge is a few seconds behind the encoder: that still counts as LIVE
    const DVR = () => Math.max(30, Number((cfg && cfg.dvr_window) || WINDOW));
    const edge = () => { const r = media.seekable; return r && r.length ? r.end(r.length - 1) : (isFinite(media.duration) ? media.duration : 0); };
    const floorT = () => { const r = media.seekable; return r && r.length ? Math.max(r.start(0), edge() - DVR()) : 0; };
    const win = () => (media ? (isLive ? DVR() : (media.duration || 0)) : WINDOW);
    const lag = () => (media ? (isLive ? Math.max(0, Math.round(edge() - media.currentTime)) : 0) : behind);
    const isPlaying = () => (media ? !media.paused && !media.ended : playing);
    const showMsg = (t) => { if (msg) { msg.hidden = !t; msg.textContent = t || ''; } };

    const say = (m, level) => (snx.toast ? snx.toast(m, { level: level || 'warning' }) : null);
    const needPass = () => {
      say('Previous and forward are Battle Pass features. Get one in the Store.', 'warning');
      if (snx.Sound) snx.Sound.play('err');
    };

    function paint() {
      const on = isPlaying(), b = lag();
      root.classList.toggle('is-playing', on);
      icoPlay.toggleAttribute('hidden', on); icoPause.toggleAttribute('hidden', !on);   // SVG elements have no .hidden property
      const label = on ? 'Pause' : 'Play';
      btnPlay.setAttribute('aria-label', label); big.setAttribute('aria-label', label);
      if (embed) {                                   // a frame we cannot control: just say what it is
        root.classList.remove('is-behind');
        badge.textContent = ended ? 'ENDED' : isLive ? 'LIVE' : 'REPLAY';
        time.textContent = '';
        return;
      }
      const replay = !!media && !isLive;
      const atLive = media ? b <= LIVE_SLACK : b === 0;
      root.classList.toggle('is-behind', !atLive && !replay);
      root.classList.toggle('is-replay', replay);
      live.hidden = atLive || ended || replay;
      let pct;
      if (replay) {
        const d = media.duration || 0;
        time.textContent = ended ? 'ENDED' : fmt(media.currentTime) + ' / ' + fmt(d);
        badge.textContent = ended ? 'ENDED' : 'REPLAY';
        pct = d ? (media.currentTime / d) * 100 : 0;
      } else {
        time.textContent = ended ? 'ENDED' : atLive ? 'LIVE' : '-' + fmt(b) + ' behind';
        badge.textContent = ended ? 'ENDED' : atLive ? 'LIVE' : 'DELAYED';
        pct = ((win() - Math.min(b, win())) / win()) * 100;
      }
      fill.style.width = pct + '%'; knob.style.left = pct + '%';
      track.setAttribute('aria-valuemax', String(Math.round(win()))); track.setAttribute('aria-valuenow', String(Math.round(win() - Math.min(b, win()))));
      const free = pass || replay;
      [prev, next].forEach((x) => x.setAttribute('aria-disabled', free ? 'false' : 'true'));
      if (mute) { mute.setAttribute('aria-pressed', String(!!media && media.muted)); mute.setAttribute('aria-label', media && media.muted ? 'Sound off, tap to turn on' : 'Sound on');
        const w = mute.querySelector('[data-vp-wave]'); if (w) w.toggleAttribute('hidden', !!media && media.muted); }
    }

    function goLive() {
      if (!media) { behind = 0; return; }
      if (!isLive) return;
      const t = hls && hls.liveSyncPosition ? hls.liveSyncPosition : edge();
      media.currentTime = Math.max(floorT(), t - 1);
    }
    function start() {
      if (media) { if (!pass && isLive) goLive(); const p = media.play(); if (p && p.catch) p.catch(() => {}); paint(); return; }
      if (embed) return;
      if (!pass) behind = 0;                         // no pass: no rewind, playing means live
      playing = true; clearInterval(tick);
      // while paused you fall behind live, while playing the gap stays the same
      tick = setInterval(() => paint(), 1000);
      paint();
    }
    function stop() {
      if (media) { media.pause(); paint(); return; }
      if (embed) return;
      playing = false; clearInterval(tick);
      if (pass) {                                     // with a pass, the time you paused for is kept
        tick = setInterval(() => { behind = Math.min(WINDOW, behind + 1); paint(); if (behind >= WINDOW) clearInterval(tick); }, 1000);
      }
      paint();
    }
    const toggle = () => {
      if (embed) return;
      if (snx.Sound) snx.Sound.play('tick');
      if (media && !isPlaying()) media.muted = false;   // a tap is the user gesture browsers want before sound
      (isPlaying() ? stop : start)();
    };
    btnPlay.addEventListener('click', toggle);
    big.addEventListener('click', toggle);
    screen.addEventListener('click', (ev) => { if (ev.target === screen || ev.target.closest('.vp-scene') || (media && ev.target === media)) toggle(); });
    if (mute) mute.addEventListener('click', () => { if (media) { media.muted = !media.muted; paint(); } });

    function nudge(dir) {                             // dir: -1 = previous 10 s, +1 = forward 10 s
      if (media) {
        if (isLive && !pass) return needPass();
        const t = media.currentTime + dir * STEP;
        media.currentTime = isLive ? Math.min(edge(), Math.max(floorT(), t)) : Math.min(media.duration || t, Math.max(0, t));
        if (snx.Sound) snx.Sound.play('tick');
        paint(); return;
      }
      if (embed) return;
      if (!pass) return needPass();
      behind = dir < 0 ? Math.min(WINDOW, behind + STEP) : Math.max(0, behind - STEP);
      if (snx.Sound) snx.Sound.play('tick'); paint();
    }
    prev.addEventListener('click', () => nudge(-1));
    next.addEventListener('click', () => nudge(1));
    live.addEventListener('click', () => { goLive(); if (!isPlaying()) start(); else paint(); });

    function seek(x) {
      const r = track.getBoundingClientRect(), p = Math.min(1, Math.max(0, (x - r.left) / r.width));
      if (media) {
        if (isLive) { if (!pass) return needPass(); media.currentTime = Math.max(floorT(), edge() - (1 - p) * DVR()); }
        else media.currentTime = p * (media.duration || 0);
        paint(); return;
      }
      if (!pass) return needPass();
      behind = Math.round(WINDOW - p * WINDOW); paint();
    }
    track.addEventListener('click', (ev) => seek(ev.clientX));

    // ---- real video: mount / unmount / retry
    function loadHls(cb) {
      if (window.Hls) return cb();
      const url = root.dataset.hls;
      if (!url) return cb(new Error('no hls bundle'));
      const el = document.createElement('script');
      el.src = url; el.onload = () => cb(); el.onerror = () => cb(new Error('hls load failed'));
      document.head.appendChild(el);
    }
    function unmount() {
      clearTimeout(retryTimer);
      if (hls) { try { hls.destroy(); } catch (e) { /* ignore */ } hls = null; }
      if (media) { try { media.pause(); media.removeAttribute('src'); media.load(); } catch (e) { /* ignore */ } }
      if (mediaHost) mediaHost.replaceChildren();
      media = null; embed = false; showMsg('');
      root.classList.remove('has-media', 'is-embed', 'is-replay', 'is-standby', 'is-slate');
    }
    // Failover: after two failures in a row the player tells the server which source it could not play, and the server
    // answers with the next one in the chain (or the same one if there is none, so retrying simply continues).
    // The skip lasts a minute, then the primary is tried again.
    const bad = {};
    const badList = () => { const now = Date.now(); return Object.keys(bad).filter((k) => bad[k] > now); };
    function fatal() {                                // never a black box: retry with backoff, then say so
      failures += 1;
      if (failures === 2 && cfg && cfg.source_id && root.dataset.liveUrl) {
        bad[cfg.source_id] = Date.now() + 60000; etag = ''; poll();
      }
      if (failures > 4) {
        showMsg('The video is not available right now. We keep trying.');
        retryTimer = setTimeout(() => { failures = 3; mount(cfg, true); }, 15000);
        return;
      }
      showMsg('Reconnecting…');
      retryTimer = setTimeout(() => mount(cfg, true), 1000 * Math.pow(2, failures - 1));
    }
    function onMedia(ev) {
      if (ev.type === 'playing') { failures = 0; mediaFixes = 0; showMsg(''); }
      if (ev.type === 'loadedmetadata' && cfg && cfg.start && !isLive) { try { media.currentTime = cfg.start; } catch (e) { /* ignore */ } }
      if (ev.type === 'durationchange' && cfg && cfg.kind === 'hls' && cfg.mode === 'auto' && !hls) isLive = !isFinite(media.duration);   // Safari: native HLS says live as Infinity
      paint();
    }
    function mount(c, retrying) {
      cfg = c; unmount();
      if (!retrying) failures = 0;
      if (c && (c.status === 'slate' || c.status === 'off')) {      // nothing usable (or the admin chose a message): never a black box
        root.classList.add('is-slate'); showMsg(c.message || 'The stream is offline. Please check back soon.'); paint(); return;
      }
      if (!c || !c.kind) { paint(); return; }         // nothing configured: the sample feed
      if (c.mode === 'standby') root.classList.add('is-standby');
      root.classList.add('has-media');
      if (c.kind === 'hls' || c.kind === 'file') {
        const v = document.createElement('video');
        v.setAttribute('playsinline', ''); v.playsInline = true; v.preload = 'auto';
        v.muted = c.autoplay_muted !== false; v.loop = !!c.loop && c.kind === 'file';
        mediaHost.appendChild(v); media = v;
        isLive = c.kind === 'hls' && c.mode !== 'replay' && c.mode !== 'standby';
        v.loop = v.loop || c.mode === 'standby';
        ['play', 'pause', 'playing', 'seeked', 'timeupdate', 'durationchange', 'loadedmetadata', 'ended', 'volumechange'].forEach((e) => v.addEventListener(e, onMedia));
        v.addEventListener('error', fatal);
        if (c.kind === 'file') v.src = c.src;
        else if (v.canPlayType('application/vnd.apple.mpegurl')) v.src = c.src;          // Safari / iPhone play HLS natively
        else {
          loadHls((err) => {
            if (media !== v) return;
            if (err || !window.Hls || !window.Hls.isSupported()) { showMsg('This browser cannot play this live stream.'); return; }
            const H = window.Hls;
            hls = new H({ lowLatencyMode: true, backBufferLength: DVR(), liveSyncDurationCount: 3 });
            hls.on(H.Events.LEVEL_LOADED, (e, d) => { if (c.mode === 'auto') isLive = !!(d && d.details && d.details.live); paint(); });
            hls.on(H.Events.ERROR, (e, d) => {
              if (!d || !d.fatal) return;
              if (d.type === H.ErrorTypes.MEDIA_ERROR && mediaFixes++ < 2) hls.recoverMediaError(); else fatal();
            });
            hls.loadSource(c.src); hls.attachMedia(v);
          });
        }
        paint();
        if (!ended) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
      } else {
        embed = true; isLive = c.mode !== 'replay' && c.mode !== 'standby';
        root.classList.add('is-embed');
        let u = c.embed || '';
        if (c.kind === 'youtube') u += '&autoplay=1' + (c.autoplay_muted !== false ? '&mute=1' : '') + (c.loop ? '' : '');
        const f = document.createElement('iframe');
        f.src = u; f.loading = 'lazy'; f.allowFullscreen = true; f.title = c.name || 'Live video';
        f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
        f.allow = 'autoplay; fullscreen; picture-in-picture'; f.referrerPolicy = 'strict-origin-when-cross-origin';
        mediaHost.appendChild(f);
        paint();
      }
    }
    const sig = (c) => (c ? [c.status, c.kind, c.mode, c.src, c.embed, c.loop, c.start, c.message, c.source_id].join('|') : '');
    let pollTimer = 0;
    async function poll() {
      clearTimeout(pollTimer);
      let url = root.dataset.liveUrl;
      if (!url) return;
      const skip = badList();
      if (skip.length) url += '?bad=' + skip.join(',');
      if (url !== lastUrl) { lastUrl = url; etag = ''; }          // a different question has a different ETag
      try {
        if (!document.hidden) {
          const r = await fetch(url, { headers: etag ? { 'If-None-Match': etag } : {}, credentials: 'same-origin' });
          if (r.status === 200) {
            const d = await r.json();
            etag = r.headers.get('ETag') || etag;
            if (sig(d) !== sig(cfg)) { if (rev) say('The video was just updated.', 'info'); mount(d); } else cfg = d;
            rev = d.rev || rev;
          }
        }
      } catch (e) { /* offline: keep what is playing */ }
      pollTimer = setTimeout(poll, 3000);
    }
    let etag = '', lastUrl = '';
    document.addEventListener('visibilitychange', () => { if (document.hidden && media && !isLive && !media.paused) media.pause(); });   // a replay in a hidden tab only burns data
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
    const autoStart = (snapLive) => {
      if (ended || embed) return;
      if (media) { if (snapLive && isLive && lag() > LIVE_SLACK) goLive(); if (media.paused) start(); return; }
      if (snapLive) behind = 0; if (!playing) start(); else if (snapLive) paint();
    };
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
      if (d.id != null && root.dataset.liveUrl) { root.dataset.liveUrl = '/_live/stream/f' + d.id; etag = ''; poll(); }   // each game can have its own video
      if (!media && !embed) { behind = 0; paint(); autoStart(false); } else paint();
    });

    let boot = null;
    try { boot = JSON.parse(root.dataset.stream || 'null'); } catch (e) { boot = null; }
    if (boot && (boot.kind || boot.status === 'slate' || boot.status === 'off')) mount(boot);
    else { cfg = boot; paint(); autoStart(false); }
    if (root.dataset.liveUrl) pollTimer = setTimeout(poll, 3000);
  });
})();
