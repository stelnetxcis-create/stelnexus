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
  $$('[data-copy]').forEach((b) => b.addEventListener('click', async () => {
    const text = b.getAttribute('data-copy');
    try { await navigator.clipboard.writeText(text); } catch (e) {
      const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select();
      try { document.execCommand('copy'); } catch (e2) { /* ignore */ } t.remove();
    }
    const old = b.textContent; b.textContent = 'Copied'; setTimeout(() => { b.textContent = old; }, 1400);
  }));

  /* ---- modals ---- */
  $$('[data-modal]').forEach((trigger) => {
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
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    });
  });
  $$('dialog.modal').forEach((dialog) => {
    dialog.addEventListener('click', (e) => {
      if (e.target === dialog) dialog.close();
    });
  });

  /* ---- idempotency + double-submit guard ----
     One key per form render: retrying after a network failure reuses the key,
     so the server collapses the duplicate instead of moving money twice. */
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  $$('form[method="post"]').forEach((form) => {
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
      });
      setTimeout(() => {
        form.dataset.submitted = '';
        $$('button', form).forEach((b) => {
          b.disabled = false;
          if (b.dataset.label) b.textContent = b.dataset.label;
        });
      }, 8000);
    });
  });

  /* ---- client-side table search / sort / paging ---- */
  $$('[data-table]').forEach((table) => {
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

  /* ---- flash auto-dismiss ---- */
  setTimeout(() => {
    $$('.alert-success').forEach((el) => {
      el.style.transition = 'opacity .4s'; el.style.opacity = '0';
      setTimeout(() => el.remove(), 450);
    });
  }, 6000);

  /* ---- optional live refresh (pool view) ---- */
  const live = $('[data-autorefresh]');
  if (live) {
    const seconds = parseInt(live.getAttribute('data-autorefresh'), 10) || 20;
    let timer = setInterval(() => { if (!document.hidden) window.location.reload(); }, seconds * 1000);
    const toggle = $('[data-live-toggle]');
    if (toggle) {
      toggle.addEventListener('change', () => {
        if (toggle.checked) timer = setInterval(() => { if (!document.hidden) window.location.reload(); }, seconds * 1000);
        else clearInterval(timer);
      });
    }
  }

  /* ---- JSON helper for the mobile API / future native shell ---- */
  window.StelNexus = {
    csrf: csrf,
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
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    });
  }
  let deferredPrompt = null;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    const button = $('[data-install]');
    if (button) {
      button.hidden = false;
      button.addEventListener('click', async () => {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
        deferredPrompt = null;
        button.hidden = true;
      });
    }
  });
})();
