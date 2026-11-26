"""Django settings for octoreview.

Every value comes from the environment (docs/spec.md section 15; `.env.example` lists each one).
A misconfiguration fails at boot with an `ImproperlyConfigured` message that names the variable
and ends with the fix.
"""

from __future__ import annotations

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_GENERATOR = 'python -c "import secrets; print(secrets.token_urlsafe(48))"'


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer; set it to a whole number.") from exc
    if minimum is not None and parsed < minimum:
        raise ImproperlyConfigured(f"{name} must be at least {minimum}; raise it.")
    return parsed


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def require_secret(name: str, *, debug_default: str | None) -> str:
    """A secret that may fall back to a fixed development value only when DEBUG is on."""

    value = os.getenv(name, "")
    if value:
        if not DEBUG and debug_default is not None and value == debug_default:
            raise ImproperlyConfigured(
                f"{name} uses the development default while DJANGO_DEBUG is off; "
                f"generate one with {SECRET_GENERATOR}"
            )
        return value
    if DEBUG and debug_default is not None:
        return debug_default
    raise ImproperlyConfigured(f"{name} is required; generate one with {SECRET_GENERATOR}")


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = require_secret("DJANGO_SECRET_KEY", debug_default="local-dev-secret-key")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS") or (["*"] if DEBUG else [])

INSTALLED_APPS = [
    # Unfold precedes the admin so its templates win. BasicAppConfig, not unfold's default
    # config, which would swap in a bare UnfoldAdminSite and drop the superuser-only gate in
    # config/adminsite.py (copied from octonomy).
    "unfold.apps.BasicAppConfig",
    "config.admin.OctoreviewAdminConfig",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "octoreview.core",
    "octoreview.service_auth",
    "octoreview.audit",
    "octoreview.events",
    "octoreview.entities",
    "octoreview.reviews",
    "octoreview.media",
    "octoreview.lifecycle",
    "octoreview.openapi",
    "octoreview.console",
    "corsheaders",
]

# Sessions, CSRF, auth and messages serve the optional admin only. The REST API is unaffected:
# DRF has no session authentication here (service keys are the only credential), and DRF views
# are CSRF-exempt (tests/console/test_rest_unaffected.py). WhiteNoise sits right after
# SecurityMiddleware, as it documents, so a static hit skips the rest of the chain.
MIDDLEWARE = [
    "octoreview.core.middleware.RequestContextMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

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
            ]
        },
    }
]

# The opt-in operator console (docs/operations.md "Admin console"). Mounted at /admin/ only when
# true; the site admits active superusers only (config/adminsite.py). Off in the image, on in
# compose (docs/spec.md section 15).
ADMIN_ENABLED = env_bool("OCTOREVIEW_ADMIN_ENABLED", False)
UNFOLD = {
    "SITE_TITLE": "Octoreview",
    "SITE_HEADER": "Octoreview",
    "SITE_SUBHEADER": "Operator console",
    "SITE_URL": "https://github.com/octoverse-id/octoreview",
}

# Bundled static files only (the admin's), collected into the image and served by WhiteNoise.
# Compressed but not manifest-hashed, so a page renders even where collectstatic never ran
# (tests, `runserver`); WhiteNoise then serves from the app finders when DEBUG is on.
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=env_int("DATABASE_CONN_MAX_AGE", 60, minimum=0),
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
APPEND_SLASH = False

