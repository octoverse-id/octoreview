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
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "drf_spectacular",
    "octoreview.core",
    "octoreview.service_auth",
    "octoreview.audit",
    "octoreview.events",
]

MIDDLEWARE = [
    "octoreview.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

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
    "VERSION": "1.0.0-dev",
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
