/* In-app browser notice. Links opened from Messenger / Facebook / Instagram / TikTok / Line ... load inside the
   host app's stripped-down WebView: slower JavaScript, no reliable service worker or "install app", and storage
   that is separate from the phone's browser (so people are asked to sign in again). This shows a small dismissible
   bar that sends them to their real browser.
   Android: an intent link hands the page to the default browser. iPhone: no app may force Safari open, so the bar
   copies the link and shows the exact menu steps. Nothing happens in a normal browser. */
(function () {
  'use strict';
  var RE = /FBAN|FBAV|FB_IAB|FBIOS|FB4A|Messenger|Instagram|MicroMessenger|Line\/|TikTok|musical_ly|BytedanceWebview|Snapchat|LinkedInApp|Twitter/i;
  var ua = navigator.userAgent || '';
  if (!RE.test(ua)) return;

  var root = document.documentElement;
  root.classList.add('in-app');
  try { if (sessionStorage.getItem('snx.inapp.off') === '1') return; } catch (e) { /* storage blocked: show it */ }

  var android = /Android/i.test(ua);
  var url = location.href;

  function build() {
    var bar = document.createElement('div');
    bar.className = 'inapp'; bar.setAttribute('role', 'region'); bar.setAttribute('aria-label', 'Open in your browser');
    bar.innerHTML =
      '<div class="inapp-row">' +
        '<div class="inapp-txt"><b>Faster in your browser</b>' +
        '<span>This in-app view is slow and can sign you out. Open this page in your browser.</span></div>' +
        '<button type="button" class="inapp-go">Open in browser</button>' +
        '<button type="button" class="inapp-x" aria-label="Dismiss">\u00d7</button>' +
      '</div>' +
      '<p class="inapp-help" hidden></p>';
    document.body.insertBefore(bar, document.body.firstChild);

    var help = bar.querySelector('.inapp-help');
    function say(text) { help.textContent = text; help.hidden = false; }

    function copy() {
      if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(url).then(function () { return true; }, function () { return false; });
      try {
        var t = document.createElement('textarea'); t.value = url; t.setAttribute('readonly', ''); t.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(t); t.select(); var ok = document.execCommand('copy'); t.remove(); return Promise.resolve(!!ok);
      } catch (e) { return Promise.resolve(false); }
    }

    bar.querySelector('.inapp-go').addEventListener('click', function () {
      if (android) {
        var intent = 'intent://' + location.host + location.pathname + location.search +
          '#Intent;scheme=' + location.protocol.replace(':', '') +
          ';S.browser_fallback_url=' + encodeURIComponent(url) + ';end';
        location.href = intent;
        // still here a moment later: the app blocked the hand-off, so show the manual steps
        setTimeout(function () {
          if (!document.hidden) say('Tap the \u22ee or \u22ef menu at the top or bottom, then choose \u201cOpen in browser\u201d or \u201cOpen in Chrome\u201d.');
        }, 1400);
      } else {
        copy().then(function (ok) {
          say((ok ? 'Link copied. ' : '') + 'Tap the \u22ef menu (or the compass icon at the bottom right), then choose \u201cOpen in Safari\u201d or \u201cOpen in browser\u201d' + (ok ? ', or paste the link there.' : '.'));
        });
      }
    });

    bar.querySelector('.inapp-x').addEventListener('click', function () {
      try { sessionStorage.setItem('snx.inapp.off', '1'); } catch (e) { /* optional */ }
      bar.remove(); root.classList.remove('has-inapp');
    });
    root.classList.add('has-inapp');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build); else build();
})();
