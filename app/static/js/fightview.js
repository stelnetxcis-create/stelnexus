/* Fight view glue: the game strip, switching games without reloading the video, and the one live poll that feeds
   the strip and the bet panel.

   - The strip is rendered by the server (so it works at first paint); this file recolours it from /_live/event/ID
     every few seconds and adds chips for games created while you watch.
   - Tapping a chip switches game in place: the URL changes (back button works), the video keeps running, the chat
     re-points at the new room (snx.fightChat.switchFight), the bet panel follows (select), and the "fight-panel" box
     (pool, declare form, tickets) is refreshed through the page's partial-refresh code (StelNexus.refreshRegion).
   - When the game on screen changes state (locked, declared) the same box is refreshed, as the old games grid did. */
(function () {
  'use strict';
  const strip = document.querySelector('[data-fv-strip]');
  const stage = document.querySelector('[data-stage]');
  if (!strip || !stage) return;
  const snx = window.snx || {};
  const eventId = Number(strip.dataset.event);
  const player = stage.querySelector('[data-player]');
  let activeId = Number(strip.dataset.active) || 0;
  let fights = [], hide = false, keys = new Map(), first = true;

  const bpEl = document.querySelector('[data-bp]');
  const bp = bpEl && snx.betPanel ? snx.betPanel(bpEl, { onPick: (id) => go(id, true) }) : null;
  if (bp) bp.select(activeId);

  // pre-show / live: the status pill, the Start-stream button and the early-bet panel all follow the poll
  const liveBar = document.querySelector('[data-fv-live]');
  let lastLive = null;
  if (bp && liveBar) bp.setLive(liveBar.dataset.live === '1');   // server state at first paint, before the first poll
  function setLive(on) {
    if (bp) bp.setLive(on);
    if (on !== lastLive) { if (lastLive !== null) document.dispatchEvent(new CustomEvent('snx:live', { detail: { on } })); lastLive = on; }
    if (!liveBar || (liveBar.dataset.live === '1') === on) return;
    liveBar.dataset.live = on ? '1' : '0';
    const t = liveBar.querySelector('[data-fv-live-text]');
    if (t) t.textContent = on ? 'Stream is live \u00B7 early betting closed' : 'Pre-show \u00B7 early betting open';
    liveBar.querySelectorAll('[data-fv-live-form]').forEach((f) => { f.hidden = (f.dataset.fvLiveForm === 'start') === on; });
  }
  const key = (f) => (f.res ? 'res:' + f.res : 'st:' + f.st);
  const ended = (f) => !!f.res || f.st === 'settled' || f.st === 'void';
  const url = (id) => '/change_name?event_id=' + eventId + '&fight_id=' + id;
  const chipOf = (id) => strip.querySelector('[data-fight="' + id + '"]');

  function paintChip(a, f) {
    a.className = 'fv-chip st-' + f.st + (f.res ? ' res-' + f.res : '') + (f.id === activeId ? ' is-active' : '') + (f.hot ? ' is-hot' : '');
    a.dataset.n = f.n; a.dataset.m = f.nm; a.dataset.w = f.nw; a.dataset.st = f.st; a.dataset.res = f.res || '';
    a.title = 'Game ' + f.n + ' · ' + f.nm + ' vs ' + f.nw + ' · ' + (f.res || f.st).toUpperCase();
    if (f.id === activeId) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
  }

  function renderStrip() {
    const list = strip.querySelector('[data-fv-list]') || strip;
    const have = new Set();
    fights.forEach((f) => {
      let a = chipOf(f.id);
      if (!a) {                                   // a game created while watching
        a = document.createElement('a');
        a.dataset.fight = f.id; a.href = url(f.id);
        const b = document.createElement('b'); b.textContent = f.n; a.appendChild(b);
        const nodes = Array.from(list.querySelectorAll('[data-fight]')).filter((x) => Number(x.dataset.n) > f.n);
        list.insertBefore(a, nodes[0] || null);
      }
      paintChip(a, f); have.add(String(f.id));
    });
    Array.from(list.querySelectorAll('[data-fight]')).forEach((a) => { if (!have.has(a.dataset.fight)) a.remove(); });
  }

  function centerActive() {
    const on = strip.querySelector('.is-active');
    if (on && on.scrollIntoView) { try { on.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' }); } catch (e) { on.scrollIntoView(); } }
  }

  async function refreshPanel(force) {
    const api = window.StelNexus;
    if (!api || !api.refreshRegion) { location.reload(); return; }
    const r = await api.refreshRegion('fight-panel', { force: !!force });
    if (r === 'failed') location.reload();
    else if (r === 'skipped' && snx.toast) snx.toast('This game changed.', { level: 'info', ttl: 12000, action: { label: 'Refresh', run: () => refreshPanel(true) } });
  }

  function go(id, push) {
    const f = fights.find((x) => x.id === id);
    if (!f || id === activeId) return;
    activeId = id;
    if (push !== false) { try { history.pushState({ fv: id }, '', url(id)); } catch (e) { /* ignore */ } }
    fights.forEach((g) => { const a = chipOf(g.id); if (a) paintChip(a, g); });
    centerActive();
    if (player) player.dispatchEvent(new CustomEvent('snx:game', { detail: { n: f.n, left: f.nm, right: f.nw, ended: ended(f) } }));
    if (snx.fightChat) snx.fightChat.switchFight(f);
    if (bp) bp.select(id);
    if (snx.Sound) snx.Sound.play('tick');
    refreshPanel(true);
  }

  strip.addEventListener('click', (ev) => {
    const a = ev.target.closest('[data-fight]');
    if (!a || ev.defaultPrevented || ev.button > 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    const id = Number(a.dataset.fight);
    if (!fights.some((x) => x.id === id)) return;                // not known yet: let the link do a normal page load
    ev.preventDefault();
    go(id, true);
  });
  window.addEventListener('popstate', () => {
    const id = Number(new URLSearchParams(location.search).get('fight_id'));
    if (id && id !== activeId && fights.some((x) => x.id === id)) go(id, false); else if (id !== activeId) location.reload();
  });

  function onData(d) {
    hide = !!d.hide; fights = d.fights;
    setLive(!!(d.event && d.event.live));
    renderStrip();
    if (bp) bp.setFights(fights, hide);
    const active = fights.find((f) => f.id === activeId);
    let changed = null;
    fights.forEach((f) => {
      const k = key(f), was = keys.get(f.id);
      if (!first && was && was !== k) {
        if (f.id === activeId) changed = f;
        if (snx.Sound && f.id !== activeId) snx.Sound.play(f.st === 'open' ? 'open' : 'lock');
      }
      keys.set(f.id, k);
    });
    if (changed) {
      const what = changed.res ? 'was declared ' + changed.res.toUpperCase() : changed.st === 'locked' ? 'betting is now closed' : changed.st === 'open' ? 'betting is open again' : 'changed';
      if (snx.toast) snx.toast('Game ' + changed.n + ' ' + what + '. Updating…', { level: 'info', ttl: 2000 });
      if (player) player.dispatchEvent(new CustomEvent('snx:game', { detail: { n: changed.n, left: changed.nm, right: changed.nw, ended: ended(changed) } }));
      setTimeout(() => refreshPanel(false), 1200);
    } else if (first && active) { /* first paint is server-rendered */ }
    if (first) { centerActive(); first = false; }
  }

  if (snx.poller) snx.poller('/_live/event/' + eventId, (d) => onData(d), 5000, strip);
})();
