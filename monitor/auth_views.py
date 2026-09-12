from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from .mfa import (
    PENDING_BACKEND_SESSION_KEY,
    PENDING_NEXT_SESSION_KEY,
    PENDING_USER_SESSION_KEY,
    confirm_enrollment,
    new_enrollment,
    provisioning_uri,
    qr_data_uri,
    verify_code,
    mark_session_verified,
)
from .models import UserMFA


LOGIN_THROTTLE_LIMIT = 10
LOGIN_THROTTLE_SECONDS = 15 * 60


def _auth_throttle_key(request, subject):
    address = request.META.get("REMOTE_ADDR", "unknown")
    return f"auth-fail:{address}:{str(subject).strip().lower()[:150]}"


def _is_throttled(key):
    return int(cache.get(key, 0) or 0) >= LOGIN_THROTTLE_LIMIT


def _record_failure(key):
    try:
        cache.incr(key)
    except ValueError:
        cache.add(key, 1, LOGIN_THROTTLE_SECONDS)


def _clear_failures(key):
    cache.delete(key)


class DashboardLoginView(LoginView):
    template_name = "monitor/login.html"

    def post(self, request, *args, **kwargs):
        subject = request.POST.get("username", "")
        if _is_throttled(_auth_throttle_key(request, subject)):
            form = self.get_form()
            form.add_error(None, "Too many sign-in attempts. Try again in 15 minutes.")
            return self.render_to_response(self.get_context_data(form=form))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        subject = self.request.POST.get("username", "")
        _record_failure(_auth_throttle_key(self.request, subject))
        return super().form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        _clear_failures(_auth_throttle_key(self.request, user.get_username()))
        try:
            profile = user.mfa_profile
        except UserMFA.DoesNotExist:
            profile, _created = UserMFA.objects.get_or_create(user=user)
        if not profile.enabled:
            response = super().form_valid(form)
            if response.status_code in (301, 302, 303, 307, 308):
                return redirect("mfa-setup")
            return response
        if profile.enabled:
            next_url = self.get_redirect_url()
            if next_url and not url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={self.request.get_host()}
            ):
                next_url = ""
            self.request.session[PENDING_USER_SESSION_KEY] = user.pk
            self.request.session[PENDING_BACKEND_SESSION_KEY] = user.backend
            self.request.session[PENDING_NEXT_SESSION_KEY] = next_url
            return redirect("mfa-verify")
        return super().form_valid(form)


@require_http_methods(["GET", "POST"])
def mfa_verify(request):
    if request.user.is_authenticated:
        return redirect("instance-list")
    user_id = request.session.get(PENDING_USER_SESSION_KEY)
    if not user_id:
        return redirect("login")
    try:
        user = get_user_model().objects.get(pk=user_id, is_active=True)
        profile = user.mfa_profile
    except (get_user_model().DoesNotExist, UserMFA.DoesNotExist):
        request.session.flush()
        return redirect("login")

    error = ""
    if request.method == "POST":
        code = request.POST.get("code", "")
        throttle_key = _auth_throttle_key(request, f"mfa:{user.pk}")
        if _is_throttled(throttle_key):
            error = "Too many verification attempts. Try again in 15 minutes."
        elif verify_code(profile, code):
            _clear_failures(throttle_key)
            next_url = request.session.get(PENDING_NEXT_SESSION_KEY) or reverse("instance-list")
            backend = request.session.get(PENDING_BACKEND_SESSION_KEY) or settings.AUTHENTICATION_BACKENDS[0]
            login(request, user, backend=backend)
            mark_session_verified(request)
            for key in (PENDING_USER_SESSION_KEY, PENDING_BACKEND_SESSION_KEY, PENDING_NEXT_SESSION_KEY):
                request.session.pop(key, None)
            return redirect(next_url)
        else:
            _record_failure(throttle_key)
            error = "That authenticator or recovery code is invalid. Try again."
    return render(request, "monitor/mfa_verify.html", {"error": error})


@login_required
@require_http_methods(["GET", "POST"])
def mfa_setup(request):
    profile, _created = UserMFA.objects.get_or_create(user=request.user)
    error = ""
    if request.method == "POST":
        action = request.POST.get("action", "confirm")
        if action == "regenerate":
            new_enrollment(profile)
            messages.success(request, "A new authenticator setup secret was generated. The previous secret is no longer valid.")
            return redirect("mfa-setup")
        if action == "reset" and profile.enabled:
            if verify_code(profile, request.POST.get("code", "")):
                new_enrollment(profile)
                messages.success(request, "MFA was reset. Scan the new QR code and confirm it to enable MFA again.")
                return redirect("mfa-setup")
            error = "Enter a valid current authenticator or recovery code to reset MFA."
        elif not profile.enabled:
            if action == "confirm" and confirm_enrollment(profile, request.POST.get("code", "")):
                messages.success(request, "Google Authenticator MFA is enabled for your account.")
                return redirect("mfa-setup")
            error = "Enter the 6-digit code shown by your authenticator app to confirm setup."

    if not profile.enabled and (not profile.secret_encrypted or not profile.pending_backup_codes_encrypted):
        new_enrollment(profile)
    context = {"profile": profile, "error": error}
    if not profile.enabled:
        secret = profile.get_secret()
        context.update(
            secret=secret,
            provisioning_uri=provisioning_uri(request.user, secret),
            qr_data_uri=qr_data_uri(provisioning_uri(request.user, secret)),
            backup_codes=profile.get_pending_backup_codes(),
        )
    return render(request, "monitor/mfa_setup.html", context)
