// Simple service worker — mostly just enables "installability".
// Caches the app shell so the icon/splash still works briefly if offline;
// does NOT cache Firestore data or the AI backend calls (those always need network).
const CACHE_NAME = 'hostelom-v2';
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

  // HAMESHA EK ASLI Response LAUTANA HAI.
  //
  // Pehle yahan `fetch(...).catch(() => caches.match(...))` tha. caches.match()
  // kuch na mile to `undefined` lautata hai, aur respondWith(undefined) browser
  // ke liye galat hai:
  //     "Failed to convert value to 'Response'"
  //     "The FetchEvent for https://ho-om.in/ resulted in a network error response"
  // Yani net ek pal ke liye jhapka bhi, to page khulna hi band ho jata tha -
  // browser ka apna "no internet" wala page bhi nahi aata tha.
  //
  // Ek aur baat: cache me './index.html' padi hai, par ho-om.in/ ki request ka
  // pata 'https://ho-om.in/' hai - dono ek nahi hain, isliye seedha match kabhi
  // nahi hota tha. Isliye navigation ke liye shell alag se dekhte hain.
  event.respondWith((async () => {
    try {
      return await fetch(event.request);
    } catch (e) {
      const hit = await caches.match(event.request);
      if (hit) return hit;
      if (event.request.mode === 'navigate') {
        const shell = await caches.match('./index.html');
        if (shell) return shell;
      }
      return new Response(
        'Aap abhi offline hain. Internet aate hi page dobara khul jayega.',
        { status: 503, statusText: 'Offline',
          headers: { 'Content-Type': 'text/plain; charset=utf-8' } }
      );
    }
  })());
});
