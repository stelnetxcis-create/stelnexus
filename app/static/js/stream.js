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
  const DBG = (typeof location !== 'undefined' && /[?&]vpdebug\b/.test(location.search)) || (function () { try { return localStorage.getItem('snx.vpdebug') === '1'; } catch (e) { return false; } })();   // ?vpdebug=1 logs every YouTube message to the console
  function ytShim(frame, hooks, startMuted) {
    const st = { time: 0, dur: 0, start: 0, end: 0, head: null, state: -1, vol: 100, muted: startMuted !== false, ready: false,
                 hold: 0, reportedAt: 0, moved: Date.now(), playingSince: 0, pausedAt: 0, endAtPause: 0, levels: [], quality: '', volLocked: false, asserted: false, seekOk: null };
    const queue = [];
    const baseEnd = () => (st.end > 0 ? st.end : st.dur);
    // While paused on a live stream the live edge keeps moving even if YouTube stops reporting it: that is what makes
    // "paused for 5 seconds" show as 5 seconds behind.
    const effEnd = () => { let e = baseEnd(); if (st.pausedAt && st.endAtPause > 0) e = Math.max(e, st.endAtPause + (Date.now() - st.pausedAt) / 1000); return e; };
    const setState = (n) => {
      if (n === st.state) return;
      const was = st.state; st.state = n;
      if (n === 1) {
        st.playingSince = Date.now(); st.pausedAt = 0;
        // YouTube applies the viewer's own saved youtube.com volume when playback starts: say ours once more
        if (!st.asserted && st.volLocked) { st.asserted = true; cmd('setVolume', [st.vol]); cmd(st.muted ? 'mute' : 'unMute'); }
      }
      else if (n === 2 && was !== 2) { st.pausedAt = Date.now(); st.endAtPause = baseEnd(); }
    };
    let hs = 0;
    const send = (obj) => { if (DBG) console.log('[yt >]', JSON.stringify(obj)); try { frame.contentWindow.postMessage(JSON.stringify(obj), '*'); } catch (e) { /* frame is gone */ } };
    const cmd = (func, args) => { const m = { event: 'command', func, args: args || [] }; if (st.ready) send(m); else queue.push(m); };
    const shake = () => send({ event: 'listening', id: 'snx', channel: 'widget' });
    function onMessage(ev) {
      if (ev.source !== frame.contentWindow || !YT_ORIGIN.test(ev.origin || '')) return;
      let d = ev.data;
      if (typeof d === 'string') { try { d = JSON.parse(d); } catch (e) { return; } }
      if (!d || typeof d !== 'object' || !d.event) return;
      if (DBG && d.event !== 'infoDelivery') console.log('[yt <]', JSON.stringify(d)); else if (DBG) console.log('[yt <] infoDelivery', JSON.stringify(d.info));
      window.snxYtLast = d;                          // last raw message: open the console and type snxYtLast to see what YouTube says
      if (!st.ready) { st.ready = true; clearInterval(hs); queue.splice(0).forEach(send); if (hooks.ready) hooks.ready(); }
      if (d.event === 'onStateChange') { setState(Number(d.info)); hooks.change(); }
      else if (d.event === 'onError') { if (hooks.error) hooks.error(Number(d.info)); }
      else if ((d.event === 'infoDelivery' || d.event === 'initialDelivery') && d.info && typeof d.info === 'object') {
        const i = d.info, held = Date.now() < st.hold;      // right after a seek the old position is still being reported
        if (!held && typeof i.currentTime === 'number') { if (Math.abs(i.currentTime - st.time) > 0.05) st.moved = Date.now(); st.time = i.currentTime; st.reportedAt = Date.now(); }
        if (typeof i.duration === 'number') st.dur = i.duration;
        if (typeof i.playerState === 'number') setState(i.playerState);
        if (Array.isArray(i.availableQualityLevels) && i.availableQualityLevels.join() !== st.levels.join()) { st.levels = i.availableQualityLevels.slice(); if (hooks.levels) hooks.levels(st.levels); }
        if (typeof i.playbackQuality === 'string') st.quality = i.playbackQuality;
        // our own volume / mute choice is the truth: YouTube echoes stale or its own saved values
        if (!st.volLocked) { if (typeof i.volume === 'number') st.vol = i.volume; if (typeof i.muted === 'boolean') st.muted = i.muted; }
        const p = i.progressState;                          // YouTube's own live-edge numbers, when it sends them
        if (p && typeof p === 'object') {
          if (!held && typeof p.current === 'number') { if (Math.abs(p.current - st.time) > 0.05) st.moved = Date.now(); st.time = p.current; st.reportedAt = Date.now(); }
          if (typeof p.duration === 'number' && p.duration > 0) st.dur = p.duration;
          if (typeof p.seekableStart === 'number') st.start = p.seekableStart;
          if (typeof p.seekableEnd === 'number' && p.seekableEnd > 0) st.end = p.seekableEnd;
          if (typeof p.isAtLiveHead === 'boolean') st.head = p.isAtLiveHead;
          if (typeof p.allowSeeking === 'boolean') st.seekOk = p.allowSeeking;       // false = the video's owner switched rewinding off: seekTo is ignored
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
      get frozen() { return st.state === 1 && Date.now() - Math.max(st.moved, st.playingSince) > 4000; },   // playing but the clock never moves: its numbers cannot be trusted
      set currentTime(t) {                                                // no known window (live without rewind): never seek, or "go live" would jump to 0
        const end = effEnd();
        if (!(end > 0)) return;
        st.time = Math.min(end, Math.max(st.start || 0, Number(t) || 0)); st.hold = Date.now() + 3500; st.moved = Date.now();
        cmd('seekTo', [st.time, true]); hooks.change();
      },
      get duration() { return st.dur > 0 ? st.dur : NaN; },               // a live stream without a rewind window reports 0
      get seekable() { const e = effEnd(); return { length: e > 0 ? 1 : 0, start: () => st.start || 0, end: () => e }; },
      get qualityLevels() { return st.levels; },
      get reportedAt() { return st.reportedAt; },
      get videoId() { try { return new URL(frame.src).pathname.split('/').filter(Boolean).pop() || ''; } catch (e) { return ''; } },
      // Method 2: load the same video again asking for a quality (YouTube honours this at load time more often than a live
      // change). A live video starts at the live head, so it is only used when the viewer is already at (or near) live.
      restartAt(code) { const id = this.videoId; if (!id) return false; st.hold = 0; st.reportedAt = 0; cmd('loadVideoById', [{ videoId: id, suggestedQuality: code }]); return true; },
      get allowSeeking() { return st.seekOk; },                              // true / false when YouTube says, otherwise null
      get playing() { return st.state === 1; },
      get playingSince() { return st.playingSince; },
      // Relative move used by the live controls: no window numbers needed, just "go to this second" in YouTube's own clock.
      jump(t) { st.time = Math.max(0, Number(t) || 0); st.hold = Date.now() + 3500; st.moved = Date.now(); st.reportedAt = 0; cmd('seekTo', [st.time, true]); hooks.change(); },
      get quality() { return st.quality; },
      // YouTube treats these as hints and applies them when it next fills its buffer, so after asking we nudge it by
      // re-seeking to where we already are (no jump). Auto frees the range again (it stays locked otherwise).
      setQuality(q, nudge) {
        if (q === 'auto') {
          const codes = st.levels.filter((c) => c !== 'auto');
          cmd('setPlaybackQualityRange', codes.length ? [codes[codes.length - 1], codes[0]] : ['tiny', 'highres']);
          cmd('setPlaybackQuality', ['default']);
        } else { cmd('setPlaybackQualityRange', [q, q]); cmd('setPlaybackQuality', [q]); }
        if (nudge && st.state === 1 && st.time > 0) { st.hold = Date.now() + 3500; cmd('seekTo', [st.time, true]); }
      },
      get muted() { return st.muted; },
      set muted(v) { st.muted = !!v; st.volLocked = true; cmd(v ? 'mute' : 'unMute'); hooks.change(); },
      get volume() { return st.vol / 100; },
      set volume(v) { st.vol = Math.round(Math.min(1, Math.max(0, Number(v) || 0)) * 100); st.volLocked = true; cmd('setVolume', [st.vol]); hooks.change(); },
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
    // Remembered per browser: sound on / off, volume and quality. A first visit wants sound (browsers only allow it
    // after a click, so it is switched on at the first click anywhere on the page).
    const PREF_KEY = 'snx.player';
    let prefs = {};
    try { prefs = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') || {}; } catch (e) { prefs = {}; }
    const savePrefs = () => { try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) { /* storage blocked */ } };
    const wantSound = () => prefs.muted !== true;
    let ended = root.dataset.ended === '1';
    const btnPlay = $('[data-vp-play]'), big = $('[data-vp-big]'), prev = $('[data-vp-prev]'), next = $('[data-vp-next]');
    const live = $('[data-vp-live]'), time = $('[data-vp-time]'), badge = $('[data-vp-badge-text]');
    const fill = $('[data-vp-fill]'), knob = $('[data-vp-knob]'), track = $('[data-vp-track]');
    const icoPlay = $('[data-vp-ico-play]'), icoPause = $('[data-vp-ico-pause]'), screen = $('[data-vp-screen]');
    let playing = false, behind = 0, tick = 0, volDrag = false;
    const mediaHost = $('[data-vp-media]'), msg = $('[data-vp-msg]'), mute = $('[data-vp-mute]');
    const vol = $('[data-vp-vol]'), snd = $('[data-vp-sound]');
    let guardTimer = 0, liveCheck = 0, seekTarget = null, seekUntil = 0;
    // YouTube live: we keep our own "seconds behind live" (ytBase, growing while paused). YouTube's duration / seekable
    // numbers are not trustworthy on live streams, but its currentTime and seekTo share one clock, so every move is relative
    // to that clock and checked afterwards (ytMove).
    let ytBase = 0, ytSince = 0, ytWasPaused = true, ytStarted = false, ytVerify = 0, ytTimer = 0, qCheck = 0, qTried = {};
    const LIVE_YT = 3;
    const ytLocked = () => !!(media && media.isYt && isLive && media.allowSeeking === false);
    let lockToastAt = 0;
    const noRewind = () => {
      if (Date.now() - lockToastAt < 8000) return; lockToastAt = Date.now();
      say('This live stream does not allow rewinding (YouTube has it switched off), so Previous, Forward and the bar are unavailable. Pause, Go live, sound and full screen still work.', 'info');
    };                              // YouTube did not answer in time: fall back to its own controls
    // ---- real video state (media === null and embed === false means the sample feed)
    let media = null, embed = false, hls = null, cfg = null, isLive = true, rev = '', failures = 0, retryTimer = 0, mediaFixes = 0;
    const LIVE_SLACK = 12;                           // a real HLS edge is a few seconds behind the encoder: that still counts as LIVE
    const DVR = () => Math.max(30, Number((cfg && cfg.dvr_window) || WINDOW));
    const edge = () => { const r = media.seekable; return r && r.length ? r.end(r.length - 1) : (isFinite(media.duration) ? media.duration : 0); };
    const floorT = () => { const r = media.seekable; return r && r.length ? Math.max(r.start(0), edge() - DVR()) : 0; };
    const win = () => (media ? (isLive ? DVR() : (media.duration || 0)) : WINDOW);
    function ytFold() {                              // called on every paint: starts the clock at the first frame, and folds pauses in
      if (!media || !media.isYt || !isLive) return;
      const p = media.paused, now = Date.now();
      if (!ytStarted) { if (!p) { ytStarted = true; ytBase = 0; ytSince = now; ytWasPaused = false; } return; }
      if (p !== ytWasPaused) { if (ytWasPaused) ytBase += (now - ytSince) / 1000; ytSince = now; ytWasPaused = p; }
      if (!p && media.atLiveHead === true && seekTarget == null) { ytBase = 0; ytSince = now; }      // YouTube says we are at the live head: believe it
    }
    const ytBehind = () => (ytStarted ? ytBase + (media.paused ? (Date.now() - ytSince) / 1000 : 0) : 0);
    const lag = () => {
      if (!media) return behind;
      if (!isLive) return 0;
      if (seekTarget != null) return seekTarget;                       // dragging, or a seek not yet confirmed: show where we are going
      if (media.isYt) return Math.max(0, Math.round(ytBehind()));
      return Math.max(0, Math.round(edge() - media.currentTime));
    };
    const isPlaying = () => (media ? !media.paused && !media.ended : playing);
    const showMsg = (t) => { if (msg) { msg.hidden = !t; msg.textContent = t || ''; } };

    const say = (m, level) => (snx.toast ? snx.toast(m, { level: level || 'warning' }) : null);
    const needPass = () => {
      say('Previous and forward are Battle Pass features. Get one in the Store.', 'warning');
      if (snx.Sound) snx.Sound.play('err');
    };

    function paint() {
      ytFold(); settleSeek(); if (qBox && !qBox.hidden) qLabelRefresh();
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
      const atLive = media ? b <= (media.isYt ? LIVE_YT : LIVE_SLACK) : b === 0;
      root.classList.toggle('is-noseek', ytLocked());
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
      if (vol && media && !volDrag) { vol.value = String(media.muted ? 0 : Math.round((media.volume == null ? 1 : media.volume) * 100)); }
      if (vol) vol.style.setProperty('--v', vol.value + '%');
      if (snd) snd.hidden = !(media && media.muted && on && !ended);   // autoplay starts muted: one tap gives the sound (a tap is the gesture browsers want)
    }

    // YouTube: move to a different distance behind live, then check YouTube really went there. We send a relative
    // seekTo, wait for YouTube to report its clock again, and correct our own number by however far it missed.
    // Going live overshoots on purpose (YouTube clamps at the live head); if we are still behind afterwards the frame is
    // reloaded, because a live video always starts at the live head.
    function ytMove(newB, toLive) {
      const m = media;
      if (!m || !m.isYt) return;
      newB = Math.min(DVR(), Math.max(toLive ? -15 : 0, newB));
      const cur = m.currentTime;
      if (!(cur > 0)) { seekTarget = null; say('The video is still loading. Try again in a moment.', 'info'); paint(); return; }
      clearTimeout(ytVerify); clearTimeout(liveCheck);
      const ytBehind0 = ytBehind(), target = cur - (newB - ytBehind0), t0 = Date.now(), wasPlaying = !m.paused;
      ytBase = newB; ytSince = t0; ytWasPaused = m.paused; ytStarted = true;
      m.jump(target);
      seekTarget = Math.max(0, Math.round(newB)); seekUntil = t0 + 6000;
      const was = ytBehind0, before = cur;
      ytVerify = setTimeout(function check(tries) {
        if (media !== m) return;
        // Wait until YouTube is playing again: a seek on a live DVR stream stalls for a few seconds, and that stall is not an error.
        if (wasPlaying && !m.paused && !m.playing && (tries = (tries || 0) + 1) < 8) { ytVerify = setTimeout(() => check(tries), 1500); return; }
        const fresh = m.reportedAt > t0 + 3000, now = Date.now();
        if (!fresh) { if (wasPlaying && !toLive) say('YouTube did not confirm the move. This stream may not allow rewinding.', 'info'); }
        else {
          const run = (wasPlaying && !m.paused) ? (now - Math.max(t0, m.playingSince)) / 1000 : 0, got = m.currentTime;
          const dLand = Math.abs(got - (target + run)), dIgnored = Math.abs(got - (before + (wasPlaying && !m.paused ? (now - t0) / 1000 : 0)));
          if (dLand <= 8) { /* landed (a little late is fine) */ }
          else if (dIgnored <= 4 && Math.abs(target - before) > 6) {                  // the clock just carried on: YouTube ignored the jump
            ytBase = was; ytSince = t0;
            if (!toLive) say('YouTube did not accept the jump. This stream may not allow rewinding.', 'info');
          } else if (dLand > 20) { ytBase += (target + run) - got; if (!toLive) say('YouTube landed somewhere else, so the position shown was corrected.', 'info'); }
        }
        ytBase = Math.max(0, ytBase); seekTarget = null;
        if (toLive && lag() > LIVE_YT && cfg) { reloadYt(cfg); return; }       // still behind: a fresh frame always starts live
        paint();
      }, 4400);
    }
    function ytGoLive() {
      const m = media;
      if (ytBehind() < 2 && seekTarget == null) { m.play(); paint(); return; }
      if (ytLocked()) { if (cfg) reloadYt(cfg); return; }                       // no seeking allowed: a fresh frame always starts at the live head
      ytMove(-15, true); m.play();
    }
    function reloadYt(c) {
      unmount(); cfg = c; root.classList.add('has-media');
      mountYouTube(c);
    }
    function goLive() {
      seekTarget = null;
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
      prefs.muted = !on; savePrefs();
      media.muted = !on;
      if (on && media.volume === 0) media.volume = 1;
      paint();
    }
    if (mute) mute.addEventListener('click', () => { if (media) setSound(media.muted); });
    if (snd) snd.addEventListener('click', () => { if (snd && snd.hidden) return; if (snx.Sound) snx.Sound.play('tick'); setSound(true); });
    if (vol) { vol.addEventListener('pointerdown', () => { volDrag = true; }); const vend = () => { volDrag = false; paint(); }; vol.addEventListener('pointerup', vend); vol.addEventListener('pointercancel', vend); vol.addEventListener('blur', vend); }
    if (vol) vol.addEventListener('input', () => {
      if (!media) return;
      const v = Number(vol.value) / 100;
      if (v > 0 && media.muted) media.muted = false;
      media.volume = v; if (v === 0) media.muted = true;
      prefs.volume = v; prefs.muted = v === 0; savePrefs();
      paint();
    });

    function nudge(dir) {                             // dir: -1 = previous 10 s, +1 = forward 10 s
      if (media) {
        if (isLive && !pass) return needPass();
        if (ytLocked()) return noRewind();
        if (media.isYt && isLive) { ytMove(lag() - dir * STEP); if (snx.Sound) snx.Sound.play('tick'); return; }
        seekTarget = null;
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

    const barPos = (x) => { const r = track.getBoundingClientRect(); return Math.min(1, Math.max(0, (x - r.left) / (r.width || 1))); };
    function seek(p, commit) {
      if (media) {
        if (isLive) {
          if (!pass) return needPass();
          if (ytLocked()) { seekTarget = null; if (commit) noRewind(); paint(); return; }
          const secs = Math.round((1 - p) * DVR());                    // seconds behind live
          if (!commit) { seekTarget = secs; paint(); return; }
          if (media.isYt) { ytMove(secs); return; }
          const e = edge();
          media.currentTime = Math.max(floorT(), e - secs);
          seekTarget = secs; seekUntil = Date.now() + 4000;            // keep showing the target until the player reports it (or 4 s pass)
        } else if (commit || !media.isYt) media.currentTime = p * (media.duration || 0);          // a replay follows the finger (not YouTube: one seek on release)
        paint(); return;
      }
      if (!pass) return needPass();
      behind = Math.round(WINDOW - p * WINDOW); paint();
    }
    // once the real position has caught up with the target (or the hold ran out) the display follows the player again
    function settleSeek() {
      if (seekTarget == null || dragging || !media || !isLive) return;
      const real = Math.max(0, Math.round(edge() - media.currentTime));
      if (Math.abs(real - seekTarget) <= 3 || Date.now() > seekUntil) seekTarget = null;
    }
    // click or drag the bar to move through the live window. While dragging only the knob moves (a seek on every
    // pointer move floods YouTube and loses the last position); the video moves once, where you let go.
    let dragging = false, dragP = 0;
    track.addEventListener('pointerdown', (ev) => {
      if ((!media || isLive) && !pass) return needPass();
      if (ytLocked()) return noRewind();
      dragging = true; dragP = barPos(ev.clientX);
      try { track.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      seek(dragP, false);
    });
    track.addEventListener('pointermove', (ev) => { if (dragging) { dragP = barPos(ev.clientX); seek(dragP, false); } });
    const endDrag = (ev) => { if (!dragging) return; dragging = false; if (ev && ev.type === 'pointercancel') { seekTarget = null; paint(); return; } seek(dragP, true); };
    track.addEventListener('pointerup', endDrag); track.addEventListener('pointercancel', endDrag);

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
      clearTimeout(retryTimer); clearTimeout(guardTimer); clearTimeout(liveCheck); clearTimeout(ytVerify); clearTimeout(qCheck); clearInterval(ytTimer);
      ytBase = 0; ytSince = 0; ytWasPaused = true; ytStarted = false; qTried = {};
      if (hls) { try { hls.destroy(); } catch (e) { /* ignore */ } hls = null; }
      if (media) { try { media.pause(); media.removeAttribute('src'); media.load(); } catch (e) { /* ignore */ } if (media.destroy) media.destroy(); }
      if (mediaHost) mediaHost.replaceChildren();
      media = null; embed = false; seekTarget = null; showMsg('');
      if (snd) snd.hidden = true;
      if (qBox) { qBox.hidden = true; qMenu.hidden = true; }
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
    // The first click or key press on the page is the gesture browsers want before they allow sound.
    const onGesture = (ev) => {
      if (ev.target && ev.target.closest && ev.target.closest('[data-vp-mute],[data-vp-vol],[data-vp-q]')) return;
      if (media && media.muted && wantSound() && !embed) setSound(true);
    };
    document.addEventListener('pointerdown', onGesture, { passive: true });
    document.addEventListener('keydown', onGesture, { passive: true });
    function applyPrefs(m) {
      m.volume = typeof prefs.volume === 'number' ? Math.min(1, Math.max(0, prefs.volume)) : 1;    // first visit = 100%, never YouTube's own saved level
      if (wantSound()) m.muted = false;
    }

    // ---- quality menu (hls.js levels, or YouTube's quality levels when it offers them)
    const qBox = $('[data-vp-q]'), qBtn = $('[data-vp-qbtn]'), qMenu = $('[data-vp-qmenu]'), qLabel = $('[data-vp-qlabel]');
    const YT_Q = { highres: 4320, hd2880: 2880, hd2160: 2160, hd1440: 1440, hd1080: 1080, hd720: 720, large: 480, medium: 360, small: 240, tiny: 144 };
    let qItems = [], qCurrent = String(prefs.quality || 'auto');
    const qName = (id) => (id === 'auto' ? 'Auto' : id + 'p');
    function showQuality(items) {                      // items: [{ id: '1080', apply() }] without Auto; [] hides the control
      qItems = items;
      if (!qBox) return;
      qBox.hidden = !items.length;
      qMenu.replaceChildren();
      [{ id: 'auto' }].concat(items).forEach((it) => {
        const li = document.createElement('li'), b = document.createElement('button');
        b.type = 'button'; b.dataset.q = it.id; b.setAttribute('role', 'menuitemradio'); b.textContent = qName(it.id);
        b.setAttribute('aria-checked', String(it.id === qCurrent));
        li.appendChild(b); qMenu.appendChild(li);
      });
      qLabelRefresh();
    }
    function qLabelRefresh() {
      if (!qLabel) return;
      let t = qName(qCurrent);
      if (qCurrent === 'auto' && media && media.isYt && YT_Q[media.quality]) t = 'Auto \u00B7 ' + YT_Q[media.quality] + 'p';     // say what is really playing
      if (qLabel.textContent !== t) qLabel.textContent = t;
    }
    // YouTube treats a quality request as a hint. Method 1 (set the range, done in apply) is checked after 4 s against
    // the quality YouTube reports. If it did not take, method 2 reloads the live with the quality suggested. If that does
    // not take either, we say so and fall back to Auto, so the label never claims a quality that is not playing.
    function ytQualityVerify(id, it, loud) {
      const m = media;
      const step = (n) => {
        if (media !== m || qCurrent !== id) return;
        const got = m.quality;
        if (got === it.code) { qLabelRefresh(); if (loud) say('Quality set to ' + id + 'p.', 'info'); return; }
        if (n === 0 && !qTried[id] && m.restartAt && !m.paused && lag() <= 10 && (qTried[id] = true) && m.restartAt(it.code)) {
          ytBase = 0; ytSince = Date.now(); ytStarted = false; ytWasPaused = true; seekTarget = null;
          qCheck = setTimeout(() => step(1), 7000); return;
        }
        qCurrent = 'auto'; prefs.quality = 'auto'; savePrefs(); if (m.setQuality) m.setQuality('auto', false); showQuality(qItems);
        if (loud) say(got && YT_Q[got]
          ? 'YouTube kept playing at ' + YT_Q[got] + 'p and would not switch. Auto is back on; the label shows what is really playing.'
          : 'YouTube did not confirm the quality change, so Auto is back on.', 'info');
      };
      qCheck = setTimeout(() => step(0), 4000);
    }
    function pickQuality(id, byUser) {
      qCurrent = id; prefs.quality = id; savePrefs();
      const it = qItems.find((x) => x.id === id);
      if (id === 'auto') { if (hls) hls.currentLevel = -1; else if (media && media.setQuality) media.setQuality('auto', !!byUser); }
      else if (it) it.apply(!!byUser);
      clearTimeout(qCheck);
      if (id !== 'auto' && media && media.isYt && it && it.code) ytQualityVerify(id, it, !!byUser);
      showQuality(qItems);
      if (qMenu) qMenu.hidden = true; if (qBtn) qBtn.setAttribute('aria-expanded', 'false');
    }
    if (qBtn) {
      qBtn.addEventListener('click', () => { qMenu.hidden = !qMenu.hidden; qBtn.setAttribute('aria-expanded', String(!qMenu.hidden)); });
      qMenu.addEventListener('click', (ev) => { const b = ev.target.closest('button[data-q]'); if (b) pickQuality(b.dataset.q, true); });
      document.addEventListener('pointerdown', (ev) => { if (!qMenu.hidden && !qBox.contains(ev.target)) { qMenu.hidden = true; qBtn.setAttribute('aria-expanded', 'false'); } }, { passive: true });
      document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && !qMenu.hidden) { qMenu.hidden = true; qBtn.setAttribute('aria-expanded', 'false'); } });
    }
    function hlsLevels() {
      if (!hls) return;
      const best = {};                                  // height -> index of its best bitrate
      hls.levels.forEach((l, i) => { const h = l.height || 0; if (h && (best[h] == null || (l.bitrate || 0) > (hls.levels[best[h]].bitrate || 0))) best[h] = i; });
      const items = Object.keys(best).map(Number).sort((a, b) => b - a).map((h) => ({ id: String(h), apply() { if (hls) hls.currentLevel = best[h]; } }));
      if (qCurrent !== 'auto' && !items.some((x) => x.id === qCurrent)) qCurrent = 'auto';
      showQuality(items.length > 1 ? items : []);
      if (qCurrent !== 'auto') pickQuality(qCurrent);
    }
    function ytLevels(list) {
      const seen = {};
      const items = [];
      list.forEach((code) => { const h = YT_Q[code]; if (h && !seen[h]) { seen[h] = 1; items.push({ id: String(h), code, apply(nudge) { if (media && media.setQuality) media.setQuality(code, nudge); } }); } });
      items.sort((a, b) => Number(b.id) - Number(a.id));
      if (qCurrent !== 'auto' && !items.some((x) => x.id === qCurrent)) qCurrent = 'auto';
      showQuality(items.length > 1 ? items : []);
      if (qCurrent !== 'auto') pickQuality(qCurrent);
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
            hls.on(H.Events.MANIFEST_PARSED, hlsLevels);
            hls.loadSource(c.src); hls.attachMedia(v);
          });
        }
        paint();
        if (!ended) {
          v.volume = typeof prefs.volume === 'number' ? prefs.volume : 1;
          const tryPlay = (withSound) => { v.muted = !withSound; const p = v.play(); if (p && p.catch) p.catch(() => { if (withSound) tryPlay(false); }); };
          tryPlay(wantSound());                      // sound first when you left it on; muted if the browser refuses
        }
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
        ready() { clearTimeout(guardTimer); showMsg(''); applyPrefs(shim); if (!ended && shim.paused) shim.play(); paint(); },
        change() { if (media === shim) { if (!shim.paused) { failures = 0; showMsg(''); } paint(); } },
        error(code) { if (media === shim) embedFail(code); },
        levels(list) { if (media === shim) ytLevels(list); },
      }, c.autoplay_muted !== false);
      media = shim; root.classList.add('is-yt');
      clearInterval(ytTimer); ytTimer = setInterval(() => { if (media === shim) paint(); }, 1000);     // keeps "-00:07 behind" ticking while paused
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
