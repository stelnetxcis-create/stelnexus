/* Runs synchronously in <head>, before first paint: restore the collapsed-sidebar state so the
   layout never jumps, then allow width transitions only after the first frame. */
(function () {
  /* Animation level, decided before first paint. Saved per device in localStorage 'snx.fx':
       auto (default) = minimal on phones / touch screens, lite on computers (full is opt-in: it runs a
                        canvas, cursor glow, tilt and ~25 paint-heavy loops that can starve a video playing beside it)
       full | lite | min = forced from the profile page.
     lite -> html.fx-lite (no ambient layers / loops), min -> html.fx-lite + html.fx-min (almost no motion). */
  try {
    var pref = 'auto';
    try { pref = localStorage.getItem('snx.fx') || 'auto'; } catch (e) { /* storage blocked */ }
    var phone = matchMedia('(pointer: coarse)').matches || matchMedia('(max-width: 820px)').matches;
    var inapp = /FBAN|FBAV|FB_IAB|FBIOS|FB4A|Messenger|Instagram|MicroMessenger|Line\/|TikTok|musical_ly|BytedanceWebview|Snapchat|LinkedInApp|Twitter/i.test(navigator.userAgent || '');
    var level = pref === 'full' || pref === 'lite' || pref === 'min' ? pref : (phone || inapp ? 'min' : 'lite');
    var root = document.documentElement;
    if (level !== 'full') root.classList.add('fx-lite');
    if (level === 'min') root.classList.add('fx-min');
    root.setAttribute('data-fx', level);
  } catch (e) { /* keep full effects */ }
  try { if (localStorage.getItem('snx.nav.collapsed') === '1') document.documentElement.classList.add('nav-collapsed'); } catch (e) { /* storage blocked */ }
  window.addEventListener('load', function () {
    requestAnimationFrame(function () { requestAnimationFrame(function () { document.documentElement.classList.add('nav-ready'); }); });
  });
})();
