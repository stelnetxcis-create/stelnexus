/* Phone navigation behaviour (pairs with css/mobilenav.css). Plain JS, no dependencies.
   PORTRAIT phone: the bottom bar is the navigation; "More" opens a bottom sheet built from the sidebar's own links, so the
   two can never drift apart. LANDSCAPE phone: the sidebar is a drawer (menu button).
   Also: top-bar buttons with an icon + label become icon-only on phones, text-only ones move into a slim action row,
   and the active section tab scrolls into view in the swipeable tab row. */
(function () {
  'use strict';
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const phone = window.matchMedia('(max-width: 860px)');
  const portrait = window.matchMedia('(max-width: 860px) and (orientation: portrait)');
  const sidebar = $('#sidebar');
  const listen = (mq, fn) => (mq.addEventListener ? mq.addEventListener('change', fn) : mq.addListener(fn));
  const menuNav = $('#sidebar .nav'); if (menuNav) menuNav.setAttribute('aria-label', 'Menu');

  /* ---------- landscape drawer: closed = inert ---------- */
  function syncDrawer() {
    if (!sidebar) return;
    const open = sidebar.classList.contains('is-open');
    const closedOnPhone = phone.matches && !open;
    if ('inert' in sidebar) sidebar.inert = closedOnPhone || portrait.matches;
    sidebar.setAttribute('aria-hidden', closedOnPhone || portrait.matches ? 'true' : 'false');
    if (phone.matches) { sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', open ? 'true' : 'false'); }
    else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); }
    $$('[data-open-nav]').forEach((b) => b.setAttribute('aria-expanded', open ? 'true' : 'false'));
  }
  if (sidebar) {
    new MutationObserver(() => {
      syncDrawer();
      if (sidebar.classList.contains('is-open')) { const first = $('.nav-link.is-active, .nav-link', sidebar); if (first) first.focus({ preventScroll: true }); }
    }).observe(sidebar, { attributes: true, attributeFilter: ['class'] });
    listen(phone, syncDrawer); listen(portrait, () => { syncDrawer(); if (portrait.matches) { const x = $('[data-close-nav]', sidebar); if (sidebar.classList.contains('is-open') && x) x.click(); } });
    syncDrawer();
    let sx = 0, sy = 0, tracking = false;
    sidebar.addEventListener('touchstart', (e) => { const t = e.touches[0]; sx = t.clientX; sy = t.clientY; tracking = true; }, { passive: true });
    sidebar.addEventListener('touchend', (e) => {
      if (!tracking) return; tracking = false;
      const t = e.changedTouches[0], dx = t.clientX - sx, dy = Math.abs(t.clientY - sy);
      if (dx < -70 && dy < 60) { const x = $('[data-close-nav]', sidebar); if (x) x.click(); }
    }, { passive: true });
    sidebar.addEventListener('click', (e) => { if (e.target.closest('a[href]') && phone.matches) { const x = $('[data-close-nav]', sidebar); if (x) x.click(); } });
  }

  /* ---------- portrait: customisable bottom bar + the "More" sheet ----------
     Up to 5 pinned destinations + More (6 slots). Default: Home, All games, Records. Pick your own in More > Customise.
     The pool of destinations is the sidebar's own links (plus any server tab that isn't in the sidebar), so nothing drifts. */
  const tabbar = $('.tabbar'), moreTab = $('.tabbar [data-open-more]');
  if (tabbar && moreTab) {
    const MAX = 5, KEY = 'sn.bar.v1';
    const pool = [], seen = {};
    const add = (el, extraActive) => {
      const href = el.getAttribute('href'); if (!href || seen[href]) return;
      const use = el.querySelector('use'); const lab = el.querySelector('span');
      seen[href] = { href, icon: use ? (use.getAttribute('href') || '').replace('#', '') : 'i-grid',
        label: href === '/dashboard' ? 'Home' : (lab || el).textContent.trim().replace(/\s*·\s*dev only$/i, ''), active: el.classList.contains('is-active') || !!extraActive,
        section: '' };
      pool.push(seen[href]);
    };
    let section = '';
    if (menuNav) Array.from(menuNav.children).forEach((el) => {
      if (el.classList.contains('nav-title')) { section = el.textContent.trim().replace(/\s*·\s*dev only$/i, ''); return; }
      if (el.matches('a.nav-link')) { add(el); const it = seen[el.getAttribute('href')]; if (it && !it.section) it.section = section; }
    });
    const serverTabs = $$('a.tab', tabbar);
    serverTabs.forEach((el) => { const h = el.getAttribute('href'); if (seen[h] && el.classList.contains('is-active')) seen[h].active = true; });   // server-only tabs (Wallet, Bets) are NOT offered: the top-bar balance chip and Records already cover them
    const fallback = serverTabs.map((a) => a.getAttribute('href')).filter((h) => seen[h]);
    const DEFAULT = ['/dashboard', '/games', '/records'].filter((h) => seen[h]);
    const defaults = () => (DEFAULT.length ? DEFAULT : fallback).slice(0, MAX);
    const load = () => { try { const v = JSON.parse(localStorage.getItem(KEY) || 'null'); if (Array.isArray(v)) return v.filter((h) => seen[h]).slice(0, MAX); } catch (e) { /* storage blocked */ } return defaults(); };
    const save = (arr) => { try { localStorage.setItem(KEY, JSON.stringify(arr)); } catch (e) { /* ignore */ } };
    let pins = load();
    const svg = (id) => '<svg class="ico"><use href="#' + id + '"/></svg>';
    const esc = (t) => t.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

    function renderBar() {
      $$('a.tab', tabbar).forEach((a) => a.remove());
      pins.forEach((h) => {
        const it = seen[h]; const a = document.createElement('a');
        a.className = 'tab' + (it.active ? ' is-active' : ''); a.href = h;
        if (it.active) a.setAttribute('aria-current', 'page');
        a.innerHTML = svg(it.icon) + '<span>' + esc(it.label) + '</span>';
        tabbar.insertBefore(a, moreTab);
      });
      tabbar.style.setProperty('--slots', pins.length + 1);
    }

    const scrim = document.createElement('div'); scrim.className = 'more-scrim';
    const sheet = document.createElement('div'); sheet.className = 'more-sheet'; sheet.id = 'more-sheet';
    sheet.setAttribute('role', 'dialog'); sheet.setAttribute('aria-modal', 'true'); sheet.setAttribute('aria-label', 'More'); sheet.inert = true;
    document.body.append(scrim, sheet);
    let editing = false;
    const inst = () => { const i = $('.sidebar-install'); return $('[data-install]') && i && !i.hidden; };

    function renderSheet() {
      let h = '<div class="more-grab" aria-hidden="true"></div><div class="more-head"><strong>' + (editing ? 'Customise bottom bar' : 'More') + '</strong>' +
        '<button type="button" class="more-edit" data-edit>' + (editing ? 'Done' : 'Customise') + '</button></div>';
      if (editing) {
        h += '<p class="more-hint">Tap to add or remove. The bar holds ' + MAX + ' shortcuts plus More (' + pins.length + '/' + MAX + ').</p>';
        h += '<div class="more-title">On your bar</div><div class="more-grid">' +
          (pins.length ? pins.map((x) => item(seen[x], true)).join('') : '<p class="more-empty">Nothing pinned. Pick some below.</p>') + '</div>';
        h += '<div class="more-title">Available</div><div class="more-grid">' + pool.filter((x) => !pins.includes(x.href)).map((x) => item(x, false)).join('') + '</div>';
        h += '<button type="button" class="more-reset" data-reset>Reset to default</button>';
      } else {
        const groups = {}; const order = [];
        pool.filter((x) => !pins.includes(x.href)).forEach((x) => { const k = x.section || ''; if (!groups[k]) { groups[k] = []; order.push(k); } groups[k].push(x); });
        order.forEach((k) => { if (k) h += '<div class="more-title">' + esc(k) + '</div>'; h += '<div class="more-grid">' + groups[k].map((x) => item(x, false)).join('') + '</div>'; });
        if (inst()) h += '<div class="more-title">App</div><div class="more-grid"><button type="button" class="more-item" data-install-app>' + svg('i-download') + '<span>Install app</span></button></div>';
        if (!order.length && !inst()) h += '<p class="more-empty">Everything is on your bar.</p>';
      }
      sheet.innerHTML = h;
    }
    function item(x, pinned) {
      if (editing) return '<button type="button" class="more-item' + (pinned ? ' is-pinned' : '') + '" data-pin="' + esc(x.href) + '" aria-pressed="' + pinned + '">' +
        '<i class="pin-badge" aria-hidden="true">' + (pinned ? '−' : '+') + '</i>' + svg(x.icon) + '<span>' + esc(x.label) + '</span></button>';
      return '<a class="more-item' + (x.active ? ' is-active' : '') + '" href="' + esc(x.href) + '">' + svg(x.icon) + '<span>' + esc(x.label) + '</span></a>';
    }

    let opener = null;
    function open() {
      if (!portrait.matches) return;
      editing = false; renderSheet();
      opener = document.activeElement; sheet.inert = false; sheet.classList.add('is-open'); scrim.classList.add('is-open');
      moreTab.classList.add('is-open'); moreTab.setAttribute('aria-expanded', 'true'); document.body.style.overflow = 'hidden';
      const first = $('.more-item', sheet); if (first) first.focus({ preventScroll: true });
    }
    function close() {
      sheet.classList.remove('is-open'); scrim.classList.remove('is-open'); sheet.inert = true;
      moreTab.classList.remove('is-open'); moreTab.setAttribute('aria-expanded', 'false'); document.body.style.overflow = '';
      if (opener && opener.focus) opener.focus({ preventScroll: true }); opener = null;
    }
    sheet.addEventListener('click', (e) => {
      const t = e.target;
      const pin = t.closest('[data-pin]');
      if (pin) {
        const h = pin.getAttribute('data-pin');
        if (pins.includes(h)) pins = pins.filter((x) => x !== h);
        else if (pins.length < MAX) pins = pins.concat(h);
        else { const hint = $('.more-hint', sheet); if (hint) { hint.textContent = 'Bar is full (' + MAX + '). Remove one first.'; hint.classList.add('is-warn'); } return; }
        save(pins); renderBar(); const y = sheet.scrollTop; renderSheet(); sheet.scrollTop = y;
        const again = $('[data-pin="' + h.replace(/"/g, '\\"') + '"]', sheet); if (again) again.focus({ preventScroll: true });
        return;
      }
      if (t.closest('[data-edit]')) { editing = !editing; renderSheet(); return; }
      if (t.closest('[data-reset]')) { pins = defaults(); save(pins); renderBar(); renderSheet(); return; }
      if (t.closest('[data-install-app]')) { close(); const b = $('[data-install]'); if (b) b.click(); return; }
      if (t.closest('a[href]')) close();
    });
    moreTab.addEventListener('click', () => (sheet.classList.contains('is-open') ? close() : open()));
    scrim.addEventListener('click', close);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && sheet.classList.contains('is-open')) close(); });
    listen(portrait, () => { if (!portrait.matches) close(); });
    let sy0 = 0, drag = false;
    sheet.addEventListener('touchstart', (e) => { sy0 = e.touches[0].clientY; drag = sheet.scrollTop <= 0; }, { passive: true });
    sheet.addEventListener('touchend', (e) => { if (drag && e.changedTouches[0].clientY - sy0 > 80) close(); drag = false; }, { passive: true });
    renderBar();
  }

  /* ---------- top-bar buttons ---------- */
  $$('.topbar-actions > .btn, .topbar-actions > a.btn, .topbar-actions > button.btn').forEach((b) => {
    if (b.querySelector('.lbl') || !b.querySelector('svg.ico')) return;
    const text = Array.from(b.childNodes).filter((n) => n.nodeType === 3 && n.textContent.trim());
    if (!text.length) return;
    const label = text.map((n) => n.textContent.trim()).join(' ');
    const span = document.createElement('span'); span.className = 'lbl'; span.textContent = label;
    text[0].replaceWith(span); text.slice(1).forEach((n) => n.remove());
    if (!b.title) b.title = label;
    if (!b.getAttribute('aria-label')) b.setAttribute('aria-label', label);
    b.classList.add('is-iconic');
  });
  const bar = $('.topbar-actions'), content = $('main.content');
  const wide = [];
  if (bar && content) {
    $$(':scope > .btn:not(.is-iconic), :scope > a.btn:not(.is-iconic)', bar).forEach((b) => {
      const mark = document.createComment('topbar-action'); b.before(mark); wide.push([b, mark]);
    });
  }
  let row = null;
  function placeActions() {
    if (!wide.length) return;
    if (phone.matches) {
      if (!row) {
        row = document.createElement('div'); row.className = 'page-actions';
        const tabs = $(':scope > .subtabs, :scope > .ax-bar', content);
        if (tabs) tabs.after(row); else content.prepend(row);
      }
      wide.forEach(([b]) => row.appendChild(b));
    } else {
      wide.forEach(([b, mark]) => mark.after(b));
      if (row) { row.remove(); row = null; }
    }
  }
  placeActions(); listen(phone, placeActions);

  /* ---------- swipeable section tabs ---------- */
  function centreActive() {
    $$('.content > .subtabs, .content > .ax-bar > .subtabs').forEach((r) => {
      const a = $('.subtab.is-active', r);
      if (a && r.scrollWidth > r.clientWidth) r.scrollLeft = Math.max(0, a.offsetLeft - (r.clientWidth - a.offsetWidth) / 2);
    });
  }
  centreActive(); window.addEventListener('load', centreActive); window.addEventListener('resize', centreActive);
})();
