/* Live video player.
   Two engines behind the same controls:
     * the SAMPLE feed (nothing taken live on Admin Panel -> Streams): a simulated live edge with a 2-minute window;
     * REAL video from the Streams page: HLS / MP4 in our own <video> (hls.js is lazy-loaded only when needed), or a
       sandboxed iframe for YouTube / Facebook / allowed embeds.
   behind = seconds behind the live edge (0 = live).
   Everyone: play / pause. Without a Battle Pass, Play snaps back to live on a live stream.
   Battle Pass (data-pass="1"): Previous (-10s), Forward (+10s, never past live), click the bar to seek.
   A replay (a finished video) has a normal seek bar for everyone. YouTube is driven through its postMessage API
   (ytShim below): our play / pause / sound / volume / go live / +-10 s control it, YouTube's own controls and full
   screen button are switched off, and the iframe is click-proof, so there is ONE player UI and ONE full screen (ours,
   which keeps the strip, chat and bet panel on screen). If YouTube does not answer, the frame is reloaded with its own
   controls and ours step aside. Other embeds (Facebook, extra hosts) cannot be controlled: they run passively with
   only our full screen button, never a play button that does nothing. The page polls /_live/stream, so a switch needs
   no reload. */
(function () {
  const WINDOW = 120, STEP = 10;
  const snx = window.snx || {};
  const fmt = (s) => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');

  /* ------------------------------------------------------------------------------------------------------------------
     YouTube controller. Talks to an embed (enablejsapi=1) with postMessage and looks like the small part of an
     HTMLMediaElement that the player below uses (paused, ended, currentTime, duration, seekable, muted, volume, play,
     pause), so YouTube gets exactly the same controls, Battle Pass rules and live / behind logic as our own <video>.
     Commands sent before the frame answers are queued. hooks: ready(), change(), error(code). */
  const YT_ORIGIN = /^https:\/\/(www\.)?youtube(-nocookie)?\.com$/;
  function ytShim(frame, hooks, startMuted) {
    const st = { time: 0, dur: 0, start: 0, end: 0, head: null, state: -1, vol: 100, muted: startMuted !== false, ready: false,
                 hold: 0, moved: Date.now() };
    const queue = [];
    let hs = 0;
    const send = (obj) => { try { frame.contentWindow.postMessage(JSON.stringify(obj), '*'); } catch (e) { /* frame is gone */ } };
    const cmd = (func, args) => { const m = { event: 'command', func, args: args || [] }; if (st.ready) send(m); else queue.push(m); };
    const shake = () => send({ event: 'listening', id: 'snx', channel: 'widget' });
    function onMessage(ev) {
      if (ev.source !== frame.contentWindow || !YT_ORIGIN.test(ev.origin || '')) return;
      let d = ev.data;
      if (typeof d === 'string') { try { d = JSON.parse(d); } catch (e) { return; } }
      if (!d || typeof d !== 'object' || !d.event) return;
      window.snxYtLast = d;                          // last raw message: open the console and type snxYtLast to see what YouTube says
      if (!st.ready) { st.ready = true; clearInterval(hs); queue.splice(0).forEach(send); if (hooks.ready) hooks.ready(); }
      if (d.event === 'onStateChange') { st.state = Number(d.info); hooks.change(); }
      else if (d.event === 'onError') { if (hooks.error) hooks.error(Number(d.info)); }
      else if ((d.event === 'infoDelivery' || d.event === 'initialDelivery') && d.info && typeof d.info === 'object') {
        const i = d.info, held = Date.now() < st.hold;      // right after a seek the old position is still being reported
        if (!held && typeof i.currentTime === 'number') { if (Math.abs(i.currentTime - st.time) > 0.05) st.moved = Date.now(); st.time = i.currentTime; }
        if (typeof i.duration === 'number') st.dur = i.duration;
        if (typeof i.playerState === 'number') st.state = i.playerState;
        if (typeof i.volume === 'number') st.vol = i.volume;
        if (typeof i.muted === 'boolean') st.muted = i.muted;
        const p = i.progressState;                          // YouTube's own live-edge numbers, when it sends them
        if (p && typeof p === 'object') {
          if (!held && typeof p.current === 'number') { if (Math.abs(p.current - st.time) > 0.05) st.moved = Date.now(); st.time = p.current; }
          if (typeof p.duration === 'number' && p.duration > 0) st.dur = p.duration;
          if (typeof p.seekableStart === 'number') st.start = p.seekableStart;
          if (typeof p.seekableEnd === 'number' && p.seekableEnd > 0) st.end = p.seekableEnd;
          if (typeof p.isAtLiveHead === 'boolean') st.head = p.isAtLiveHead;
        }
        hooks.change();
      }
    }
    window.addEventListener('message', onMessage);
    frame.addEventListener('load', shake);
    hs = setInterval(shake, 600);                    // the frame may load before we listen: keep asking until it answers
    return {
      get ready() { return st.ready; },
      get paused() { return !(st.state === 1 || st.state === 3); },      // 1 playing, 3 buffering
      get ended() { return st.state === 0; },
      isYt: true,
      get currentTime() { return st.time; },
      get atLiveHead() { return st.head; },                                // true / false when YouTube says, otherwise null
      get frozen() { return !(st.state === 1) ? false : Date.now() - st.moved > 4000; },   // playing but the clock never moves: its numbers cannot be trusted
      set currentTime(t) {                                                // no known window (live without rewind): never seek, or "go live" would jump to 0
        const end = st.end > 0 ? st.end : st.dur;
        if (!(end > 0)) return;
        st.time = Math.min(end, Math.max(st.start || 0, Number(t) || 0)); st.hold = Date.now() + 1500;
        cmd('seekTo', [st.time, true]); hooks.change();
      },
      get duration() { return st.dur > 0 ? st.dur : NaN; },               // a live stream without a rewind window reports 0
      get seekable() { const e = st.end > 0 ? st.end : st.dur; return { length: e > 0 ? 1 : 0, start: () => st.start || 0, end: () => e }; },
      get muted() { return st.muted; },
      set muted(v) { st.muted = !!v; cmd(v ? 'mute' : 'unMute'); hooks.change(); },
      get volume() { return st.vol / 100; },
      set volume(v) { st.vol = Math.round(Math.min(1, Math.max(0, Number(v) || 0)) * 100); cmd('setVolume', [st.vol]); hooks.change(); },
      play() { cmd('playVideo'); return Promise.resolve(); },
      pause() { cmd('pauseVideo'); },
      removeAttribute() { /* HTMLMediaElement API used by unmount(): nothing to do */ },
      load() { /* same */ },
      destroy() { clearInterval(hs); window.removeEventListener('message', onMessage); frame.removeEventListener('load', shake); queue.length = 0; },
    };
  }
  window.snxYtShim = ytShim;                         // exposed for tests and the Admin preview

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
    const vol = $('[data-vp-vol]'), snd = $('[data-vp-sound]');
    let guardTimer = 0, liveCheck = 0;                              // YouTube did not answer in time: fall back to its own controls
    // ---- real video state (media === null and embed === false means the sample feed)
    let media = null, embed = false, hls = null, cfg = null, isLive = true, rev = '', failures = 0, retryTimer = 0, mediaFixes = 0;
    const LIVE_SLACK = 12;                           // a real HLS edge is a few seconds behind the encoder: that still counts as LIVE
    const DVR = () => Math.max(30, Number((cfg && cfg.dvr_window) || WINDOW));
    const edge = () => { const r = media.seekable; return r && r.length ? r.end(r.length - 1) : (isFinite(media.duration) ? media.duration : 0); };
    const floorT = () => { const r = media.seekable; return r && r.length ? Math.max(r.start(0), edge() - DVR()) : 0; };
    const win = () => (media ? (isLive ? DVR() : (media.duration || 0)) : WINDOW);
    const lag = () => (media ? (isLive ? (media.isYt && (media.atLiveHead === true || media.frozen) ? 0 : Math.max(0, Math.round(edge() - media.currentTime))) : 0) : behind);
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
      if (vol && media && document.activeElement !== vol) vol.value = String(media.muted ? 0 : Math.round((media.volume == null ? 1 : media.volume) * 100));
      if (snd) snd.hidden = !(media && media.muted && on && !ended);   // autoplay starts muted: one tap gives the sound (a tap is the gesture browsers want)
    }

    // YouTube: seek to the live edge, then check it really got there. Its live positions are quirky and a seek can be
    // ignored; if we are still behind after a moment, reload the frame (a live video always starts at the live head).
    function ytGoLive() {
      clearTimeout(liveCheck);
      const m = media, c = cfg;
      m.currentTime = edge(); m.play(); paint();
      liveCheck = setTimeout(() => { if (media === m && isLive && c && lag() > LIVE_SLACK) reloadYt(c); }, 2800);
    }
    function reloadYt(c) {
      unmount(); cfg = c; root.classList.add('has-media');
      mountYouTube(c);
    }
    function goLive() {
      if (!media) { behind = 0; return; }
      if (!isLive) return;
      if (media.isYt) { ytGoLive(); return; }
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
    const onPicture = (ev) => ev.target === screen || ev.target.closest('.vp-scene') || (media && ev.target.closest('[data-vp-media]'));
    screen.addEventListener('click', (ev) => { if (onPicture(ev)) toggle(); });
    screen.addEventListener('dblclick', (ev) => { if (onPicture(ev) && full) full.click(); });          // double tap the picture = our full screen
    function setSound(on) {
      if (!media) return;
      media.muted = !on;
      if (on && media.volume === 0) media.volume = 1;
      paint();
    }
    if (mute) mute.addEventListener('click', () => { if (media) setSound(media.muted); });
    if (snd) snd.addEventListener('click', () => { if (snd && snd.hidden) return; if (snx.Sound) snx.Sound.play('tick'); setSound(true); });
    if (vol) vol.addEventListener('input', () => {
      if (!media) return;
      const v = Number(vol.value) / 100;
      if (v > 0 && media.muted) media.muted = false;
      media.volume = v; if (v === 0) media.muted = true;
      paint();
    });

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
      clearTimeout(retryTimer); clearTimeout(guardTimer); clearTimeout(liveCheck);
      if (hls) { try { hls.destroy(); } catch (e) { /* ignore */ } hls = null; }
      if (media) { try { media.pause(); media.removeAttribute('src'); media.load(); } catch (e) { /* ignore */ } if (media.destroy) media.destroy(); }
      if (mediaHost) mediaHost.replaceChildren();
      media = null; embed = false; showMsg('');
      if (snd) snd.hidden = true;
      root.classList.remove('has-media', 'is-embed', 'is-yt', 'is-embed-native', 'is-replay', 'is-standby', 'is-slate');
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
      } else if (c.kind === 'youtube') {
        mountYouTube(c);
      } else {
        mountPassive(c, c.embed || '');
      }
    }

    // An embed is never allowed its own full screen: that would show the bare frame and hide our strip, chat and bet
    // panel. Only our full screen button (it takes the whole stage) is offered.
    function makeFrame(url, title) {
      const f = document.createElement('iframe');
      f.src = url; f.loading = 'lazy'; f.title = title || 'Live video';
      f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
      f.allow = 'autoplay; picture-in-picture'; f.allowFullscreen = false; f.referrerPolicy = 'strict-origin-when-cross-origin';
      return f;
    }
    function ytUrl(embed, c, ownControls) {
      let u;
      try { u = new URL(embed); } catch (e) { return embed; }
      const q = u.searchParams;
      q.set('enablejsapi', '1'); q.set('origin', location.origin);
      q.set('rel', '0'); q.set('modestbranding', '1'); q.set('playsinline', '1'); q.set('iv_load_policy', '3'); q.set('cc_load_policy', '0');
      q.set('fs', '0');                                  // no YouTube full screen button: ours is the only one
      q.set('controls', ownControls ? '1' : '0'); q.set('disablekb', ownControls ? '0' : '1');
      q.set('autoplay', '1');
      if (c.autoplay_muted !== false) q.set('mute', '1'); else q.delete('mute');
      if (c.loop || c.mode === 'standby') { q.set('loop', '1'); q.set('playlist', u.pathname.split('/').pop()); }   // YouTube loops only with a playlist of one
      return u.toString();
    }
    // YouTube: our controls drive it. If it never answers, hand the frame its own controls so nobody is left stuck.
    function mountYouTube(c) {
      isLive = c.mode !== 'replay' && c.mode !== 'standby';
      const f = makeFrame(ytUrl(c.embed || '', c, false), c.name);
      const shim = ytShim(f, {
        ready() { clearTimeout(guardTimer); showMsg(''); if (!ended && shim.paused) shim.play(); paint(); },
        change() { if (media === shim) { if (!shim.paused) { failures = 0; showMsg(''); } paint(); } },
        error(code) { if (media === shim) embedFail(code); },
      }, c.autoplay_muted !== false);
      media = shim; root.classList.add('is-yt');
      mediaHost.appendChild(f);
      guardTimer = setTimeout(() => { if (media === shim && !shim.ready) mountPassive(c, ytUrl(c.embed || '', c, true), true); }, 7000);
      paint();
    }
    // Passive embed (Facebook, extra hosts, or YouTube that would not answer): we cannot control it, so the controls
    // that would do nothing are hidden by CSS and the provider's own controls are the player.
    function mountPassive(c, url, native) {
      if (native) {                                      // coming from a YouTube frame that did not answer: start clean
        unmount(); cfg = c;
        root.classList.add('has-media'); if (c.mode === 'standby') root.classList.add('is-standby');
      }
      root.classList.add('is-embed'); if (native) root.classList.add('is-embed-native');
      embed = true; isLive = c.mode !== 'replay' && c.mode !== 'standby';
      mediaHost.appendChild(makeFrame(url, c.name));
      paint();
    }
    // This video cannot play here (private, removed, embedding switched off): tell the server so it moves to the
    // next source in the chain, the same failover an HLS stream gets.
    function embedFail(code) {
      if ((code === 100 || code === 101 || code === 150 || code === 2) && cfg && cfg.source_id && root.dataset.liveUrl) {
        bad[cfg.source_id] = Date.now() + 60000; etag = ''; showMsg('This video cannot be played here. Switching\u2026'); poll();
        return;
      }
      fatal();
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
      if (t && d.n != null) t.textContent = 'Fight #' + d.n + ' \u00B7 Live match video';
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
