/* Runs synchronously in <head>, before first paint: restore the collapsed-sidebar state so the
   layout never jumps, then allow width transitions only after the first frame. */
(function () {
  try { if (localStorage.getItem('snx.nav.collapsed') === '1') document.documentElement.classList.add('nav-collapsed'); } catch (e) { /* storage blocked */ }
  window.addEventListener('load', function () {
    requestAnimationFrame(function () { requestAnimationFrame(function () { document.documentElement.classList.add('nav-ready'); }); });
  });
})();
