from django.http import HttpResponse, JsonResponse


MANIFEST = {
    "name": "HealthGrid Database Observability",
    "short_name": "HealthGrid",
    "description": "Database lock and session observability dashboard.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f4f5f7",
    "theme_color": "#2563eb",
    "orientation": "any",
    "icons": [
        {"src": "/static/monitor/pwa/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any maskable"},
        {"src": "/static/monitor/pwa/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any maskable"},
    ],
}


SERVICE_WORKER = r"""
const CACHE_NAME = "healthgrid-static-v2";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
    )).then(() => self.clients.claim())
  );
});

// Network-first: dev/DEBUG static URLs are unhashed (same URL, changing content
// across deploys), so cache-first would pin a stale response indefinitely once
// cached. Cache is only a fallback for when the network is unavailable.
self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) {
    return;
  }

  event.respondWith(
    fetch(request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
      }
      return response;
    }).catch(() => caches.match(request))
  );
});
""".strip()


def service_worker(request):
    response = HttpResponse(SERVICE_WORKER, content_type="application/javascript")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Service-Worker-Allowed"] = "/"
    return response


def manifest(request):
    response = JsonResponse(MANIFEST)
    response["Content-Type"] = "application/manifest+json"
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response
