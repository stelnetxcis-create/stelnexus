/* StelNexus service worker.
   Security rule: NEVER cache authenticated HTML or API responses — a shared
   tablet must not serve one user's session data to the next. Only the static
   shell (css/js/icons) and an offline fallback page are cached.

   The two placeholders below are filled in by tools/build_static.mjs (output: app/build/sw.js,
   which is what /sw.js serves). BUILD changes whenever any bundle changes, so the cache name — and
   therefore the update — is automatic; there is no version string to bump by hand. Unbuilt, this
   file still works: empty shell, cache name "dev", and CSS/JS are fetched network-first. */
const BUILD = /*@build*/'dev';
const SHELL = /*@shell*/[];
const CACHE = 'stelnexus-shell-' + BUILD;
/* Not content-hashed, so fetched past the HTTP cache on install. */
const LOOSE = [
  '/static/css/app.css',            // the offline page links to it
  '/static/icons/icon.svg',
  '/static/icons/icon-192.png',
  '/static/offline.html'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => Promise.all([
      c.addAll(SHELL),
      ...LOOSE.map((u) => c.add(new Request(u, { cache: 'reload' })))
    ])).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function put(request, response) {
  if (response && response.ok) { const copy = response.clone(); caches.open(CACHE).then((c) => c.put(request, copy)); }
  return response;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;                 // never intercept mutations
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  /* Content-hashed build output: the name changes whenever the content does, so the cached copy is
     always right. Cache first. */
  if (url.pathname.startsWith('/static/dist/')) {
    event.respondWith(caches.match(request).then((cached) => cached || fetch(request).then((r) => put(request, r))));
    return;
  }

  /* Un-hashed CSS/JS (only when the app is running from unbuilt sources): network first so a new
     release is picked up at once; the cache is just the offline fallback. */
  if (url.pathname.startsWith('/static/') && /\.(js|css)$/.test(url.pathname)) {
    event.respondWith(fetch(request).then((r) => put(request, r)).catch(() => caches.match(request)));
    return;
  }

  /* Other static files (images, icons, offline page): serve the cached copy, refresh it behind the scenes. */
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const network = fetch(request).then((r) => put(request, r)).catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/static/offline.html'))
    );
  }
});