REST_FRAMEWORK = {
    # No SessionAuthentication: service keys are the only credential (docs/spec.md section 2).
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["octoreview.service_auth.permissions.BearerTokenPermission"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "octoreview.core.errors.exception_handler",
    "DEFAULT_PAGINATION_CLASS": "octoreview.core.pagination.OctoreviewLimitOffsetPagination",
    "UNAUTHENTICATED_USER": None,
    # rating_average and other decimals serialize as JSON numbers (docs/spec.md section 5.1).
    "COERCE_DECIMAL_TO_STRING": False,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Octoreview API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# Service keys (docs/spec.md section 2). Only a peppered HMAC of each key is stored.
SERVICE_TOKEN_PEPPER = require_secret(
    "OCTOREVIEW_SERVICE_TOKEN_PEPPER", debug_default="local-dev-service-token-pepper"
)

# Erasure pseudonymisation key (docs/spec.md section 9). Never rotate it: re-runs match the
# erased form, which depends on it.
ERASURE_HMAC_KEY = require_secret(
    "OCTOREVIEW_ERASURE_HMAC_KEY", debug_default="local-dev-erasure-hmac-key"
)

# Outbox (docs/spec.md section 10).
OUTBOX_TRANSPORT = os.getenv("OCTOREVIEW_OUTBOX_TRANSPORT", "logging")
OUTBOX_WEBHOOK_URL = os.getenv("OCTOREVIEW_WEBHOOK_URL", "")
OUTBOX_WEBHOOK_SECRET = os.getenv("OCTOREVIEW_WEBHOOK_SECRET", "")
OUTBOX_WEBHOOK_TIMEOUT_SECONDS = env_int("OCTOREVIEW_WEBHOOK_TIMEOUT_SECONDS", 10, minimum=1)
OUTBOX_MAX_ATTEMPTS = env_int("OCTOREVIEW_OUTBOX_MAX_ATTEMPTS", 5, minimum=1)
OUTBOX_RETRY_BASE_SECONDS = env_int("OCTOREVIEW_OUTBOX_RETRY_BASE_SECONDS", 30, minimum=1)
OUTBOX_RETRY_MAX_SECONDS = env_int("OCTOREVIEW_OUTBOX_RETRY_MAX_SECONDS", 3600, minimum=1)
OUTBOX_CLAIM_TIMEOUT_SECONDS = env_int("OCTOREVIEW_OUTBOX_CLAIM_TIMEOUT_SECONDS", 60, minimum=1)
if OUTBOX_TRANSPORT not in {"logging", "webhook"}:
    raise ImproperlyConfigured(
        "OCTOREVIEW_OUTBOX_TRANSPORT must be 'logging' or 'webhook'; pick one of them."
    )
if OUTBOX_TRANSPORT == "webhook":
    if not OUTBOX_WEBHOOK_URL:
        raise ImproperlyConfigured(
            "OCTOREVIEW_WEBHOOK_URL is required when OCTOREVIEW_OUTBOX_TRANSPORT=webhook; "
            "set it to the receiver's https URL."
        )
    if not OUTBOX_WEBHOOK_SECRET:
        raise ImproperlyConfigured(
            "OCTOREVIEW_WEBHOOK_SECRET is required when OCTOREVIEW_OUTBOX_TRANSPORT=webhook; "
            f"generate one with {SECRET_GENERATOR}"
        )
    if OUTBOX_CLAIM_TIMEOUT_SECONDS <= OUTBOX_WEBHOOK_TIMEOUT_SECONDS:
        raise ImproperlyConfigured(
            "OCTOREVIEW_OUTBOX_CLAIM_TIMEOUT_SECONDS must be greater than "
            "OCTOREVIEW_WEBHOOK_TIMEOUT_SECONDS; raise the claim timeout."
        )

# Media policy defaults and ceilings (docs/spec.md section 6.1). A default above its ceiling is a
# boot error, so every resolved policy is within bounds.
MEDIA_POLICY_CEILINGS = {
    "max_images": env_int("OCTOREVIEW_MAX_IMAGES_CEILING", 20, minimum=0),
    "max_videos": env_int("OCTOREVIEW_MAX_VIDEOS_CEILING", 5, minimum=0),
    "max_image_bytes": env_int("OCTOREVIEW_MAX_IMAGE_BYTES_CEILING", 52_428_800, minimum=1),
    "max_video_bytes": env_int("OCTOREVIEW_MAX_VIDEO_BYTES_CEILING", 524_288_000, minimum=1),
}
MEDIA_POLICY_DEFAULTS = {
    "max_images": env_int("OCTOREVIEW_DEFAULT_MAX_IMAGES", 5, minimum=0),
    "max_videos": env_int("OCTOREVIEW_DEFAULT_MAX_VIDEOS", 1, minimum=0),
    "max_image_bytes": env_int("OCTOREVIEW_DEFAULT_MAX_IMAGE_BYTES", 10_485_760, minimum=1),
    "max_video_bytes": env_int("OCTOREVIEW_DEFAULT_MAX_VIDEO_BYTES", 104_857_600, minimum=1),
}
for _field, _default in MEDIA_POLICY_DEFAULTS.items():
    if _default > MEDIA_POLICY_CEILINGS[_field]:
        raise ImproperlyConfigured(
            f"OCTOREVIEW_DEFAULT_{_field.upper()} ({_default}) exceeds "
            f"OCTOREVIEW_{_field.upper()}_CEILING ({MEDIA_POLICY_CEILINGS[_field]}); lower the "
            "default or raise the ceiling."
        )

# Storage and media (docs/spec.md sections 7, 8 and 15).
STORAGE_BACKEND = os.getenv("OCTOREVIEW_STORAGE_BACKEND", "local")
# azure and gcs arrive in release line 2.
SUPPORTED_STORAGE_BACKENDS = ("local", "s3")
if STORAGE_BACKEND not in SUPPORTED_STORAGE_BACKENDS:
    raise ImproperlyConfigured(
        f"OCTOREVIEW_STORAGE_BACKEND must be one of {', '.join(SUPPORTED_STORAGE_BACKENDS)}; "
        "pick one."
    )
STORAGE_LOCATION_ID = os.getenv("OCTOREVIEW_STORAGE_LOCATION_ID", "")
if not STORAGE_LOCATION_ID:
    raise ImproperlyConfigured(
        "OCTOREVIEW_STORAGE_LOCATION_ID is required; set a stable id for this bucket or "
        "directory, for example 'local-dev' or 'aws-ap-southeast-3-reviews'."
    )
PUBLIC_BASE_URL = os.getenv("OCTOREVIEW_PUBLIC_BASE_URL", "").rstrip("/")
MEDIA_ROOT = os.getenv("OCTOREVIEW_MEDIA_ROOT", "")
MEDIA_TOKEN_SECRET = os.getenv("OCTOREVIEW_MEDIA_TOKEN_SECRET", "")
MEDIA_TOKEN_SECRET_PREVIOUS = os.getenv("OCTOREVIEW_MEDIA_TOKEN_SECRET_PREVIOUS", "")
if STORAGE_BACKEND == "local":
    # Signs the local upload and download URLs; like the other secrets, it has a development
    # fallback only while DEBUG is on.
    MEDIA_TOKEN_SECRET = require_secret(
        "OCTOREVIEW_MEDIA_TOKEN_SECRET", debug_default="local-dev-media-token-secret"
    )
    for _name, _value, _fix in (
        ("OCTOREVIEW_PUBLIC_BASE_URL", PUBLIC_BASE_URL, "set the server's absolute base URL"),
        ("OCTOREVIEW_MEDIA_ROOT", MEDIA_ROOT, "set a writable directory for media"),
    ):
        if not _value:
            raise ImproperlyConfigured(
                f"{_name} is required with OCTOREVIEW_STORAGE_BACKEND=local; {_fix}."
            )
STORAGE_S3_BUCKET = os.getenv("OCTOREVIEW_STORAGE_S3_BUCKET", "")
STORAGE_S3_REGION = os.getenv("OCTOREVIEW_STORAGE_S3_REGION", "")
STORAGE_S3_ENDPOINT_URL = os.getenv("OCTOREVIEW_STORAGE_S3_ENDPOINT_URL", "").rstrip("/")
# Presigned URLs are signed for this host, which browsers and SDK callers must reach; the server
# itself talks to STORAGE_S3_ENDPOINT_URL (a compose service name, for example).
STORAGE_S3_PUBLIC_ENDPOINT_URL = (
    os.getenv("OCTOREVIEW_STORAGE_S3_PUBLIC_ENDPOINT_URL", "").rstrip("/")
    or STORAGE_S3_ENDPOINT_URL
)
STORAGE_S3_ADDRESSING_STYLE = os.getenv("OCTOREVIEW_STORAGE_S3_ADDRESSING_STYLE", "auto")
if STORAGE_BACKEND == "s3":
    for _name, _value, _fix in (
        ("OCTOREVIEW_STORAGE_S3_BUCKET", STORAGE_S3_BUCKET, "set the bucket name"),
        ("OCTOREVIEW_STORAGE_S3_REGION", STORAGE_S3_REGION, "set the bucket region"),
    ):
        if not _value:
            raise ImproperlyConfigured(
                f"{_name} is required with OCTOREVIEW_STORAGE_BACKEND=s3; {_fix}."
            )
    if STORAGE_S3_ADDRESSING_STYLE not in {"auto", "path", "virtual"}:
        raise ImproperlyConfigured(
            "OCTOREVIEW_STORAGE_S3_ADDRESSING_STYLE must be auto, path or virtual; use path for "
            "a store reached by a bare host name (a compose service)."
        )
    try:
        import boto3  # noqa: F401
    except ImportError as exc:
        raise ImproperlyConfigured(
            "OCTOREVIEW_STORAGE_BACKEND=s3 needs boto3; install the extra with "
            "`uv sync --extra s3` (the published image already has it)."
        ) from exc
MEDIA_PUBLIC_BASE_URL = os.getenv("OCTOREVIEW_MEDIA_PUBLIC_BASE_URL", "").rstrip("/")
UPLOAD_URL_TTL_SECONDS = env_int("OCTOREVIEW_UPLOAD_URL_TTL_SECONDS", 900, minimum=60)
MEDIA_URL_TTL_SECONDS = env_int("OCTOREVIEW_MEDIA_URL_TTL_SECONDS", 900, minimum=60)
COMPLETE_LEASE_SECONDS = env_int("OCTOREVIEW_COMPLETE_LEASE_SECONDS", 900, minimum=30)
MAX_CONCURRENT_COMPLETIONS = env_int("OCTOREVIEW_MAX_CONCURRENT_COMPLETIONS", 4, minimum=1)
LIST_MEDIA_LIMIT = env_int("OCTOREVIEW_LIST_MEDIA_LIMIT", 10, minimum=1)
# Signed read URLs cached per worker for one signing epoch (docs/spec.md section 8); 0 is off.
MEDIA_URL_CACHE_SIZE = env_int("OCTOREVIEW_MEDIA_URL_CACHE_SIZE", 10_000, minimum=0)
MEDIA_CORP = os.getenv("OCTOREVIEW_MEDIA_CORP", "cross-origin")
if MEDIA_CORP not in {"cross-origin", "same-site", "same-origin"}:
    raise ImproperlyConfigured(
        "OCTOREVIEW_MEDIA_CORP must be cross-origin, same-site or same-origin; pick one."
    )
# Uploads may still complete for this long after upload_expires_at (docs/spec.md section 6.3).
UPLOAD_GRACE_SECONDS = 300
# cleanup_media waits this long past the grace before deleting an abandoned upload.
CLEANUP_DELAY_SECONDS = 600

# Local media endpoints answer CORS for browser uploads and downloads; nothing else does.
CORS_ALLOWED_ORIGINS = env_list("OCTOREVIEW_MEDIA_CORS_ORIGINS")
CORS_URLS_REGEX = r"^/api/v1/media-(uploads|files)/"
CORS_ALLOW_METHODS = ("GET", "PUT", "OPTIONS")
CORS_ALLOW_HEADERS = ("content-type", "content-length", "range")
CORS_EXPOSE_HEADERS = ("content-length", "content-range", "x-request-id")

# Covering indexes are PostgreSQL-only; SQLite simply skips them (docs/spec.md section 3.10).
SILENCED_SYSTEM_CHECKS = ["models.W040"]

# Reserved for webhooks; receivers reject signatures older than this (docs/spec.md section 10).
WEBHOOK_SIGNATURE_TOLERANCE_SECONDS = 300

ERROR_DOC_BASE_URL = os.getenv(
    "OCTOREVIEW_ERROR_DOC_BASE_URL",
    "https://github.com/octoverse-id/octoreview/blob/main/docs/errors.md",
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "octoreview.core.logging.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": os.getenv("OCTOREVIEW_LOG_LEVEL", "INFO")},
    "loggers": {
        # Django's own request logger prints the raw path; our middleware logs the route
        # template instead (docs/spec.md section 16).
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.server": {"handlers": [], "level": "ERROR", "propagate": False},
    },
}
