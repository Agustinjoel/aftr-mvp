const CACHE_NAME = "aftr-v2";

const ASSETS = [
  "/static/style.css?v=10",
  "/static/logo_aftr.png",
  "/static/manifest.webmanifest",
  "/static/pwa/icon-192.png",
  "/static/pwa/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Para API: siempre a red (dashboard data fresh)
  if (req.url.includes("/api/")) {
    event.respondWith(fetch(req).catch(() => caches.match(req)));
    return;
  }

  // Static: cache-first
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req))
  );
});