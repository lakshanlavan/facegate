/**
 * sw.js — CodeGloFix Service Worker
 *
 * Strategy:
 *   Static assets (CSS, JS, logo)  → Cache-first (fast on repeat visits)
 *   API calls / video feed          → Network-only  (always live data)
 *   HTML pages                      → Network-first, fall back to cache
 *
 * To force a cache refresh: bump CACHE_VERSION below and redeploy.
 */

const CACHE_VERSION = 'v1';
const CACHE_NAME    = 'codeglofix-' + CACHE_VERSION;

const STATIC_PRECACHE = [
  '/static/css/theme.css',
  '/static/js/common.js',
  '/static/js/index.js',
  '/static/js/admin.js',
  '/static/js/enrol.js',
  '/static/codeglofix.png',
  '/static/manifest.json',
];

// ── Install: pre-cache static assets ─────────────────────────────────────────
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_PRECACHE).catch(err => {
        console.warn('[SW] Some precache assets failed:', err);
      });
    })
  );
});

// ── Activate: remove old caches ───────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: routing strategy ───────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin GET requests
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  // API calls and video feed → always live (never cache)
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/video_feed')) {
    return; // fall through to network
  }

  // Static assets → cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(request, clone));
        }
        return res;
      }))
    );
    return;
  }

  // HTML pages → network-first, fall back to cache
  event.respondWith(
    fetch(request)
      .then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(request, clone));
        }
        return res;
      })
      .catch(() => caches.match(request))
  );
});
