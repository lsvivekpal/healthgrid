from urllib.parse import quote

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse

from .mfa import has_recent_session_verification
from .models import UserMFA


class AdminMFARequiredMiddleware:
    """Route admin sessions through the same per-user MFA verification."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
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
