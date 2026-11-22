/* Battery: pause infinite CSS animations while their element is scrolled out of view.
   Finds every running infinite animation (art-float, bob, wobble, pulse, shine, ...), watches its
   element with one IntersectionObserver and toggles .is-offscreen (see theme.css), which sets
   animation-play-state:paused. Nothing is removed, so the animation resumes where it stopped.
   prefers-reduced-motion users never get these animations at all (base.css switches them off),
   so this exits early for them. Without IntersectionObserver or getAnimations() it does nothing. */
(function () {
  'use strict';
  /* Focus pause: while this window is not the focused one (you clicked into a video / another window that is
     still visible beside it), every looping animation here is paused (see lite.css). Costs nothing, works in
     every animation level. */
  var root = document.documentElement;
  addEventListener('blur', function () { root.classList.add('is-blurred'); });
  addEventListener('focus', function () { root.classList.remove('is-blurred'); });
  if (typeof document.hasFocus === 'function' && !document.hasFocus()) root.classList.add('is-blurred');

  if (!('IntersectionObserver' in window) || typeof document.getAnimations !== 'function') return;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var MIN = document.documentElement.classList.contains('fx-min');
  var seen = new WeakSet();
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { e.target.classList.toggle('is-offscreen', !e.isIntersecting); });
  }, { rootMargin: '120px 0px' });

  function scan() {
    if (document.hidden) return;
    document.getAnimations().forEach(function (a) {
      var fx = a.effect, el = fx && fx.target;
      if (!el || seen.has(el) || el === document.body || el === document.documentElement) return;
      if (fx.getComputedTiming().iterations !== Infinity) return;
      if (MIN) { a.cancel(); return; }                       // minimal level: nothing loops, anywhere
      seen.add(el); io.observe(el);
    });
  }

  // Animations also start later (polled tiles, toasts, class changes), so rescan after DOM changes, debounced.
  var timer = 0;
  function later() { clearTimeout(timer); timer = setTimeout(scan, 400); }
  function start() {
    scan();
    if ('MutationObserver' in window) {
      /* Only react to NEW nodes, or to class changes on elements not yet watched. Toggling .is-offscreen on a
         watched element is our own doing and must not queue another scan (it used to re-trigger itself). */
      new MutationObserver(function (recs) {
        for (var i = 0; i < recs.length; i++) {
          var r = recs[i];
          if (r.type === 'childList' ? r.addedNodes.length : !seen.has(r.target)) { later(); return; }
        }
      }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  addEventListener('load', later);
  document.addEventListener('visibilitychange', later);
})();
