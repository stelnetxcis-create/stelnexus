/* Declarator Desk (/staff/declarator).

   One live board of every game that needs a result, with the controls to deal with it in place.

   - The server sends the board as JSON (embedded for the first paint, then /_live/declarator every few seconds with
     an ETag, so an idle poll is a few header bytes). The board never contains money.
   - The LEFT rail lists one card per event (its current game) and per Bola draw, grouped by urgency lane.
   - The RIGHT stage works on the selected card: game strip, on-air / betting controls, a two-step result keypad
     (pick a result, then confirm), or the two balls for a Bola draw.
   - What you are half-way through (the armed result, the note, the chosen picture, typed balls) lives in `drafts`,
     not in the DOM, so a live update can redraw the stage without losing it.
   - Every action posts to /staff/declarator/... and the server re-checks permission and state. Hiding a button here
     is cosmetic; a stale or forged request is refused by the same services the Fight View uses.
   - All text that came from the server is escaped before it is put in the page. */
(function () {
  'use strict';
  const root = document.querySelector('[data-desk]');
  const bootEl = document.getElementById('dk-boot');
  if (!root || !bootEl) return;
  const snx = window.snx || {};
  const $ = (sel, scope) => (scope || root).querySelector(sel);
  const $$ = (sel, scope) => Array.from((scope || root).querySelectorAll(sel));
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const LANES = ['air', 'due', 'open', 'next'];
  const LANE_NAME = { air: 'On air', due: 'Result due', open: 'Betting open', next: 'Coming up' };
  const RES = { meron: 'MERON', wala: 'WALA', draw: 'DRAW', canceled: 'CANCELLED' };
  const POLL_MS = 4000;

  let data = JSON.parse(bootEl.textContent);
  let clockSkew = 0;                    // server time minus this browser's time, from the Date header
  let first = true;
  const st = { sel: null, filter: 'all', tab: 'live', view: {}, drafts: {}, busy: false, error: '' };
  const now = () => Date.now() + clockSkew;

  /* ---------------------------------------------------------------- small helpers */
  const timeFmt = new Intl.DateTimeFormat('en-PH', { hour: 'numeric', minute: '2-digit', timeZone: 'Asia/Manila' });
  const dayFmt = new Intl.DateTimeFormat('en-PH', { month: 'short', day: 'numeric', timeZone: 'Asia/Manila' });
  const clockFmt = new Intl.DateTimeFormat('en-PH', { hour: 'numeric', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Manila' });
  const baseTitle = document.title;
  const at = (ms) => (ms ? timeFmt.format(new Date(ms)) : '');
  function clock(totalSeconds) {
    const s = Math.max(0, Math.floor(totalSeconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const two = (n) => String(n).padStart(2, '0');
    return h ? h + ':' + two(m) + ':' + two(r) : two(m) + ':' + two(r);
  }
  function toast(message, level, extra) {
    if (snx.toast) snx.toast(message, Object.assign({ level: level || 'info', ttl: 5000 }, extra || {}));
  }
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const ballClass = (n) => 'c' + (n ? (Math.floor((n - 1) / 10) % 5) + 1 : 0);

  /* ---------------------------------------------------------------- the board */
  const keep = (c) => st.filter === 'all' || st.filter === c.kind;

  function cards() {
    const out = [];
    data.events.forEach((e) => {
      if (e.st === 'draft') {
        out.push({ key: 'e:' + e.id, kind: 'sabong', lane: 'next', sort: Date.parse(e.date) || 0, e });
      } else if (e.cur) {
        const g = e.games.find((x) => x.id === e.cur);
        if (g) out.push({ key: 'e:' + e.id, kind: 'sabong', lane: e.lane, sort: g.sa || g.lk || 0, e, g });
      }
    });
    data.draws.forEach((d) => out.push({ key: 'b:' + d.id, kind: 'bola', lane: d.lane, sort: d.at || 0, d }));
    return out.filter(keep).sort((a, b) => LANES.indexOf(a.lane) - LANES.indexOf(b.lane) || a.sort - b.sort);
  }
  const selected = () => cards().find((c) => c.key === st.sel) || null;

  function ensureSelection() {
    const all = cards();
    if (!all.some((c) => c.key === st.sel)) st.sel = all.length ? all[0].key : null;
  }

  function renderStats() {
    const all = cards();
    $('[data-stats]').innerHTML = LANES.map((lane) => {
      const n = all.filter((c) => c.lane === lane).length;
      return '<button type="button" class="dk-stat' + (n ? ' has' : '') + '" data-lane="' + lane + '" data-jump="' + lane +
        '"><i></i><b>' + n + '</b><span>' + LANE_NAME[lane] + '</span></button>';
    }).join('');
    const due = all.filter((c) => c.lane === 'due').length;
    document.title = (due ? '(' + due + ') ' : '') + baseTitle;          // a result is waiting, even in another tab
    $$('[data-tab-count="live"]').forEach((el) => { el.textContent = all.length; });
    $$('[data-tab-count="recent"]').forEach((el) => { el.textContent = recentRows().length; });
  }

  function cardHtml(c) {
    const sel = c.key === st.sel;
    const open = '<button type="button" class="dk-card" role="option" data-key="' + c.key + '" data-lane="' + c.lane +
      '" aria-selected="' + sel + '">';
    if (c.kind === 'bola') {
      const d = c.d, overdue = now() >= d.at;
      return open + '<div class="dk-card-top"><span>Bola</span><span class="t" data-until="' + d.at + '" data-pre="' +
        (overdue ? '' : 'in ') + '" data-done="due now"></span></div>' +
        '<h4>Slot ' + esc(d.slot) + ' <small>' + esc(d.venue || 'Area') + '</small></h4>' +
        '<div class="dk-mini"><span class="dk-chip is-ball">' + esc(at(d.at)) + '</span><span>' +
        (d.due ? 'Waiting for the two balls' : 'Draw time') + '</span></div></button>';
    }
    const e = c.e;
    if (e.st === 'draft') {
      return open + '<div class="dk-card-top"><span>Sabong</span><span class="t">Not open</span></div>' +
        '<h4>' + esc(e.title) + '</h4><div class="dk-mini">' + esc(e.date) + (e.venue ? ' · ' + esc(e.venue) : '') +
        '</div></button>';
    }
    const g = c.g;
    const timer = c.lane === 'air' && g.sa ? '<span class="t" data-since="' + g.sa + '" data-pre="on air "></span>'
      : c.lane === 'due' && g.lk ? '<span class="t" data-since="' + g.lk + '" data-pre="closed "></span>'
        : '<span class="t">pre-show</span>';
    const pct = e.total ? Math.round(e.done / e.total * 100) : 0;
    return open + '<div class="dk-card-top"><span>Sabong</span>' + timer + '</div>' +
      '<h4>Fight #' + esc(g.n) + ' <small>' + esc(e.title) + (e.venue ? ' · ' + esc(e.venue) : '') + '</small></h4>' +
      '<div class="dk-vs"><span class="m">' + esc(g.nm) + '</span><i>VS</i><span class="w">' + esc(g.nw) + '</span></div>' +
      '<div class="dk-prog" title="' + e.done + ' of ' + e.total + ' fights done"><i style="width:' + pct + '%"></i></div></button>';
  }

  function recentRows() {
    return (data.recent || []).filter((r) => st.filter === 'all' || (st.filter === 'bola') === (r.kind === 'bola'));
  }
  function recentHtml() {
    const rows = recentRows();
    if (!rows.length) return '<div class="dk-empty"><p>Nothing was declared in the last 24 hours.</p></div>';
    return rows.map((r) => {
      const href = r.kind === 'bola' ? '/bola/draws/' + r.id : '/events/' + r.ev + '?fight_id=' + r.id;
      const chip = r.kind === 'bola'
        ? '<span class="dk-chip ' + (r.res === 'void' ? 'res-void' : 'is-ball') + '">' + esc(r.res ? r.res.toUpperCase() : '—') + '</span>'
        : '<span class="dk-chip res-' + esc(r.res) + '">' + esc(RES[r.res] || r.res) + '</span>';
      const who = r.kind === 'bola' ? '' : esc(r.nm) + ' vs ' + esc(r.nw) + ' · ';
      return '<a class="dk-rec" href="' + esc(href) + '"><b>' + esc(r.ref) + '</b><span class="muted small">' + esc(r.title) +
        '</span>' + chip + '<small>' + who + (r.by ? 'by ' + esc(r.by) + ' · ' : '') + esc(at(r.at)) + '</small></a>';
    }).join('');
  }

  function renderRail() {
    const list = $('[data-list]');
    list.dataset.tab = st.tab;
    $$('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)));
    if (st.tab === 'recent') { list.innerHTML = recentHtml(); return; }
    const all = cards();
    if (!all.length) {
      list.innerHTML = '<div class="dk-empty"><p>Nothing is live right now. New fights show up here by themselves.</p></div>';
      return;
    }
    let html = '';
    LANES.forEach((lane) => {
      const mine = all.filter((c) => c.lane === lane);
      if (!mine.length) return;
      html += '<div class="dk-lane" data-lane="' + lane + '"><i></i>' + LANE_NAME[lane] + '<b>' + mine.length + '</b></div>';
      mine.forEach((c) => { html += cardHtml(c); });
    });
    list.innerHTML = html;
    tick();                                   // fill the timers now, not a second from now
  }

  /* ---------------------------------------------------------------- stage: shared pieces */
  const draft = (key) => (st.drafts[key] = st.drafts[key] || { armed: '', note: '', proof: null, thumb: '', b1: '', b2: '' });
  const errHtml = () => (st.error ? '<div class="dk-err" role="alert">' + esc(st.error) + '</div>' : '');
  const dis = () => (st.busy ? ' disabled' : '');
  root.addEventListener('input', (ev) => { if (ev.target.matches && ev.target.matches('[data-timer-secs]')) st.timerSecs = ev.target.value; });

  function emptyStage() {
    const links = '<div class="dk-actions"><a class="btn" href="/games/sabong/live">Sabong</a>' +
      (data.draws.length || data.can.bola ? '<a class="btn" href="/bola">Bola</a>' : '') + '</div>';
    return '<div class="dk-panel"><div class="dk-empty"><img src="/static/img/empty/arena.svg" alt="" width="150" height="110">' +
      '<h3>All clear</h3><p>No fight is waiting for you. The board updates by itself when one needs a result.</p>' + links + '</div></div>';
  }

  function proofField(d) {
    const label = d.proof
      ? '<img src="' + esc(d.thumb) + '" alt="Chosen picture"><span>' + esc(d.proof.name) + '<br><small class="muted">Tap to change</small></span>'
      : '<span>Add a picture of the result' + (data.proof_required ? ' <em>required</em>' : ' <small class="muted">(optional)</small>') + '</span>';
    return '<label><span>Picture</span><button type="button" class="dk-proof" data-act="proof">' + label + '</button></label>';
  }

  /* ---------------------------------------------------------------- stage: a sabong game */
  function viewGame(e) {
    const picked = st.view[e.id] && e.games.find((x) => x.id === st.view[e.id]);
    return picked || e.games.find((x) => x.id === e.cur) || e.games[e.games.length - 1] || null;
  }

  function beadClass(g, e) {
    const ended = g.st === 'settled' || g.st === 'void';
    let k = ended ? 'is-done res-' + (g.res || 'canceled')
      : g.sd ? 'is-air' : g.st === 'locked' ? 'is-locked' : g.ph === 'open' ? 'is-open' : 'is-wait';
    if (g.id === e.cur) k += ' is-cur';
    if (g.id === (viewGame(e) || {}).id) k += ' is-view';
    return 'dk-bead ' + k;
  }

  function resolve(g, a) {
    if (a === 'llamado' || a === 'dehado') {
      const side = a === 'llamado' ? g.lla : (g.lla === 'meron' ? 'wala' : 'meron');
      return { res: side, via: a.toUpperCase() };
    }
    return { res: a, via: '' };
  }
  const sideName = (g, side) => (side === 'meron' ? g.nm : g.nw);

  function summary(g, a) {
    const r = resolve(g, a);
    const tail = '<small>Payouts post immediately. A wrong result can only be changed by a correction.</small>';
    if (r.res === 'draw') return 'Declare Fight #' + esc(g.n) + ' a <em>DRAW</em>?<small>Draw bets win; Meron and Wala stakes are refunded.</small>' + tail;
    if (r.res === 'canceled') return 'Cancel Fight #' + esc(g.n) + '?<small>Every stake is refunded.</small>';
    return 'Declare <em>' + esc(sideName(g, r.res)) + '</em> (' + RES[r.res] + (r.via ? ' · ' + r.via : '') +
      ') the winner of Fight #' + esc(g.n) + '?' + tail;
  }

  function keypad(g, d) {
    const keys = [
      ['meron', 'M', 'MERON', g.nm], ['wala', 'W', 'WALA', g.nw],
      ['draw', 'D', 'DRAW', 'Draw bets win · Meron and Wala refunded'], ['canceled', 'X', 'CANCEL GAME', 'Refund every stake'],
    ];
    const btn = (id, hot, title, sub, cls) => '<button type="button" class="dk-key k-' + cls + '" data-arm="' + id +
      '" aria-pressed="' + (d.armed === id) + '"' + dis() + '>' + (hot ? '<kbd>' + hot + '</kbd>' : '') + '<b>' + title +
      '</b><span>' + esc(sub) + '</span></button>';
    let html = '<div class="dk-pad">' + keys.map((k) => btn(k[0], k[1], k[2], k[3], k[0])).join('') + '</div>';
    if (g.lla === 'meron' || g.lla === 'wala') {
      const dehado = g.lla === 'meron' ? 'wala' : 'meron';
      html += '<div class="dk-pad is-sub">' + btn('llamado', '', 'LLAMADO', sideName(g, g.lla), g.lla) +
        btn('dehado', '', 'DEHADO', sideName(g, dehado), dehado) + '</div>';
    }
    return html;
  }

  function confirmBox(g, d) {
    const r = resolve(g, d.armed);
    const blocked = data.proof_required && !d.proof;
    return '<div class="dk-confirm k-' + esc(r.res) + '" data-confirm>' +
      '<p class="dk-sum">' + summary(g, d.armed) + '</p>' +
      '<div class="dk-fields"><label><span>Note <small class="muted">(optional)</small></span>' +
      '<input type="text" data-f="note" maxlength="200" placeholder="e.g. confirmed on the monitor" value="' + esc(d.note) + '"></label>' +
      proofField(d) + '</div>' + errHtml() +
      '<div class="dk-go-row"><button type="button" class="btn btn-primary dk-go" data-act="declare"' + (blocked || st.busy ? ' disabled' : '') +
      '>' + (st.busy ? 'Working…' : 'Confirm &amp; settle') + '</button><button type="button" class="btn" data-act="clear"' + dis() + '>Clear</button>' +
      (blocked ? '<span class="dk-fine">Add the picture to continue.</span>' : '') + '</div></div>';
  }

  function controls(g, e) {
    const b = [];
    if (g.ph === 'open') b.push('<button type="button" class="btn btn-primary" data-act="start"' + dis() + '>Start fight (go on air)</button>');
    if (g.sd) b.push('<button type="button" class="btn" data-act="reset"' + dis() + '>Back to pre-show</button>');
    b.push(g.st === 'locked'
      ? '<button type="button" class="btn" data-act="unlock"' + dis() + '>Re-open betting</button>'
      : '<button type="button" class="btn btn-danger" data-act="lock"' + dis() + '>Close betting</button>');
    return '<div class="dk-panel"><h5>Fight controls</h5><div class="dk-actions">' + b.join('') + '</div>' +
      '<p class="dk-hint">Starting a fight closes betting and chat for it, and opens the next fight after it is declared.</p>' +
      (st.error && !draft('f' + g.id).armed ? errHtml() : '') + '</div>' + timerPanel(g) + balancerPanel(g);
  }

  /* Auto-close: close betting by itself after a time you set. Only while betting is open and the game is not on air. */
  function timerPanel(g) {
    if (g.st !== 'open' || g.sd) return '';
    const run = g.ac ? '<p class="dk-timer" role="status"><b>Closing betting in <span data-until="' + g.ac + '" data-done="closing now">--:--</span></b>' +
      '<button type="button" class="btn btn-sm" data-act="timer-cancel"' + dis() + '>Cancel timer</button></p>' : '';
    const picks = [30, 60, 90, 120].map((n) => '<button type="button" class="btn btn-sm" data-act="timer" data-secs="' + n + '"' + dis() + '>' + (n < 60 ? n + 's' : n / 60 + ' min') + '</button>').join('');
    return '<div class="dk-panel"><h5>Close betting automatically</h5>' + run +
      '<div class="dk-actions">' + picks +
      '<span class="dk-custom"><input type="number" inputmode="numeric" min="5" max="3600" placeholder="seconds" value="' + esc(st.timerSecs || '') + '" aria-label="Seconds until betting closes" data-timer-secs>' +
      '<button type="button" class="btn btn-sm" data-act="timer-custom"' + dis() + '>Set</button></span></div>' +
      '<p class="dk-hint">Betting closes by itself when the time is up. You can still close it yourself earlier, or cancel the timer.</p></div>';
  }

  /* Emergency manual balancer: stop bets on one side so the other side can catch up. Use only when the automatic
     balancing is not enough. Blocking Meron leaves Wala open (and the other way round). */
  function balancerPanel(g) {
    if (g.st !== 'open' || g.sd) return '';
    const mBlocked = g.bm === 'wala', wBlocked = g.bm === 'meron';
    const btn = (side, blocked) => '<button type="button" class="btn ' + (blocked ? 'btn-danger' : '') + '" data-act="' + (blocked ? 'unblock' : 'block-' + side) + '"' + dis() + '>' +
      (blocked ? 'Unblock ' : 'Block ') + side.charAt(0).toUpperCase() + side.slice(1) + '</button>';
    const status = mBlocked ? 'MERON is blocked. Only WALA takes bets.' : wBlocked ? 'WALA is blocked. Only MERON takes bets.' : 'Both sides are open.';
    return '<div class="dk-panel' + (g.bm !== 'all' ? ' is-alert' : '') + '"><h5>Emergency balancer</h5>' +
      '<p class="dk-hint" role="status"><b>' + esc(status) + '</b></p>' +
      '<div class="dk-actions">' + btn('meron', mBlocked) + btn('wala', wBlocked) + '</div>' +
      '<p class="dk-hint">Only if the automatic balancing is not working. The blocked side cannot be pressed by players; the other side can.</p></div>';
  }

  function phaseOf(g, e) {
    if (g.st === 'settled' || g.st === 'void') return { lane: '', text: 'Declared' };
    if (g.id !== e.cur) return { lane: 'next', text: 'Up next' };
    if (g.sd) return { lane: 'air', text: 'On air', since: g.sa, pre: '' };
    if (g.st === 'locked') return { lane: 'due', text: 'Betting closed', since: g.lk, pre: '' };
    return { lane: 'open', text: 'Pre-show' };
  }

  function verdict(g, e) {
    const colour = g.res === 'meron' ? 'var(--meron)' : g.res === 'wala' ? 'var(--wala)' : g.res === 'draw' ? 'var(--purple)' : '#a59d86';
    const label = g.res === 'meron' || g.res === 'wala' ? esc(sideName(g, g.res)) + ' wins'
      : g.res === 'draw' ? 'Draw' : 'Fight cancelled';
    return '<div class="dk-verdict" style="--wc:' + colour + '"><b>' + label + '</b><span>declared ' + esc(at(g.at)) + '</span>' +
      (g.cam ? '<a href="/fights/' + g.id + '/proof" target="_blank" rel="noopener">Result picture</a>' : '') +
      '<a href="/events/' + e.id + '?fight_id=' + g.id + '">Open fight</a></div>';
  }

  /* Spectate: the Fight View (live video, pools, Meron / Wala) in a pop-out window next to the desk. One window is
     reused; while it is open it follows the event's current game, so the declarator is always watching what they will
     have to declare. */
  const watchUrl = (e, g) => '/events/' + e.id + '?fight_id=' + g.id;
  const follow = { win: null, ev: 0, game: 0 };
  const watching = () => !!(follow.win && !follow.win.closed);
  function openSpectate(e, g) {
    if (!g) return;
    const url = watchUrl(e, g);
    if (watching()) { follow.win.location.href = url; follow.win.focus(); }
    else follow.win = window.open(url, 'snx-spectate', 'popup=yes,width=1180,height=780');
    if (!follow.win) { toast('Your browser blocked the pop-up. Allow pop-ups for this site, then press S again.', 'warning'); return; }
    follow.ev = e.id; follow.game = g.id;
  }
  function followCurrent() {
    if (!watching()) return;
    const e = data.events.find((x) => x.id === follow.ev), g = e && e.games.find((x) => x.id === e.cur);
    if (!g || g.id === follow.game) return;
    follow.game = g.id;
    follow.win.location.href = watchUrl(e, g);
    toast('Spectate window moved to Fight #' + g.n + '.', 'info', { ttl: 4000 });
  }

  function sabongStage(c) {
    const e = c.e;
    if (e.st === 'draft') return draftStage(e);
    const g = viewGame(e);
    if (!g) return emptyStage();
    const isCur = g.id === e.cur;
    const ended = g.st === 'settled' || g.st === 'void';
    const ph = phaseOf(g, e);
    const d = draft('f' + g.id);
    const win = ended && (g.res === 'meron' || g.res === 'wala') ? g.res : '';
    const lbl = (side) => (g.lla === side ? 'LLAMADO' : g.lla ? 'DEHADO' : '');
    const pill = '<span class="dk-pill"' + (ph.lane ? ' data-lane="' + ph.lane + '"' : '') + '><i></i>' + ph.text +
      (ph.since ? ' <time data-since="' + ph.since + '"></time>' : '') + '</span>';

    let html = '<div class="dk-panel"><div class="dk-head"><div><span class="dk-kicker">' + esc(e.venue || 'Sabong') + ' · ' + esc(e.date) +
      '</span><h3>' + esc(e.title) + '</h3></div><div class="dk-head-r">' + pill +
      '<button type="button" class="btn btn-sm dk-watch' + (watching() && follow.game === g.id ? ' is-on' : '') + '" data-act="watch" title="Open the live video and pools in a pop-out window">' +
      '<span aria-hidden="true">&#9654;</span> Watch live <kbd>S</kbd></button></div></div>' +
      '<div class="dk-duel' + (ended ? ' has-result' : '') + '">' +
      '<div class="dk-side m' + (win === 'meron' ? ' is-win' : '') + '" style="--wc:var(--meron)"><small>MERON</small><strong>' + esc(g.nm) + '</strong>' +
      (lbl('meron') ? '<span class="dk-tag">' + lbl('meron') + '</span>' : '') + '</div>' +
      '<div class="dk-mid"><div class="dk-medal"><small>GAME</small><b>' + esc(g.n) + '</b></div></div>' +
      '<div class="dk-side w' + (win === 'wala' ? ' is-win' : '') + '" style="--wc:var(--wala)"><small>WALA</small><strong>' + esc(g.nw) + '</strong>' +
      (lbl('wala') ? '<span class="dk-tag">' + lbl('wala') + '</span>' : '') + '</div></div>' +
      (ended ? verdict(g, e) : '') +
      '<div><div class="dk-beads" data-beads>' + e.games.map((x) => '<button type="button" class="' + beadClass(x, e) + '" data-bead="' + x.id +
        '" title="Fight ' + esc(x.n) + (x.res ? ' · ' + (RES[x.res] || x.res) : '') + '">' + esc(x.n) + '</button>').join('') + '</div>' +
      '<div class="dk-legend"><span style="--bc:var(--green)"><i></i>pre-show</span><span style="--bc:var(--air);--fill:var(--air)"><i></i>on air</span>' +
      '<span style="--bc:var(--amber)"><i></i>betting closed</span><span style="--bc:#b82a3d;--fill:#b82a3d"><i></i>Meron</span>' +
      '<span style="--bc:#2f7fd1;--fill:#2f7fd1"><i></i>Wala</span><span style="--bc:#8266d6;--fill:#8266d6"><i></i>Draw</span>' +
      '<span style="--bc:#6c675a;--fill:#6c675a"><i></i>Cancelled</span></div></div></div>';

    if (!isCur && !ended) {
      const cur = e.games.find((x) => x.id === e.cur);
      html += '<div class="dk-warn">Fight #' + esc(g.n) + ' opens after Fight #' + esc(cur ? cur.n : '?') + ' is done, so it cannot be declared yet.' +
        (cur ? '<button type="button" class="btn btn-sm" data-bead="' + cur.id + '">Go to Fight #' + esc(cur.n) + '</button>' : '') + '</div>';
    }
    if (isCur && !ended && data.can.lock) html += controls(g, e);
    if (isCur && !ended) {
      if (!data.can.declare) {
        html += '<div class="dk-panel"><p class="dk-hint">Your account can follow this fight but not declare it.</p></div>';
      } else {
        html += '<div class="dk-panel"><h5>Declare the result</h5>' + keypad(g, d) + (d.armed ? confirmBox(g, d) : (
          '<p class="dk-hint">Pick the result, then confirm. Nothing is settled until you press Confirm.</p>')) + '</div>';
      }
    }
    if (data.can.open_event) html += eventPanel();
    return html;
  }

  /* Close the whole event: only offered to accounts that may open events; the server refuses while bets are unsettled. */
  function eventPanel() {
    return '<div class="dk-panel"><h5>Event</h5><div class="dk-actions"><button type="button" class="btn" data-act="closeevent"' + dis() + '>Close event</button></div>' +
      '<p class="dk-hint">Closing the event stops new fights. Every fight with bets must be declared first.</p></div>';
  }

  function draftStage(e) {
    const can = data.can.open_event;
    return '<div class="dk-panel"><div class="dk-head"><div><span class="dk-kicker">' + esc(e.venue || 'Sabong') + ' · ' + esc(e.date) +
      '</span><h3>' + esc(e.title) + '</h3></div><span class="dk-pill" data-lane="next"><i></i>Not open yet</span></div>' +
      '<p class="dk-hint">This event is still a draft, so it has no live fight. Open it when the show is about to start.</p>' +
      '<div class="dk-actions">' + (can ? '<button type="button" class="btn btn-primary" data-act="openevent"' + dis() + '>Open event</button>'
        : '<span class="dk-hint">A Master or the Admin opens this event.</span>') + '</div>' + errHtml() + '</div>';
  }

  /* ---------------------------------------------------------------- stage: a Bola draw */
  function ballsError(d) {
    const a = parseInt(d.b1, 10), b = parseInt(d.b2, 10), max = data.bola_max;
    if (!d.b1 || !d.b2) return '';
    if (!(a >= 1 && a <= max && b >= 1 && b <= max)) return 'Each ball must be between 1 and ' + max + '.';
    if (a === b && !data.bola_dupes) return 'The two balls must be different numbers.';
    return '';
  }
  const ballsReady = (d) => !!(d.b1 && d.b2) && !ballsError(d);

  /* Open / close betting on a draw: Declarators and managers only. */
  function bolaControls(dw) {
    if (!data.can.bola_open) return '';
    const open = dw.st === 'open';
    if (!open && dw.st !== 'scheduled' && dw.st !== 'locked') return '';
    return '<div class="dk-panel"><h5>Draw betting</h5><div class="dk-actions">' + (open
      ? '<button type="button" class="btn btn-danger" data-act="bola-lock"' + dis() + '>Close betting</button>'
      : '<button type="button" class="btn btn-primary" data-act="bola-open"' + dis() + '>Open betting</button>') + '</div>' +
      (st.error && !draft('b' + dw.id).b1 ? errHtml() : '') + '</div>';
  }

  function bolaStage(c) {
    const dw = c.d, d = draft('b' + dw.id);
    const due = now() >= dw.at, early = !due && data.early_ok;
    const locked = (!due && !early) || !data.can.bola;
    const lane = dw.due ? 'due' : 'next';
    const ball = (n, key, label) => '<div class="dk-ball ' + ballClass(parseInt(d[key], 10)) + '" data-ball="' + key + '"><input type="text" inputmode="numeric" pattern="[0-9]*" maxlength="2" ' +
      'data-f="' + key + '" aria-label="' + label + '" placeholder="–" value="' + esc(d[key]) + '"' + (locked ? ' disabled' : '') + '><label>' + label + '</label></div>';
    return '<div class="dk-panel" data-bola data-at="' + dw.at + '" data-due="' + (due ? 1 : 0) + '"><div class="dk-head"><div><span class="dk-kicker">' + esc(dw.venue || 'Bola') +
      ' · draw at ' + esc(at(dw.at)) + '</span><h3>Slot ' + esc(dw.slot) + ' draw</h3></div>' +
      '<div class="dk-head-r"><span class="dk-pill" data-lane="' + lane + '"><i></i>' + (dw.due ? 'Result due' : 'Draw in') + ' <time data-until="' + dw.at + '" data-done="now"></time></span>' +
      '<a class="btn btn-sm" href="/bola/draws/' + dw.id + '" title="Open the full draw page">Draw page</a></div></div>' +
      (!data.can.bola ? '<p class="dk-hint">Your account can follow this draw but not declare it.</p>'
        : !due && !early ? '<div class="dk-warn">The draw time has not come yet. You can enter the balls from ' + esc(at(dw.at)) + '.</div>'
          : early ? '<div class="dk-warn">This draw has not reached its time. You may declare early, but you must write the reason.</div>' : '') +
      '<div class="dk-balls">' + ball(1, 'b1', 'First ball (tumbok)') + ball(2, 'b2', 'Second ball (sahod)') + '</div>' +
      '<div class="dk-confirm" data-bola-confirm' + (locked ? ' hidden' : '') + '><p class="dk-sum" data-sum></p>' +
      '<div class="dk-fields"><label><span>' + (early ? 'Reason <em style="font-style:normal;color:var(--red)">required</em>' : 'Note <small class="muted">(optional)</small>') +
      '</span><input type="text" data-f="note" maxlength="200" placeholder="' + (early ? 'why this draw is declared early' : 'e.g. read from the draw sheet') + '" value="' + esc(d.note) + '"></label>' +
      proofField(d) + '</div><div class="dk-err" role="alert" data-err' + (st.error ? '' : ' hidden') + '>' + esc(st.error) + '</div>' +
      '<div class="dk-go-row"><button type="button" class="btn btn-primary dk-go" data-act="bola-declare" disabled>Confirm &amp; settle</button>' +
      '<button type="button" class="btn" data-act="bola-clear"' + dis() + '>Clear</button></div></div></div>' + bolaControls(dw);
  }

  /* Update the Bola stage in place while typing (a redraw would drop the cursor). */
  function paintBola() {
    const c = selected();
    const panel = $('[data-bola]');
    if (!c || c.kind !== 'bola' || !panel) return;
    const d = draft('b' + c.d.id);
    ['b1', 'b2'].forEach((k) => {
      const el = $('[data-ball="' + k + '"]', panel);
      if (el) el.className = 'dk-ball ' + ballClass(parseInt(d[k], 10));
    });
    const err = ballsError(d);
    const due = now() >= c.d.at;
    const needReason = !due && data.early_ok && (d.note || '').trim().length < 5;
    const blocked = !ballsReady(d) || needReason || (data.proof_required && !d.proof) || st.busy;
    const sum = $('[data-sum]', panel);
    if (sum) {
      sum.innerHTML = ballsReady(d)
        ? 'Declare <em>' + esc(parseInt(d.b1, 10)) + ' / ' + esc(parseInt(d.b2, 10)) + '</em> for ' + esc(c.d.venue || 'this area') + ' · Slot ' + esc(c.d.slot) + '?' +
          '<small>Order matters: first ball is the tumbok, second the sahod. Payouts post immediately.</small>'
        : 'Enter both balls to continue.';
    }
    const e = $('[data-err]', panel);
    if (e) { e.hidden = !(err || st.error); e.textContent = err || st.error; }
    const go = $('[data-act="bola-declare"]', panel);
    if (go) { go.disabled = blocked; go.textContent = st.busy ? 'Working…' : 'Confirm & settle'; }
  }

  /* ---------------------------------------------------------------- stage: render with focus kept */
  let stageSig = '';
  function renderStage(force) {
    const stage = $('[data-stage]');
    const c = selected();
    const sig = c ? c.key + '|' + JSON.stringify(c.e || c.d) + '|' + st.error + '|' + st.busy + '|' + JSON.stringify(st.view) : 'none';
    if (!force && sig === stageSig) return;
    stageSig = sig;
    const active = document.activeElement;
    const focusKey = active && stage.contains(active) ? active.getAttribute('data-f') : null;
    const range = focusKey && active.selectionStart != null ? [active.selectionStart, active.selectionEnd] : null;
    stage.innerHTML = !c ? emptyStage() : c.kind === 'bola' ? bolaStage(c) : sabongStage(c);
    if (c && c.kind === 'bola') paintBola();
    if (focusKey) {
      const el = $('[data-f="' + focusKey + '"]', stage);
      if (el && !el.disabled) { el.focus(); if (range) try { el.setSelectionRange(range[0], range[1]); } catch (x) { /* number-like input */ } }
    }
    const cur = $('.dk-bead.is-view', stage);
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    tick();
  }

  function render() {
    ensureSelection();
    renderStats();
    renderRail();
    renderStage(false);
  }

  /* ---------------------------------------------------------------- actions */
  async function post(url, fields, proof) {
    let body;
    if (proof !== undefined) {
      body = new FormData();
      Object.keys(fields).forEach((k) => body.append(k, fields[k]));
      if (proof) body.append('proof', proof, proof.name);
    } else {
      body = new URLSearchParams(fields);
    }
    const r = await fetch(url, { method: 'POST', body, credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf(), Accept: 'application/json' } });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.detail || ('That did not work (' + r.status + '). Reload the page and try again.'));
    return out;
  }

  async function run(job, after) {
    if (st.busy) return;
    st.busy = true; st.error = '';
    renderStage(true); paintBola();
    try {
      const out = await job();
      toast(out.message, out.level || 'success');
      if (after) after();
    } catch (err) {
      st.error = err.message || 'Something went wrong.';
    }
    st.busy = false;
    await poll(true);
    renderStage(true); paintBola();
  }

  function twoTap(btn, go) {
    if (btn.classList.contains('is-armed')) { clearTimeout(btn._t); go(); return; }
    btn.dataset.label = btn.textContent;
    btn.textContent = 'Tap again to confirm';
    btn.classList.add('is-armed');
    btn._t = setTimeout(() => { btn.classList.remove('is-armed'); btn.textContent = btn.dataset.label; }, 3500);
  }

  function chooseProof(key) {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/jpeg,image/png,image/webp';
    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const d = draft(key);
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) { st.error = 'Use a JPG, PNG or WebP picture.'; renderStage(true); return; }
      if (file.size > data.max_upload) { st.error = 'That picture is too large (max ' + Math.round(data.max_upload / 1048576) + ' MB).'; renderStage(true); return; }
      if (d.thumb) URL.revokeObjectURL(d.thumb);
      d.proof = file; d.thumb = URL.createObjectURL(file); st.error = '';
      renderStage(true);
    });
    input.click();
  }

  function onStageClick(ev) {
    const t = ev.target.closest('[data-arm],[data-act],[data-bead]');
    if (!t || st.busy) return;
    const c = selected();
    if (!c) return;

    if (t.dataset.bead) {                                   // look at another game of this event
      const id = Number(t.dataset.bead);
      if (id === c.e.cur) delete st.view[c.e.id]; else st.view[c.e.id] = id;
      st.error = '';
      renderStage(true);
      return;
    }
    if (c.kind === 'sabong' && t.dataset.act === 'watch') {   // before the block below, which returns on any act it does not know
      openSpectate(c.e, viewGame(c.e));
      render();
      return;
    }
    if (c.kind === 'sabong' && c.e.st !== 'draft') {
      const g = viewGame(c.e), d = g ? draft('f' + g.id) : null;
      if (t.dataset.arm) { d.armed = d.armed === t.dataset.arm ? '' : t.dataset.arm; st.error = ''; renderStage(true); return; }
      switch (t.dataset.act) {
        case 'clear': d.armed = ''; st.error = ''; renderStage(true); return;
        case 'proof': chooseProof('f' + g.id); return;
        case 'start': return twoTap(t, () => run(() => post('/staff/declarator/events/' + c.e.id + '/stream', { action: 'start', fight_id: g.id })));
        case 'reset': return twoTap(t, () => run(() => post('/staff/declarator/events/' + c.e.id + '/stream', { action: 'reset', fight_id: g.id })));
        case 'lock': return twoTap(t, () => run(() => post('/staff/declarator/fights/' + g.id + '/lock', { locked: '1' })));
        case 'unlock': return twoTap(t, () => run(() => post('/staff/declarator/fights/' + g.id + '/lock', { locked: '0' })));
        case 'block-meron': return twoTap(t, () => run(() => post('/staff/declarator/fights/' + g.id + '/block', { side: 'meron' })));
        case 'block-wala': return twoTap(t, () => run(() => post('/staff/declarator/fights/' + g.id + '/block', { side: 'wala' })));
        case 'unblock': return run(() => post('/staff/declarator/fights/' + g.id + '/block', { side: 'none' }));
        case 'timer': return run(() => post('/staff/declarator/fights/' + g.id + '/timer', { seconds: t.dataset.secs }));
        case 'timer-custom': { const v = $('[data-timer-secs]'); return run(() => post('/staff/declarator/fights/' + g.id + '/timer', { seconds: v ? v.value : '' })); }
        case 'timer-cancel': return run(() => post('/staff/declarator/fights/' + g.id + '/timer', { seconds: '0' }));
        case 'declare':
          if (!d.armed) return;
          return run(() => post('/staff/declarator/fights/' + g.id + '/declare', { result: d.armed, reason: d.note }, d.proof || null),
            () => { delete st.drafts['f' + g.id]; delete st.view[c.e.id]; });
        default: return;
      }
    }
    if (c.kind === 'sabong' && t.dataset.act === 'closeevent') {
      twoTap(t, () => run(() => post('/staff/declarator/events/' + c.e.id + '/close', {})));
      return;
    }
    if (c.kind === 'sabong' && t.dataset.act === 'openevent') {
      twoTap(t, () => run(() => post('/staff/declarator/events/' + c.e.id + '/open', {})));
      return;
    }
    if (c.kind === 'bola') {
      const d = draft('b' + c.d.id);
      if (t.dataset.act === 'proof') { chooseProof('b' + c.d.id); return; }
      if (t.dataset.act === 'bola-open') { twoTap(t, () => run(() => post('/staff/declarator/bola/' + c.d.id + '/open', {}))); return; }
      if (t.dataset.act === 'bola-lock') { twoTap(t, () => run(() => post('/staff/declarator/bola/' + c.d.id + '/lock', {}))); return; }
      if (t.dataset.act === 'bola-clear') { d.b1 = ''; d.b2 = ''; d.note = ''; st.error = ''; renderStage(true); return; }
      if (t.dataset.act === 'bola-declare' && ballsReady(d)) {
        run(() => post('/staff/declarator/bola/' + c.d.id + '/declare',
          { ball_first: parseInt(d.b1, 10), ball_second: parseInt(d.b2, 10), reason: d.note }, d.proof || null),
        () => { delete st.drafts['b' + c.d.id]; });
      }
    }
  }

  function onStageInput(ev) {
    const f = ev.target.getAttribute && ev.target.getAttribute('data-f');
    const c = selected();
    if (!f || !c) return;
    if (c.kind === 'bola') {
      const d = draft('b' + c.d.id);
      if (f === 'note') d.note = ev.target.value;
      else { ev.target.value = ev.target.value.replace(/\D+/g, '').slice(0, 2); d[f] = ev.target.value; st.error = ''; }
      paintBola();
    } else if (c.e.st !== 'draft') {
      const g = viewGame(c.e);
      if (g && f === 'note') draft('f' + g.id).note = ev.target.value;
    }
  }

  /* ---------------------------------------------------------------- rail, tabs, filter, keyboard */
  function select(key, scroll) {
    st.sel = key; st.error = '';
    renderStats(); renderRail(); renderStage(true);
    tick();
    if (scroll && window.matchMedia && window.matchMedia('(max-width: 900px)').matches) {
      $('[data-stage]').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    if (snx.Sound && snx.Sound.play) snx.Sound.play('tick');
  }

  $('[data-list]').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-key]');
    if (b) select(b.dataset.key, true);
  });
  $('[data-stats]').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-jump]');
    if (!b) return;
    const hit = cards().find((c) => c.lane === b.dataset.jump);
    st.tab = 'live';
    if (hit) select(hit.key, true); else renderRail();
  });
  $$('[data-tab]').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; renderRail(); }));
  $$('[data-filter]').forEach((b) => b.addEventListener('click', () => {
    st.filter = b.dataset.filter;
    $$('[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    render();
  }));
  const stageEl = $('[data-stage]');
  stageEl.addEventListener('click', onStageClick);
  stageEl.addEventListener('input', onStageInput);

  document.addEventListener('keydown', (ev) => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) { if (ev.key === 'Escape') a.blur(); return; }
    const key = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
    const press = (sel) => { const b = $(sel); if (b && !b.disabled) { ev.preventDefault(); b.click(); } };
    if (key === 'ArrowDown' || key === 'ArrowUp' || key === 'j' || key === 'k') {
      const all = cards();
      if (!all.length) return;
      const i = Math.max(0, all.findIndex((c) => c.key === st.sel));
      const next = all[(i + (key === 'ArrowDown' || key === 'j' ? 1 : all.length - 1)) % all.length];
      ev.preventDefault();
      select(next.key, false);
      return;
    }
    if (key === 's') press('[data-act="watch"]');
    else if (key === 'n') {                                 // jump to the next game waiting for a result
      const dueNow = cards().filter((c) => c.lane === 'due');
      if (dueNow.length) { ev.preventDefault(); select(dueNow[(dueNow.findIndex((c) => c.key === st.sel) + 1) % dueNow.length].key, false); }
    } else if (key === 'm') press('[data-arm="meron"]');
    else if (key === 'w') press('[data-arm="wala"]');
    else if (key === 'd') press('[data-arm="draw"]');
    else if (key === 'x') press('[data-arm="canceled"]');
    else if (key === 'Escape') press('[data-act="clear"],[data-act="bola-clear"]');
    else if (key === 'Enter' && (!a || a === document.body)) press('[data-act="declare"],[data-act="bola-declare"]');
  });

  /* ---------------------------------------------------------------- timers */
  function tick() {
    const t = now();
    const wall = $('[data-clock]');
    if (wall) wall.textContent = clockFmt.format(new Date(t));
    $$('[data-since]').forEach((el) => { el.textContent = (el.dataset.pre || '') + clock((t - Number(el.dataset.since)) / 1000); });
    $$('[data-until]').forEach((el) => {
      const left = (Number(el.dataset.until) - t) / 1000;
      el.textContent = left > 0 ? (el.dataset.pre || '') + clock(left) : (el.dataset.done || 'due now');
    });
    const panel = $('[data-bola][data-due="0"]');       // the draw time just arrived: unlock the balls
    if (panel && Number(panel.dataset.at) <= t) renderStage(true);
  }
  setInterval(tick, 1000);

  /* ---------------------------------------------------------------- live poll */
  const liveDot = $('[data-live]');
  let etag = '', failures = 0, timer = 0, stopped = false;

  function apply(next) {
    const before = new Set(cards().filter((c) => c.lane === 'due').map((c) => c.key));
    data = next;
    ensureSelection();
    if (!first) {
      cards().filter((c) => c.lane === 'due' && !before.has(c.key)).forEach((c) => {
        const what = c.kind === 'bola' ? 'Bola Slot ' + c.d.slot + (c.d.venue ? ' · ' + c.d.venue : '')
          : 'Fight #' + c.g.n + ' · ' + c.e.title;
        toast(what + ' needs a result.', 'warning', { ttl: 12000, action: { label: 'Open', run: () => select(c.key, true) } });
        if (snx.Sound && snx.Sound.play) snx.Sound.play('lock');
      });
    }
    first = false;
    render();
    followCurrent();
  }

  async function poll(force) {
    clearTimeout(timer);
    if (stopped || (document.hidden && !force)) return;
    try {
      const headers = { Accept: 'application/json' };
      if (etag && !force) headers['If-None-Match'] = etag;
      const r = await fetch('/_live/declarator', { headers, credentials: 'same-origin', cache: 'no-store' });
      const stamp = Date.parse(r.headers.get('Date') || '');
      if (!isNaN(stamp)) clockSkew = stamp - Date.now();
      if (r.redirected || r.status === 401 || r.status === 403) {
        stopped = true; liveDot.classList.add('is-off');
        toast('You were signed out or lost access. Reload the page.', 'danger', { ttl: 20000 });
        return;
      }
      if (r.status !== 304) {
        if (!r.ok) throw new Error(String(r.status));
        const next = await r.json();
        etag = r.headers.get('ETag') || '';
        if (!next.same) apply(next);
      }
      failures = 0; liveDot.classList.remove('is-off');
    } catch (err) {
      failures += 1;
      if (failures > 1) liveDot.classList.add('is-off');
    }
    if (!stopped && !document.hidden) timer = setTimeout(poll, POLL_MS * Math.min(1 + failures, 6));
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(false); });

  /* ---------------------------------------------------------------- go */
  render();
  tick();
  first = false;           // the embedded board is the first paint, not a "new" arrival
  poll(true);
})();
