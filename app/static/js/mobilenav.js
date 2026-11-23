/* Phone navigation behaviour (pairs with css/mobilenav.css). Plain JS, no dependencies.
   - the Menu drawer is a proper dialog: keyboard/screen readers can't reach it while closed, aria-expanded stays true,
     swipe it left to close, links close it
   - top-bar buttons that have an icon + a text label get an icon-only look on phones (label kept as the tooltip)
   - the active section tab scrolls itself into view in the swipeable tab row */
(function () {
  'use strict';
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const phone = window.matchMedia('(max-width: 860px)');
  const sidebar = $('#sidebar');

  const menuNav = $('#sidebar .nav'); if (menuNav) menuNav.setAttribute('aria-label', 'Menu');

  /* drawer: closed = inert on phones */
  function syncDrawer() {
    if (!sidebar) return;
    const open = sidebar.classList.contains('is-open');
    const closedOnPhone = phone.matches && !open;
    if ('inert' in sidebar) sidebar.inert = closedOnPhone;
    sidebar.setAttribute('aria-hidden', closedOnPhone ? 'true' : 'false');
    if (phone.matches) { sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', open ? 'true' : 'false'); }
    else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); }
    $$('[data-open-nav]').forEach((b) => b.setAttribute('aria-expanded', open ? 'true' : 'false'));
  }
  if (sidebar) {
    new MutationObserver(() => {
      syncDrawer();
      if (sidebar.classList.contains('is-open')) { const first = $('.nav-link.is-active, .nav-link', sidebar); if (first) first.focus({ preventScroll: true }); }
    }).observe(sidebar, { attributes: true, attributeFilter: ['class'] });
    (phone.addEventListener ? phone.addEventListener('change', syncDrawer) : phone.addListener(syncDrawer));
    syncDrawer();

    /* swipe left on the drawer to close it */
    let sx = 0, sy = 0, tracking = false;
    sidebar.addEventListener('touchstart', (e) => { const t = e.touches[0]; sx = t.clientX; sy = t.clientY; tracking = true; }, { passive: true });
    sidebar.addEventListener('touchend', (e) => {
      if (!tracking) return; tracking = false;
      const t = e.changedTouches[0], dx = t.clientX - sx, dy = Math.abs(t.clientY - sy);
      if (dx < -70 && dy < 60) { const x = $('[data-close-nav]', sidebar); if (x) x.click(); }
    }, { passive: true });
    /* tapping a link inside the drawer closes it first, so the page behind never keeps the scroll lock */
    sidebar.addEventListener('click', (e) => { if (e.target.closest('a[href]') && phone.matches) { const x = $('[data-close-nav]', sidebar); if (x) x.click(); } });
  }

  /* top-bar buttons: icon + text label -> text goes into .lbl (hidden on phones), full name kept as tooltip */
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

  /* text-only top-bar buttons (Back to players, Audit log...) don't fit beside the menu, bell, wallet and you on a phone:
     they move into a slim action row at the top of the page and go back to the top bar on bigger screens */
  const bar = $(".topbar-actions"), content = $("main.content");
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
      if (!row) { row = document.createElement('div'); row.className = 'page-actions';
        const tabs = $(':scope > .subtabs, :scope > .ax-bar', content);        // keep the section tabs on top
        if (tabs) tabs.after(row); else content.prepend(row);
      }
      wide.forEach(([b]) => row.appendChild(b));
    } else {
      wide.forEach(([b, mark]) => mark.after(b));
      if (row) { row.remove(); row = null; }
    }
  }
  placeActions();
  (phone.addEventListener ? phone.addEventListener('change', placeActions) : phone.addListener(placeActions));

  /* swipeable section tabs: bring the active one to the middle */
  function centreActive() {
    $$('.content > .subtabs, .content > .ax-bar > .subtabs').forEach((row) => {
      const a = $('.subtab.is-active', row);
      if (a && row.scrollWidth > row.clientWidth) row.scrollLeft = Math.max(0, a.offsetLeft - (row.clientWidth - a.offsetWidth) / 2);
    });
  }
  centreActive();
  window.addEventListener('load', centreActive);
  window.addEventListener('resize', centreActive);
})();
