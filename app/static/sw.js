/* StelNexus service worker.
   Security rule: NEVER cache authenticated HTML or API responses — a shared
   tablet must not serve one user's session data to the next. Only the static
   shell (css/js/icons) and an offline fallback page are cached. */
const CACHE = 'stelnexus-shell-royale10';
const SHELL = [
  '/static/css/app.css',
  '/static/css/luxe.css',
  '/static/css/arena.css',
  '/static/css/royale.css',
  '/static/js/royale.js',
  '/static/js/luxe.js',
  '/static/js/arena.js',
  '/static/js/app.js',
  '/static/js/nav-init.js',
  '/static/icons/icon.svg',
  '/static/icons/icon-192.png',
  '/static/offline.html'
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;                 // never intercept mutations
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  /* CSS and JS are the app's behaviour: always ask the network first so a new release is picked up at
     once (a stale cached script is what once left the bet slip dead). The cache is only the offline fallback. */
  if (url.pathname.startsWith('/static/') && /\.(js|css)$/.test(url.pathname)) {
    event.respondWith(
      fetch(request).then((response) => {
        if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((c) => c.put(request, copy)); }
        return response;
      }).catch(() => caches.match(request))
    );
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((c) => c.put(request, copy));
        return response;
      }))
    );
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/static/offline.html'))
    );
  }
});
