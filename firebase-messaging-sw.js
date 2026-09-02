// firebase-messaging-sw.js
//
// This file MUST sit at the site root (https://ho-om.in/firebase-messaging-sw.js) — the browser
// will not look anywhere else for it. It handles pushes that arrive while the app is CLOSED or in
// the background, which is the entire point of push notifications for HO-Om: a hostel owner is not
// sitting with the app open, so an in-app-only alert reaches them hours too late.
//
// It runs separately from service-worker.js (the offline/caching one). Both can coexist.

importScripts('https://www.gstatic.com/firebasejs/10.8.1/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.8.1/firebase-messaging-compat.js');

firebase.initializeApp({
  apiKey: "AIzaSyCzZ_oeEtxEYFZw2ZBXymQ1eZYS_0YINFM",
  authDomain: "hostel-om.firebaseapp.com",
  projectId: "hostel-om",
  storageBucket: "hostel-om.firebasestorage.app",
  messagingSenderId: "111899182724",
  appId: "1:111899182724:web:88b31e9979750b010624d1"
});

const messaging = firebase.messaging();

// Fires when a push arrives and the app is not in the foreground.
messaging.onBackgroundMessage((payload) => {
  const n = payload.notification || {};
  const d = payload.data || {};

  self.registration.showNotification(n.title || 'HO-Om', {
    body: n.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    // Same tag replaces an earlier notification instead of stacking five of them for one chat.
    tag: d.bookingId || d.type || 'hoom',
    renotify: true,
    data: { url: d.url || '/' },
  });
});

// Tapping the notification should reuse an already-open tab rather than piling up new ones.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(target);
    })
  );
});
