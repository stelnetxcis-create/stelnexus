/* Battery: pause infinite CSS animations while their element is scrolled out of view.
   Finds every running infinite animation (art-float, bob, wobble, pulse, shine, ...), watches its
   element with one IntersectionObserver and toggles .is-offscreen (see theme.css), which sets
   animation-play-state:paused. Nothing is removed, so the animation resumes where it stopped.
   prefers-reduced-motion users never get these animations at all (base.css switches them off),
   so this exits early for them. Without IntersectionObserver or getAnimations() it does nothing. */
(function () {
  'use strict';
  if (!('IntersectionObserver' in window) || typeof document.getAnimations !== 'function') return;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;

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
      seen.add(el); io.observe(el);
    });
  }

  // Animations also start later (polled tiles, toasts, class changes), so rescan after DOM changes, debounced.
  var timer = 0;
  function later() { clearTimeout(timer); timer = setTimeout(scan, 400); }
  function start() {
    scan();
    if ('MutationObserver' in window) {
      new MutationObserver(later).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  addEventListener('load', later);
  document.addEventListener('visibilitychange', later);
})();
