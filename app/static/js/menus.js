/* Header menus (notifications, wallet, account, dev): only one open at a time.
   Opening one closes the others; a click outside or Esc closes it. Works with the native <details>. */
(function () {
  const menus = () => Array.from(document.querySelectorAll('.topbar details.menu'));
  const closeAll = (except) => menus().forEach((m) => { if (m !== except && m.open) m.open = false; });

  document.addEventListener('toggle', (ev) => {
    const m = ev.target;
    if (m && m.matches && m.matches('.topbar details.menu') && m.open) closeAll(m);
  }, true);

  document.addEventListener('click', (ev) => {
    const inside = ev.target.closest && ev.target.closest('.topbar details.menu');
    closeAll(inside || null);
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    const open = menus().find((m) => m.open);
    if (open) { open.open = false; const s = open.querySelector('summary'); if (s) s.focus(); }
  });
})();
