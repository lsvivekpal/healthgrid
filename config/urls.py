from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse, JsonResponse
from django.urls import include, path

from monitor.auth_views import DashboardLoginView, mfa_verify
from monitor.pwa import manifest, service_worker


def healthz(request):
    return JsonResponse({"status": "ok"})


def robots_txt(request):
    response = HttpResponse("User-agent: *\nDisallow:\n", content_type="text/plain")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


def sitemap_xml(request):
    response = HttpResponse(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>\n',
        content_type="application/xml",
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("robots.txt", robots_txt, name="robots-txt"),
    path("sitemap.xml", sitemap_xml, name="sitemap-xml"),
    path("sw.js", service_worker, name="service-worker"),
    path("manifest.webmanifest", manifest, name="pwa-manifest"),
    path("login/", DashboardLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("mfa/verify/", mfa_verify, name="mfa-verify"),
    path("", include("monitor.urls")),
]
