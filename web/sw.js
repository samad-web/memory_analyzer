/* Service worker: the offline shell, and the push that is the point of it.
 *
 * Two jobs, and the second is the one that matters. An agent blocked on a
 * human is dead time nobody is told about -- 62 of those over nine days in
 * the measured baseline. This is what turns that into a buzz in a pocket.
 *
 * Push messages from this dashboard carry NO payload. Encrypting one needs
 * AES-GCM, which the server's standard library does not have and which is not
 * worth hand-writing. So a push says only "something happened", and this
 * worker asks the dashboard what -- falling back to generic text when it
 * cannot reach it, which is the honest cost of not writing a cipher.
 */

const VERSION = 'v1';
const SHELL = 'shell-' + VERSION;
const AUTH = 'auth-' + VERSION;
const TOKEN_KEY = '/__dashboard_token';

/* The shell, not the data. Caching /api/ responses would mean opening the app
 * to a confident-looking board that is hours stale, which is worse than a
 * spinner. */
const SHELL_FILES = [
  '/', '/index.html', '/style.css', '/theme.css', '/app.js',
  '/manifest.webmanifest', '/icon-192.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL)
      /* addAll is all-or-nothing: one 404 and the whole worker fails to
       * install. Added one at a time so a missing optional file cannot cost
       * us the push handler, which is the part that matters. */
      .then((cache) => Promise.all(
        SHELL_FILES.map((url) => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => n !== SHELL && n !== AUTH)
             .map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

/* The page hands us the token after it has one. It has to be persisted rather
 * than held in memory: a worker is killed between events, and a push may
 * arrive days after the page that registered it was closed. */
self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'token') {
    event.waitUntil(
      caches.open(AUTH).then((cache) => cache.put(
        TOKEN_KEY, new Response(String(data.token || ''))))
    );
  }
});

function storedToken() {
  return caches.open(AUTH)
    .then((cache) => cache.match(TOKEN_KEY))
    .then((hit) => (hit ? hit.text() : ''))
    .catch(() => '');
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  /* Data is network-only. A cached chart is a lie with a timestamp on it. */
  if (url.pathname.startsWith('/api/')) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(SHELL).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request)
        .then((hit) => hit || caches.match('/index.html')))
  );
});

self.addEventListener('push', (event) => {
  event.waitUntil(
    storedToken()
      .then((token) => fetch('/api/escalations', {
        headers: token ? { 'X-Dashboard-Token': token } : {},
        cache: 'no-store',
      }))
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null)
      .then((payload) => self.registration.showNotification(
        ...describe(payload)))
  );
});

/* Turns whatever we managed to learn into a notification. Every branch
 * produces something: a push that shows nothing is worse than a vague one,
 * because the user is left knowing only that their phone buzzed. */
function describe(payload) {
  const rows = (payload && payload.rows) || null;
  const options = {
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: 'escalation',          /* replaces, never stacks */
    renotify: true,
    requireInteraction: true,   /* someone is waiting; do not auto-dismiss */
    data: { url: '/' },
  };

  if (rows === null) {
    return ['An agent needs you',
            Object.assign(options, { body: 'Open the dashboard to see which.' })];
  }
  if (rows.length === 0) {
    /* It unblocked between the push and this fetch. Say so rather than
     * inventing an escalation that is already over. */
    return ['An agent was waiting',
            Object.assign(options, { body: 'It is no longer blocked.' })];
  }
  if (rows.length === 1) {
    const row = rows[0];
    return [row.project + ' needs you',
            Object.assign(options, {
              body: row.message || 'Waiting for an answer.',
              data: { url: '/' },
            })];
  }
  return [rows.length + ' agents need you',
          Object.assign(options, {
            body: rows.map((r) => r.project).join(', '),
          })];
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data || {}).url || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((windows) => {
        /* Focus the dashboard if it is already open rather than opening a
         * second copy of it. */
        for (const client of windows) {
          if (client.url.indexOf(self.location.origin) === 0 && client.focus) {
            return client.focus();
          }
        }
        return self.clients.openWindow(target);
      })
  );
});
