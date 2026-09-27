from secrets import token_urlsafe
from urllib.parse import quote

from django.contrib.auth import logout
from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

from .mfa import has_recent_session_verification
from .models import UserMFA


class BrowserSecurityHeadersMiddleware:
    """Set a same-origin browser policy on application and static responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = token_urlsafe(16)
        response = self.get_response(request)
        response.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            f"script-src 'self' 'nonce-{request.csp_nonce}' https://unpkg.com; "
            "script-src-attr 'unsafe-inline'; "
            f"style-src 'self' 'nonce-{request.csp_nonce}'; style-src-attr 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'",
        )
        response.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
        response.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
        response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        return response


class AdminMFARequiredMiddleware:
    """Route admin sessions through the same per-user MFA verification."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            settings.MFA_REQUIRED
            and request.user.is_authenticated
            and request.path not in {"/logout/", "/settings/security/mfa/"}
            and not request.path.startswith("/admin/login")
        ):
            try:
                profile = request.user.mfa_profile
            except UserMFA.DoesNotExist:
                profile = None
            if not profile or not profile.enabled:
                return redirect(f"{reverse('mfa-setup')}?next={quote(request.get_full_path(), safe='/?:=&')}")
        if (
            request.path.startswith("/admin/")
            and not request.path.startswith("/admin/login")
            and not request.path.startswith("/admin/logout")
            and request.user.is_authenticated
        ):
            try:
                profile = request.user.mfa_profile
            except UserMFA.DoesNotExist:
                profile = None
            if not profile or not profile.enabled or not has_recent_session_verification(request):
                next_url = quote(request.get_full_path(), safe="/?=&")
                logout(request)
                return redirect(f"{reverse('login')}?next={next_url}")
        return self.get_response(request)
