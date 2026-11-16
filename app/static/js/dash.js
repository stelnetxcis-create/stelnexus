/* Dashboard: greeting + date in the viewer's own timezone (the server only knows UTC). */
(function () {
  'use strict';
  var h = new Date().getHours();
  var greeting = h < 5 ? 'Good evening' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  document.querySelectorAll('[data-greet]').forEach(function (el) { el.textContent = greeting; });
  document.querySelectorAll('[data-today]').forEach(function (el) {
    try { el.textContent = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }); } catch (e) { /* keep server date */ }
  });
})();
