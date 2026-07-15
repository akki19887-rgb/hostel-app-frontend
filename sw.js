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
  // Network-first for everything (Firestore/Storage/API calls need to always be live).
  // Falls back to cache only if the network is unavailable.
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
