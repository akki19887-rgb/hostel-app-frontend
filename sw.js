// Simple service worker — mostly just enables "installability".
// Caches the app shell so the icon/splash still works briefly if offline;
// does NOT cache Firestore data or the AI backend calls (those always need network).
const CACHE_NAME = 'hostelom-v1';
const APP_SHELL = ['./index.html', './manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Only intercept same-origin GET requests (the app's own pages/assets).
  // Cross-origin calls (backend API — Aadhar extraction, registration PDFs,
  // OTP, etc.) and any non-GET request (POST/PUT/DELETE) must pass straight
  // through untouched — intercepting them and returning an unresolved/undefined
  // Response is what was breaking Aadhar photo uploads with a
  // "Failed to fetch" / false CORS error.
  if (event.request.method !== 'GET' || new URL(event.request.url).origin !== self.location.origin) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
