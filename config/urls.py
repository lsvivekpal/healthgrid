from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import JsonResponse
from django.urls import include, path

from monitor.auth_views import DashboardLoginView, mfa_verify
from monitor.pwa import manifest, service_worker


def healthz(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("sw.js", service_worker, name="service-worker"),
    path("manifest.webmanifest", manifest, name="pwa-manifest"),
    path("login/", DashboardLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("mfa/verify/", mfa_verify, name="mfa-verify"),
    path("", include("monitor.urls")),
]
