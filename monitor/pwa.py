from django.http import HttpResponse


SERVICE_WORKER = r"""
const CACHE_NAME = "healthgrid-static-v1";

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

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Never cache dashboard HTML, HTMX responses, APIs, or POST requests.
  if (request.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) {
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
      }
      return response;
    }))
  );
});
""".strip()


def service_worker(request):
    response = HttpResponse(SERVICE_WORKER, content_type="application/javascript")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Service-Worker-Allowed"] = "/"
    return response
