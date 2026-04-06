// AFTR Service Worker — Push notifications
self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data.json(); } catch (e) { data = { title: 'AFTR', body: event.data ? event.data.text() : '' }; }

  var title = data.title || 'AFTR Pick';
  var options = {
    body:    data.body  || '',
    icon:    data.icon  || '/static/logo_aftr.png',
    badge:   '/static/logo_aftr.png',
    tag:     data.tag   || 'aftr-pick',
    data:    { url: data.url || '/' },
    vibrate: [200, 100, 200],
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
});
