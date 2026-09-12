import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "local-only-insecure-secret"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false")

allowed_hosts_value = os.environ.get("DJANGO_ALLOWED_HOSTS", "")
if not allowed_hosts_value and not DEBUG:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must list the public hostname in production")
ALLOWED_HOSTS = [h.strip() for h in (allowed_hosts_value or "localhost,127.0.0.1").split(",") if h.strip()]

database_url = os.environ.get("DATABASE_URL", "")
if not database_url and not DEBUG:
    raise ImproperlyConfigured("DATABASE_URL must be set when DJANGO_DEBUG is false")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "monitor",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "monitor.middleware.AdminMFARequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Serve the files collected into the image even with DEBUG=False. Hashed URLs
# ensure browsers fetch the matching stylesheet after each UI deployment.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "instance-list"
LOGOUT_REDIRECT_URL = "login"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# Cookie-based sessions: zero session-table writes/growth in the app DB, no cron
# cleanup needed. Trade-off: no server-side forced logout, ~4KB session size cap
# (fine here — we only ever store the auth user id + small messages).
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "rds-dashboard-cache",
    }
}

# TTL for caching polled activity results, in seconds.
ACTIVITY_CACHE_TTL = int(os.environ.get("ACTIVITY_CACHE_TTL", "10"))

# Fernet key encrypting stored RDS instance passwords at rest. Dev-only default below —
# set a real key (Fernet.generate_key()) via env var in any shared/deployed environment,
# changing it makes existing stored passwords undecryptable.
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")
if not ENCRYPTION_KEY:
    if DEBUG:
        ENCRYPTION_KEY = "REDACTED_DEV_KEY"
    else:
        raise ImproperlyConfigured("ENCRYPTION_KEY must be set when DJANGO_DEBUG is false")

# Domains allowed to POST here (Django 4+ require this even for same-origin
# requests through a reverse proxy). Comma-separated, must include scheme.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

# Set true only when nginx actually terminates TLS (serves https://). If nginx
# is plain HTTP, leave false — Secure cookies get silently dropped by the
# browser over HTTP, which looks exactly like a CSRF failure.
USE_HTTPS = os.environ.get("DJANGO_USE_HTTPS", "false").lower() == "true"
if USE_HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    if not USE_HTTPS:
        raise ImproperlyConfigured("DJANGO_USE_HTTPS=true is required in production")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"

# Production requires users to finish authenticator enrollment before using the
# dashboard. Local development keeps the old workflow for test fixtures.
MFA_REQUIRED = os.environ.get("DJANGO_MFA_REQUIRED", "true").lower() == "true"

# Comma-separated Teams/Power Automate hostnames or suffixes. Keep this narrow
# in production; the sender also blocks private/reserved destinations.
WEBHOOK_ALLOWED_HOSTS = tuple(
    host.strip().lower()
    for host in os.environ.get("WEBHOOK_ALLOWED_HOSTS", "").split(",")
    if host.strip()
)
if not WEBHOOK_ALLOWED_HOSTS and not DEBUG:
    raise ImproperlyConfigured("WEBHOOK_ALLOWED_HOSTS must be set in production")

# Verify the database server certificate for monitored TLS connections. Set
# DB_SSL_ROOT_CERT to the mounted AWS RDS CA bundle when the base image does
# not already trust it.
DB_SSL_MODE = os.environ.get("DB_SSL_MODE", "prefer" if DEBUG else "verify-full")
DB_SSL_ROOT_CERT = os.environ.get("DB_SSL_ROOT_CERT", "")
