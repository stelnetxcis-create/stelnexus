/* StelNexus console behaviour. No frameworks, no CDN — works offline. */
(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  /* ---- navigation drawer ---- */
  const sidebar = $('#sidebar');
  const backdrop = $('.backdrop');
  function openNav() {
    if (!sidebar) return;
    sidebar.classList.add('is-open');
    if (backdrop) backdrop.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function closeNav() {
    if (!sidebar) return;
    sidebar.classList.remove('is-open');
    if (backdrop) backdrop.hidden = true;
    document.body.style.overflow = '';
  }
  $$('[data-open-nav]').forEach((b) => b.addEventListener('click', openNav));
  $$('[data-close-nav]').forEach((b) => b.addEventListener('click', closeNav));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeNav(); });

  /* ---- collapsible sidebar (desktop) ---- */
  const NAV_KEY = 'snx.nav.collapsed';
  const setCollapsed = (on) => {
    document.documentElement.classList.toggle('nav-collapsed', on);
    document.body.classList.toggle('nav-collapsed', on);
    try { localStorage.setItem(NAV_KEY, on ? '1' : '0'); } catch (e) { /* storage blocked */ }
  };
  try { if (localStorage.getItem(NAV_KEY) === '1') document.body.classList.add('nav-collapsed'); } catch (e) { /* ignore */ }
  document.documentElement.classList.add('nav-ready');
  $$('[data-collapse-nav]').forEach((b) => b.addEventListener('click', () => {
    setCollapsed(!document.documentElement.classList.contains('nav-collapsed'));
  }));

  /* ---- copy-to-clipboard buttons ---- */
  function enhanceCopy(root) {
    $$('[data-copy]', root).forEach((b) => {
      if (b.dataset.enh) return; b.dataset.enh = '1';
      b.addEventListener('click', async () => {
        const text = b.getAttribute('data-copy');
        try { await navigator.clipboard.writeText(text); } catch (e) {
          const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select();
          try { document.execCommand('copy'); } catch (e2) { /* ignore */ } t.remove();
        }
        const old = b.textContent; b.textContent = 'Copied'; setTimeout(() => { b.textContent = old; }, 1400);
      });
    });
  }

  /* ---- modals ---- */
  function enhanceModals(root) {
    $$('[data-modal]', root).forEach((trigger) => {
      if (trigger.dataset.enh) return; trigger.dataset.enh = '1';
      trigger.addEventListener('click', () => {
        const dialog = document.getElementById(trigger.getAttribute('data-modal'));
        if (!dialog) return;
        $$('[data-fill]', dialog).forEach((input) => {
          const value = trigger.getAttribute('data-' + input.getAttribute('data-fill'));
          if (value !== null) input.value = value;
        });
        $$('[data-action-from]', dialog).forEach((form) => {
          const template = trigger.getAttribute('data-action');
          if (template) form.action = template;
        });
        $$('[data-text]', dialog).forEach((el) => {
          const value = trigger.getAttribute('data-' + el.getAttribute('data-text'));
          if (value !== null) el.textContent = value;
        });
        document.documentElement.classList.add('modal-open');
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');
      });
    });
    $$('dialog.modal', root).forEach((dialog) => {
      if (dialog.dataset.enh) return; dialog.dataset.enh = '1';
      dialog.addEventListener('close', () => document.documentElement.classList.remove('modal-open'));
      dialog.addEventListener('click', (e) => {
        if (e.target === dialog) dialog.close();
      });
    });
  }

  /* ---- idempotency + double-submit guard ----
     One key per form render: retrying after a network failure reuses the key,
     so the server collapses the duplicate instead of moving money twice. */
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  function enhanceForms(root) {
  $$('form[method="post"]', root).forEach((form) => {
    if (form.dataset.enh) return; form.dataset.enh = '1';
    const keyField = form.querySelector('[data-idem]');
    if (keyField && !keyField.value) keyField.value = uuid();
    form.addEventListener('submit', (e) => {
      const message = form.getAttribute('data-confirm');
      if (message && !window.confirm(message)) { e.preventDefault(); return; }
      if (form.dataset.submitted === '1') { e.preventDefault(); return; }
      form.dataset.submitted = '1';
      $$('button[type="submit"], button:not([type])', form).forEach((b) => {
        b.disabled = true;
        b.dataset.label = b.textContent;
        b.textContent = 'Working…';
        b.classList.add('is-working');
      });
      setTimeout(() => {
        form.dataset.submitted = '';
        $$('button', form).forEach((b) => {
          b.disabled = false;
          b.classList.remove('is-working');
          if (b.dataset.label) b.textContent = b.dataset.label;
        });
      }, 8000);
    });
  });
  }

  /* ---- client-side table search / sort / paging ---- */
  function enhanceTables(root) {
  $$('[data-table]', root).forEach((table) => {
    const wrap = table.closest('.table-wrap');
    if (!wrap || wrap.dataset.enhanced) return;
    wrap.dataset.enhanced = '1';
    const body = table.tBodies[0];
    if (!body || body.rows.length === 0) return;
    const allRows = Array.from(body.rows);
    let page = 0;
    const size = parseInt(table.getAttribute('data-page-size') || '25', 10);
    let filtered = allRows;

    const tools = document.createElement('div');
    tools.className = 'table-tools no-print';
    tools.style.padding = '10px 12px';
    const search = document.createElement('div');
    search.className = 'search';
    search.style.maxWidth = '260px';
    search.innerHTML = '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
    const input = document.createElement('input');
    input.type = 'search';
    input.placeholder = 'Filter rows…';
    input.setAttribute('aria-label', 'Filter table rows');
    search.appendChild(input);
    tools.appendChild(search);
    const count = document.createElement('span');
    count.className = 'muted small';
    tools.appendChild(count);
    wrap.parentNode.insertBefore(tools, wrap);

    const pager = document.createElement('div');
    pager.className = 'pager no-print';
    wrap.parentNode.insertBefore(pager, wrap.nextSibling);

    function render() {
      const pages = Math.max(1, Math.ceil(filtered.length / size));
      page = Math.min(page, pages - 1);
      allRows.forEach((r) => { r.hidden = true; });
      filtered.slice(page * size, page * size + size).forEach((r) => { r.hidden = false; });
      count.textContent = filtered.length + ' row' + (filtered.length === 1 ? '' : 's');
      pager.innerHTML = '';
      if (pages <= 1) return;
      const prev = document.createElement('button');
      prev.className = 'btn btn-sm'; prev.textContent = '‹ Prev'; prev.disabled = page === 0;
      prev.onclick = () => { page--; render(); };
      const next = document.createElement('button');
      next.className = 'btn btn-sm'; next.textContent = 'Next ›'; next.disabled = page >= pages - 1;
      next.onclick = () => { page++; render(); };
      const label = document.createElement('span');
      label.textContent = 'Page ' + (page + 1) + ' of ' + pages;
      pager.append(prev, label, next);
    }

    input.addEventListener('input', () => {
      const term = input.value.trim().toLowerCase();
      filtered = term ? allRows.filter((r) => r.textContent.toLowerCase().includes(term)) : allRows;
      page = 0;
      render();
    });

    Array.from(table.tHead ? table.tHead.rows[0].cells : []).forEach((th, index) => {
      th.style.cursor = 'pointer';
      th.title = 'Sort';
      th.addEventListener('click', () => {
        const asc = th.dataset.asc !== '1';
        Array.from(table.tHead.rows[0].cells).forEach((c) => { c.dataset.asc = ''; });
        th.dataset.asc = asc ? '1' : '0';
        const value = (row) => {
          const cell = row.cells[index];
          if (!cell) return '';
          const raw = cell.getAttribute('data-sort') || cell.textContent.trim();
          const numeric = parseFloat(raw.replace(/[^0-9.\-]/g, ''));
          return isNaN(numeric) || /[^0-9.,\-\s₱%]/.test(raw) ? raw.toLowerCase() : numeric;
        };
        filtered.sort((a, b) => {
          const x = value(a), y = value(b);
          if (x === y) return 0;
          return (x > y ? 1 : -1) * (asc ? 1 : -1);
        });
        filtered.forEach((r) => body.appendChild(r));
        render();
      });
    });
    render();
  });
  }

  function enhance(root) {
    enhanceCopy(root); enhanceModals(root); enhanceForms(root); enhanceTables(root);
  }
  enhance(document);

  /* ---- flash auto-dismiss ---- */
  setTimeout(() => {
    $$('.alert-success').forEach((el) => {
      el.style.transition = 'opacity .4s'; el.style.opacity = '0';
      setTimeout(() => el.remove(), 450);
    });
  }, 6000);

  /* ---- partial page refresh: replace one box, not the whole page ----
     A box is marked on the server with {% call ui.region('name') %} (it renders <div data-region="name">).
     Asking the same URL with "X-Fragment: name" returns only that box's HTML, with an ETag; sending the ETag
     back gets "304 Not Modified" and no body when nothing changed. The swap leaves the rest of the page, the
     scroll position, the video player and the chat untouched, then re-binds the page's handlers to the new HTML
     (enhance) and announces it ("snx:region") so other scripts can do the same. */
  const regionState = {};                                     // name -> { etag, busy }
  const typingIn = (el) => { const a = document.activeElement; return !!(a && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)); };
  const blocked = (el) => typingIn(el) || !!document.querySelector('dialog[open]');

  function swapRegion(el, name, html) {
    const x = window.scrollX, y = window.scrollY;
    el.innerHTML = '<!--region:' + name + '-->' + html + '<!--/region:' + name + '-->';
    enhance(el);
    if (window.scrollY !== y) window.scrollTo(x, y);          // keep the reader where they were
    document.dispatchEvent(new CustomEvent('snx:region', { detail: { name: name, el: el } }));
  }

  /* Resolves to 'updated' | 'same' | 'skipped' (user is typing / a dialog is open) | 'busy' | 'missing' | 'failed'.
     opts.force ignores the typing / dialog guard (the person pressed "Refresh"). */
  async function refreshRegion(name, opts) {
    opts = opts || {};
    const el = document.querySelector('[data-region="' + name + '"]');
    if (!el) return 'missing';
    const st = regionState[name] || (regionState[name] = { etag: '', busy: false });
    if (st.busy) return 'busy';
    if (!opts.force && blocked(el)) return 'skipped';
    st.busy = true;
    try {
      const headers = { 'X-Fragment': name, 'Accept': 'text/html' };
      if (st.etag) headers['If-None-Match'] = st.etag;        // set by hand: the request is no-store, so the browser would not
      const r = await fetch(location.pathname + location.search,
        { headers: headers, credentials: 'same-origin', cache: 'no-store' });
      if (r.status === 304) return 'same';
      // A redirect (signed out), an error page or a full page (no such region) all mean: not a fragment.
      if (!r.ok || r.redirected || r.headers.get('X-Fragment') !== name) return 'failed';
      const html = await r.text();
      if (!opts.force && blocked(el)) return 'skipped';       // they started typing while we waited: try again later
      st.etag = r.headers.get('ETag') || '';                  // remembered only once applied, or a skipped update would be lost
      swapRegion(el, name, html);
      return 'updated';
    } catch (e) {
      return 'failed';
    } finally {
      st.busy = false;
    }
  }

  /* ---- shared helper: how long to wait before the next poll ----
     Quiet for a minute: 4 s -> 15 s. Quiet for five: -> 30 s. Any activity ("bump": a message, a key press, a tap,
     the tab coming back) snaps it back, and calls onWake so a sleeping timer fires now, not up to 30 s late. */
  function pacer(opts) {
    opts = opts || {};
    const base = opts.base || 4000;
    const steps = opts.steps || [[60000, 15000], [300000, 30000]];
    let lastActive = Date.now();
    const quiet = () => Date.now() - lastActive;
    return {
      bump() { const was = quiet() >= steps[0][0]; lastActive = Date.now(); if (was && opts.onWake) opts.onWake(); },
      delay() { let d = base; steps.forEach((st) => { if (quiet() >= st[0] && st[1] > d) d = st[1]; }); return d; },
    };
  }

  /* ---- optional live refresh: <section data-autorefresh="20" data-autorefresh-region="name"> ----
     Refreshes that one box every N seconds (stretching the wait while nothing changes, pausing in a hidden tab).
     Markup that names no box keeps the old behaviour (reload the page), so nothing silently stops updating. */
  const live = $('[data-autorefresh]');
  if (live) {
    const base = (parseInt(live.getAttribute('data-autorefresh'), 10) || 20) * 1000;
    const owner = live.closest('[data-region]');
    const name = live.getAttribute('data-autorefresh-region') || live.getAttribute('data-region') || (owner && owner.getAttribute('data-region')) || '';
    const toggle = $('[data-live-toggle]');
    let timer = 0, running = true, unchanged = 0;
    const wait = () => base * Math.min(1 + Math.floor(unchanged / 3), 4);       // every 3 identical answers adds one interval, up to 4x
    async function tick() {
      clearTimeout(timer);
      if (!running || document.hidden) return;                                   // hidden: no timer at all; visibilitychange restarts it
      if (!name) { window.location.reload(); return; }
      const result = await refreshRegion(name);
      if (result === 'updated') unchanged = 0;
      else if (result === 'same') unchanged += 1;
      else if (result === 'failed' || result === 'missing') unchanged = Math.min(unchanged + 3, 9);
      if (running && !document.hidden) timer = setTimeout(tick, wait());
    }
    document.addEventListener('visibilitychange', () => { if (!document.hidden && running) tick(); });
    if (toggle) {
      toggle.addEventListener('change', () => {
        running = toggle.checked;
        if (running) tick(); else clearTimeout(timer);
      });
    }
    timer = setTimeout(tick, base);
  }

  /* ---- JSON helper for the mobile API / future native shell ---- */
  window.StelNexus = {
    csrf: csrf,
    refreshRegion: refreshRegion,
    enhance: enhance,
    pacer: pacer,
    async api(path, options) {
      const opts = Object.assign({ headers: {} }, options || {});
      opts.headers = Object.assign({
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf(),
        'Accept': 'application/json'
      }, opts.headers);
      opts.credentials = 'same-origin';
      if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
      const response = await fetch(path, opts);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Request failed');
      return data;
    }
  };

  /* ---- PWA ---- */
  if ('serviceWorker' in navigator && location.protocol === 'https:' && !document.documentElement.classList.contains('in-app')) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    });
  }
  let deferredPrompt = null;
  const installBtns = $$('[data-install]');
  const installHint = $('[data-install-hint]');
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const foot = $('.sidebar-install');
  if (standalone && foot) foot.hidden = true;           // already installed: nothing to offer
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferredPrompt = e; });
  window.addEventListener('appinstalled', () => { if (foot) foot.hidden = true; });
  installBtns.forEach((button) => button.addEventListener('click', async () => {
    if (deferredPrompt) {
      deferredPrompt.prompt();
      await deferredPrompt.userChoice;
      deferredPrompt = null;
      return;
    }
    // No install prompt available (iOS, or the browser already declined): say how to do it by hand.
    if (installHint) {
      const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
      installHint.textContent = ios ? 'Tap Share, then “Add to Home Screen”.'
                                    : 'Open your browser menu and choose “Install app” or “Add to Home screen”.';
      installHint.hidden = !installHint.hidden;
    }
  }));
})();
