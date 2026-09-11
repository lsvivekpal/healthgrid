"""Small, self-contained TOTP helpers used by the staff MFA flow."""

import base64
import io
import secrets
from datetime import datetime, timezone as dt_timezone

import pyotp
import qrcode
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from qrcode.image.svg import SvgPathImage

from .models import UserMFA

PENDING_USER_SESSION_KEY = "mfa_pending_user_id"
PENDING_BACKEND_SESSION_KEY = "mfa_pending_backend"
PENDING_NEXT_SESSION_KEY = "mfa_pending_next"
MFA_VERIFIED_AT_SESSION_KEY = "mfa_verified_at"
MFA_STEP_UP_MAX_AGE_SECONDS = 300


def generate_backup_codes(count=10):
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return ["".join(secrets.choice(alphabet) for _ in range(10)) for _ in range(count)]


def new_enrollment(profile):
    """Rotate a pending enrollment secret and its one-time recovery codes."""
    secret = pyotp.random_base32()
    codes = generate_backup_codes()
    profile.set_secret(secret)
    profile.set_pending_backup_codes(codes)
    profile.backup_codes = []
    profile.enabled = False
    profile.confirmed_at = None
    profile.save(update_fields=[
        "secret_encrypted", "pending_backup_codes_encrypted", "backup_codes",
        "enabled", "confirmed_at", "updated_at",
    ])
    return secret, codes


def provisioning_uri(user, secret):
    account = user.email or user.get_username()
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name="RDS Dashboard")


def qr_data_uri(value):
    qr = qrcode.QRCode(version=None, box_size=5, border=2)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def verify_totp(profile, code):
    if not profile.secret_encrypted:
        return False
    code = (code or "").replace(" ", "").strip()
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(profile.get_secret()).verify(code, valid_window=1)


def consume_backup_code(profile, code):
    candidate = (code or "").replace(" ", "").strip().upper()
    if not candidate or not profile.backup_codes:
        return False
    for index, hashed in enumerate(profile.backup_codes):
        if check_password(candidate, hashed):
            profile.backup_codes.pop(index)
            profile.save(update_fields=["backup_codes", "updated_at"])
            return True
    return False


def verify_code(profile, code, allow_backup=True):
    if verify_totp(profile, code):
        return True
    return allow_backup and consume_backup_code(profile, code)


def confirm_enrollment(profile, code):
    """Confirm the current pending TOTP, then hash and retain recovery codes."""
    if profile.enabled or not verify_totp(profile, code):
        return False
    pending_codes = profile.get_pending_backup_codes()
    if not pending_codes:
        return False
    profile.backup_codes = [make_password(code) for code in pending_codes]
    profile.pending_backup_codes_encrypted = ""
    profile.enabled = True
    profile.confirmed_at = datetime.now(dt_timezone.utc)
    profile.save(update_fields=[
        "backup_codes", "pending_backup_codes_encrypted", "enabled",
        "confirmed_at", "updated_at",
    ])
    return True

def mark_session_verified(request):
    request.session[MFA_VERIFIED_AT_SESSION_KEY] = timezone.now().timestamp()


def has_recent_session_verification(request):
    try:
        verified_at = float(request.session.get(MFA_VERIFIED_AT_SESSION_KEY, 0))
    except (TypeError, ValueError):
        return False
    return 0 <= timezone.now().timestamp() - verified_at <= MFA_STEP_UP_MAX_AGE_SECONDS


def require_mfa_for_action(request, code=""):
    """Accept a recent login MFA or validate a code supplied for this action."""
    try:
        profile = request.user.mfa_profile
    except UserMFA.DoesNotExist:
        return False
    if not profile.enabled:
        return False
    code = (code or "").strip()
    if code:
        if not verify_code(profile, code):
            return False
        mark_session_verified(request)
        return True
    return has_recent_session_verification(request)
