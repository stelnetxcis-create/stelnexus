/* Login / register / error pages only (the .auth-card): wordmark letters, coin parallax, submit feedback.
   Split out of luxe.js so the console bundle does not carry it. Loaded after luxe.js, which builds the .coin-stage. */
/* ===== LUXE login extras ===== */
(function () {
  'use strict';
  var card = document.querySelector('.auth-card'); if (!card) return;
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  // wordmark letters
  var h = card.querySelector('.auth-brand h1');
  if (h && !reduce) {
    var t = h.textContent; h.textContent = ''; h.setAttribute('aria-label', t);
    Array.from(t).forEach(function (c, i) { var s = document.createElement('span'); s.className = 'ch'; s.setAttribute('aria-hidden', 'true'); s.style.setProperty('--i', i); s.textContent = c; h.appendChild(s); });
  }
  // sparkles around the coin
  var stage = card.querySelector('.coin-stage');
  if (stage && !reduce) {
    [[8, 20], [86, 12], [92, 62], [4, 70], [70, 90], [24, 4]].forEach(function (p, i) {
      var s = document.createElement('b'); s.className = 'spark'; s.style.left = p[0] + '%'; s.style.top = p[1] + '%'; s.style.setProperty('--d', (i * .55) + 's'); stage.appendChild(s);
    });
  }
  // pointer parallax (coin stage tilts, card drifts)
  if (!reduce && matchMedia('(pointer: fine)').matches) {
    var raf = 0, cx = 0, cy = 0;
    addEventListener('pointermove', function (e) {
      cx = e.clientX / innerWidth - .5; cy = e.clientY / innerHeight - .5;
      if (raf) return;
      raf = requestAnimationFrame(function () {
        if (stage) { stage.style.setProperty('--py', (cx * 28).toFixed(1) + 'deg'); stage.style.setProperty('--px', (-cy * 22).toFixed(1) + 'deg'); }
        card.style.translate = (cx * -10).toFixed(1) + 'px ' + (cy * -8).toFixed(1) + 'px'; raf = 0;
      });
    }, { passive: true });
  }
  // submit feedback
  var f = card.querySelector('form');
  if (f) f.addEventListener('submit', function () {
    document.body.classList.add('is-submitting');
    var b = f.querySelector('button[type=submit]'); if (b) b.textContent = 'Signing in…';
  });
})();
