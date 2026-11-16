/* Livestream Chat (experimental) — short-poll delivery, live room list, typing dots,
   a wire ticker, and an audible cue for incoming messages with one visible mute control.
   No frameworks, no CDN. Every piece of user text is written with textContent. */
(function () {
  'use strict';
  const root = document.querySelector('[data-ls]');
  if (!root) return;

  const log = root.querySelector('[data-ls-log]');
  const form = root.querySelector('[data-ls-form]');
  const field = root.querySelector('[data-ls-field]');
  const counter = root.querySelector('[data-ls-count]');
  const jump = root.querySelector('[data-ls-jump]');
  const jumpText = root.querySelector('[data-ls-jump-text]');
  const typingRow = root.querySelector('[data-ls-typing]');
  const typingText = root.querySelector('[data-ls-typing-text]');
  const roomBox = root.querySelector('[data-ls-room]');
  const roomCount = root.querySelector('[data-ls-room-count]');
  const conn = root.querySelector('[data-ls-conn]');
  const connText = root.querySelector('[data-ls-conn-text]');
  const soundBtn = root.querySelector('[data-ls-sound]');
  const soundText = root.querySelector('[data-ls-sound-text]');
  const soundOn = root.querySelector('[data-ls-sound-on]');
  const soundOff = root.querySelector('[data-ls-sound-off]');
  const tickerTrack = root.querySelector('.ls-ticker-track');
  const fightsOut = root.querySelector('[data-ls-fights]');
  const nextBox = root.querySelector('[data-ls-next]');
  const countdown = root.querySelector('[data-ls-countdown]');
  const empty = root.querySelector('[data-ls-empty]');
  const api = (window.StelNexus && window.StelNexus.api) || null;
  if (!log || !form || !field || !api) return;

  const me = root.getAttribute('data-me') || '';
  let last = parseInt(root.getAttribute('data-last') || '0', 10) || 0;
  let unread = 0;
  let pinned = true;                 // is the reader at the bottom?
  let failures = 0;
  let polling = false;
  let stopped = false;
  let nextIso = nextBox ? nextBox.getAttribute('data-iso') : null;
  const seenDays = new Set();
  log.querySelectorAll('[data-day]').forEach((n) => seenDays.add(n.getAttribute('data-day')));

  /* ---------------- sound: one switch, off means silence everywhere ---------------- */
  const SOUND_KEY = 'snx.chat.sound';
  let soundOnFlag = true;
  try { soundOnFlag = window.localStorage.getItem(SOUND_KEY) !== 'off'; } catch (e) { /* private mode */ }
  let audio = null;

  function paintSound() {
    if (!soundBtn) return;
    soundBtn.setAttribute('aria-pressed', soundOnFlag ? 'true' : 'false');
    if (soundText) soundText.textContent = soundOnFlag ? 'Sound on' : 'Muted';
    if (soundOn) soundOn.hidden = !soundOnFlag;
    if (soundOff) soundOff.hidden = soundOnFlag;
    soundBtn.title = soundOnFlag ? 'Mute every sound in this room' : 'Unmute message sounds';
  }
  paintSound();

  function context() {
    if (!soundOnFlag) return null;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    if (!audio) { try { audio = new Ctor(); } catch (e) { return null; } }
    if (audio.state === 'suspended') { audio.resume().catch(() => {}); }
    return audio.state === 'running' ? audio : null;
  }

  function tone(freq, at, dur, gainPeak, type) {
    const ctx = context();
    if (!ctx) return;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type || 'sine';
    osc.frequency.setValueAtTime(freq, ctx.currentTime + at);
    gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
    gain.gain.exponentialRampToValueAtTime(gainPeak, ctx.currentTime + at + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + dur);
    osc.connect(gain); gain.connect(ctx.destination);
    osc.start(ctx.currentTime + at);
    osc.stop(ctx.currentTime + at + dur + 0.02);
  }
  const chimeIncoming = () => { tone(660, 0, 0.16, 0.06); tone(990, 0.09, 0.2, 0.045); };
  const chimeSent = () => { tone(880, 0, 0.09, 0.035, 'triangle'); };

  if (soundBtn) {
    soundBtn.addEventListener('click', () => {
      soundOnFlag = !soundOnFlag;
      try { window.localStorage.setItem(SOUND_KEY, soundOnFlag ? 'on' : 'off'); } catch (e) { /* ignore */ }
      paintSound();
      if (soundOnFlag) chimeSent();     // audible confirmation, tied to the visible switch
    });
  }
  // Browsers only allow audio after a gesture: arm it on the first one.
  ['pointerdown', 'keydown'].forEach((evt) => {
    window.addEventListener(evt, function arm() {
      context();
      window.removeEventListener(evt, arm);
    }, { once: true });
  });

  /* ---------------- rendering ---------------- */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function dayMarker(label, isNew) {
    const row = el('div', 'ls-day' + (isNew ? ' ls-new' : ''));
    row.appendChild(el('span', null, label));
    return row;
  }

  function atBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  }
  function toBottom(smooth) {
    log.scrollTo({ top: log.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    unread = 0;
    pinned = true;
    if (jump) jump.hidden = true;
  }
  function showJump() {
    if (!jump) return;
    jump.hidden = false;
    if (jumpText) jumpText.textContent = unread ? (unread + ' new message' + (unread > 1 ? 's' : '')) : 'Jump to latest';
  }

  function add(m, fresh) {
    if (empty && empty.parentNode) empty.remove();
    if (m.day && !seenDays.has(m.day)) {
      seenDays.add(m.day);
      log.appendChild(dayMarker(m.day));
    }
    if (fresh && !pinned && unread === 0) log.appendChild(dayMarker('New messages', true));
    const row = el('article', 'ls-msg ls-tone-' + (m.tone || 'slate') + (m.user === me ? ' mine' : '') + (fresh ? ' ls-fresh' : ''));
    row.setAttribute('data-id', String(m.id));
    row.setAttribute('data-day', m.day || '');
    row.appendChild(el('span', 'ls-avatar', m.initial || (m.user || '?').slice(0, 1).toUpperCase()));
    const bubble = el('div', 'ls-bubble');
    const head = document.createElement('header');
    head.appendChild(el('b', null, m.user));
    head.appendChild(el('span', 'ls-role', m.role));
    head.appendChild(el('time', null, m.at));
    bubble.appendChild(head);
    bubble.appendChild(el('p', null, m.body));
    row.appendChild(bubble);
    log.appendChild(row);
    last = Math.max(last, m.id);
    while (log.querySelectorAll('.ls-msg').length > 200) {
      const first = log.querySelector('.ls-msg');
      if (!first) break;
      first.remove();
    }
  }

  function paintRoom(people) {
    if (!roomBox || !Array.isArray(people)) return;
    roomBox.textContent = '';
    if (!people.length) {
      roomBox.appendChild(el('p', 'muted small', 'Nobody else has been here in the last few minutes.'));
    }
    people.forEach((p) => {
      const row = el('div', 'ls-person ls-tone-' + (p.tone || 'slate'));
      row.appendChild(el('span', 'ls-avatar', p.initial || '?'));
      const box = document.createElement('div');
      const name = el('b', null, p.user);
      if (p.me) { name.appendChild(document.createTextNode(' ')); name.appendChild(el('em', null, 'you')); }
      box.appendChild(name);
      box.appendChild(el('small', null, p.role + ' · seen ' + (p.seen || '—')));
      row.appendChild(box);
      row.appendChild(el('i', 'ls-here'));
      roomBox.appendChild(row);
    });
    if (roomCount) roomCount.textContent = String(people.length);
  }

  function paintTicker(items) {
    if (!tickerTrack || !Array.isArray(items)) return;
    tickerTrack.textContent = '';
    if (!items.length) {
      const span = el('span', 'ls-wire ls-wire-slate');
      span.appendChild(el('b', null, 'WIRE'));
      span.appendChild(document.createTextNode(' No results declared yet today.'));
      tickerTrack.appendChild(span);
      return;
    }
    items.forEach((t) => {
      const span = el('span', 'ls-wire ls-wire-' + (t.tone || 'slate'));
      span.appendChild(el('b', null, t.kind));
      span.appendChild(document.createTextNode(' ' + t.label + ' '));
      span.appendChild(el('em', null, t.value));
      span.appendChild(document.createTextNode(' '));
      span.appendChild(el('small', null, t.at));
      tickerTrack.appendChild(span);
    });
  }

  function paintTyping(names) {
    if (!typingRow) return;
    const list = (names || []).filter((n) => n && n !== me);
    if (!list.length) { typingRow.hidden = true; return; }
    typingRow.hidden = false;
    const text = list.length === 1 ? list[0] + ' is typing…'
      : list.length === 2 ? list.join(' and ') + ' are typing…'
        : list.length + ' people are typing…';
    if (typingText) typingText.textContent = text;
  }

  function paintConn(state, label) {
    if (!conn) return;
    conn.setAttribute('data-state', state);
    if (connText) connText.textContent = label;
  }

  function paintNext(onAir) {
    if (fightsOut && onAir && typeof onAir.open_fights === 'number') fightsOut.textContent = String(onAir.open_fights);
    if (!nextBox || !onAir) return;
    const draw = onAir.next_draw;
    if (!draw) return;
    nextIso = draw.iso;
    nextBox.setAttribute('href', '/bola/draws/' + draw.id);
    const strong = nextBox.querySelector('strong');
    if (strong) strong.textContent = draw.area + ' · ' + draw.at;
  }

  function tickCountdown() {
    if (!countdown || !nextIso) return;
    const target = new Date(nextIso).getTime();
    if (isNaN(target)) return;
    let secs = Math.max(0, Math.round((target - Date.now()) / 1000));
    const h = Math.floor(secs / 3600); secs -= h * 3600;
    const m = Math.floor(secs / 60); const s = secs - m * 60;
    const pad = (n) => String(n).padStart(2, '0');
    countdown.textContent = (h ? h + ':' : '') + pad(m) + ':' + pad(s);
    countdown.classList.toggle('is-soon', h === 0 && m < 10);
  }

  /* ---------------- polling ---------------- */
  async function poll() {
    if (polling || stopped || document.hidden) return;
    polling = true;
    try {
      const data = await api('/chat/messages?after=' + last, { method: 'GET' });
      failures = 0;
      paintConn('live', 'Live');
      const rows = data.messages || [];
      const wasPinned = atBottom();
      let foreign = 0;
      rows.forEach((m) => { add(m, true); if (m.user !== me) foreign += 1; });
      if (rows.length) {
        if (wasPinned) { toBottom(true); } else { unread += rows.length; showJump(); }
        if (foreign) chimeIncoming();
      }
      paintRoom(data.room);
      paintTicker(data.on_air && data.on_air.ticker);
      paintNext(data.on_air);
      paintTyping(data.typing);
    } catch (e) {
      failures += 1;
      paintConn(failures > 2 ? 'off' : 'wait', failures > 2 ? 'Reconnecting' : 'Catching up');
    } finally {
      polling = false;
    }
  }

  log.addEventListener('scroll', () => {
    pinned = atBottom();
    if (pinned) { unread = 0; if (jump) jump.hidden = true; }
    else if (unread) showJump();
  });
  if (jump) jump.addEventListener('click', () => toBottom(true));

  /* ---------------- composer ---------------- */
  function paintCount() {
    if (!counter) return;
    const n = field.value.length;
    counter.textContent = n + '/300';
    counter.classList.toggle('is-near', n >= 240 && n < 300);
    counter.classList.toggle('is-max', n >= 300);
  }
  let lastPing = 0;
  field.addEventListener('input', () => {
    paintCount();
    const now = Date.now();
    if (field.value && now - lastPing > 2500) {
      lastPing = now;
      api('/chat/typing', { method: 'POST', body: {} }).catch(() => {});
    }
  });
  field.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); }
  });
  root.querySelectorAll('[data-ls-phrase]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const phrase = btn.getAttribute('data-ls-phrase') || '';
      field.value = (field.value ? field.value.replace(/\s+$/, '') + ' · ' : '') + phrase;
      field.value = field.value.slice(0, 300);
      paintCount();
      field.focus();
    });
  });

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = field.value.trim();
    if (!body) { field.focus(); return; }
    form.classList.add('is-sending');
    field.value = '';
    paintCount();
    try {
      await api('/chat/send', { method: 'POST', body: { body: body } });
      chimeSent();
      await poll();
      toBottom(true);
    } catch (e) {
      field.value = body;
      paintCount();
      paintConn('off', 'Not sent');
    } finally {
      form.classList.remove('is-sending');
      field.focus();
    }
  });

  /* ---------------- lifecycle: one interval, no duplicates ---------------- */
  paintCount();
  toBottom(false);
  tickCountdown();
  const pollTimer = window.setInterval(poll, 4000);
  const clockTimer = window.setInterval(tickCountdown, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  window.addEventListener('pagehide', () => {
    stopped = true;
    window.clearInterval(pollTimer);
    window.clearInterval(clockTimer);
    if (audio) { audio.close().catch(() => {}); audio = null; }
  });
  poll();
})();
