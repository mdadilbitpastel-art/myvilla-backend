"""
Django settings for the MyVilla backend.

12-factor style: everything environment-specific is read from env vars
(with sensible local defaults), so the same image runs in dev and prod.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["*"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:3000"]),
    ACCESS_TOKEN_LIFETIME_MIN=(int, 60),
    REFRESH_TOKEN_LIFETIME_DAYS=(int, 7),
)

# Load .env if present (does not override real environment variables).
environ.Env.read_env(BASE_DIR / ".env")

# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
SECRET_KEY = env("SECRET_KEY", default="dev-insecure-secret-key")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "corsheaders",
    # Local
    "accounts",
    "properties",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    # Edge guard: IP blocking, rate limiting, malicious-probe rejection.
    # Sits just below CORS so blocked responses still carry CORS headers.
    "config.middleware.SecurityGuardMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Serve collected static files (admin assets) directly from the app,
    # so DEBUG=False in production still delivers CSS/JS. Must sit right
    # after SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------- #
# Database — Postgres in Docker, SQLite fallback for local tooling
# --------------------------------------------------------------------------- #
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env("ACCESS_TOKEN_LIFETIME_MIN")),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env("REFRESH_TOKEN_LIFETIME_DAYS")),
    "ROTATE_REFRESH_TOKENS": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "SIGNING_KEY": SECRET_KEY,
}

# --------------------------------------------------------------------------- #
# Internationalization
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- #
# Media / uploaded files — villa images (2-in-1: local dev vs production)
#
# Rule: if CLOUDINARY_URL is set (production), every uploaded image is stored on
# Cloudinary and served from its CDN. If it's empty (local dev), files are
# written to MEDIA_ROOT on disk and served by Django at MEDIA_URL. The model
# code is identical in both cases — only the storage backend swaps.
#
#   CLOUDINARY_URL=cloudinary://<api_key>:<api_secret>@<cloud_name>
# --------------------------------------------------------------------------- #
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

CLOUDINARY_URL = env("CLOUDINARY_URL", default="")

if CLOUDINARY_URL:
    # cloudinary reads CLOUDINARY_URL from the environment automatically.
    INSTALLED_APPS += ["cloudinary_storage", "cloudinary"]
    STORAGES = {
        "default": {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

# --------------------------------------------------------------------------- #
# CORS — allow the Next.js frontend
# --------------------------------------------------------------------------- #
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

# --------------------------------------------------------------------------- #
# Email — password-reset messages (2-in-1: local dev vs production)
#
# Rule: if an SMTP host is configured (EMAIL_HOST set, e.g. in production),
# real email is sent over SMTP. If it's empty (local dev), Django falls back to
# the console backend which prints the message — including the reset link —
# to the `web` container logs, so you can test without any mail server.
# You can always force a backend explicitly via EMAIL_BACKEND.
# --------------------------------------------------------------------------- #
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default=(
        "django.core.mail.backends.smtp.EmailBackend"
        if EMAIL_HOST
        else "django.core.mail.backends.console.EmailBackend"
    ),
)
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default="MyVilla <no-reply@myvilla.com>"
)

# Frontend base URL — used to build the password-reset link in the email.
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")

# How long a password-reset token stays valid (seconds). Default: 15 minutes.
PASSWORD_RESET_TIMEOUT = env.int("PASSWORD_RESET_TIMEOUT", default=15 * 60)

# --------------------------------------------------------------------------- #
# Cache — backs rate limiting / IP blocking.
# LocMemCache is per-process (fine for dev / single worker). In production set
# REDIS_URL so all workers share one counter (dependency-free RedisCache).
# --------------------------------------------------------------------------- #
_redis_url = env("REDIS_URL", default="")
if _redis_url:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": _redis_url}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "myvilla-cache"}}

# --------------------------------------------------------------------------- #
# Rate-limit / abuse tunables (read by config.middleware.SecurityGuardMiddleware)
# --------------------------------------------------------------------------- #
RL_GENERAL_LIMIT = env.int("RL_GENERAL_LIMIT", default=120)      # req / window / IP
RL_GENERAL_WINDOW = env.int("RL_GENERAL_WINDOW", default=60)     # seconds
RL_AUTH_LIMIT = env.int("RL_AUTH_LIMIT", default=12)             # auth req / window / IP
RL_AUTH_WINDOW = env.int("RL_AUTH_WINDOW", default=300)          # seconds
RL_BLOCK_SECONDS = env.int("RL_BLOCK_SECONDS", default=900)      # IP ban duration
RL_VIOLATION_LIMIT = env.int("RL_VIOLATION_LIMIT", default=5)    # strikes → ban
RL_VIOLATION_WINDOW = env.int("RL_VIOLATION_WINDOW", default=300)

# --------------------------------------------------------------------------- #
# Hardening
# --------------------------------------------------------------------------- #
# Reject oversized request bodies early (mitigates memory-exhaustion attacks).
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=2 * 1024 * 1024)  # 2 MB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# TLS-facing hardening — enabled automatically outside DEBUG (behind a proxy
# terminating HTTPS, also set SECURE_PROXY_SSL_HEADER via env if needed).
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
