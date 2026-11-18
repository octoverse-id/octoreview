"""Settings pytest runs against: `config.settings` with test-only secrets filled in.

DEBUG stays off, so tests exercise production behavior. Nothing product-shaped belongs here; a
divergence between what tests exercise and what production runs is how bugs ship.
"""

import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "pytest-secret-key")
os.environ.setdefault("OCTOREVIEW_SERVICE_TOKEN_PEPPER", "pytest-service-token-pepper")
os.environ.setdefault("OCTOREVIEW_ERASURE_HMAC_KEY", "pytest-erasure-hmac-key")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("OCTOREVIEW_STORAGE_LOCATION_ID", "pytest-local")
os.environ.setdefault("OCTOREVIEW_PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("OCTOREVIEW_MEDIA_TOKEN_SECRET", "pytest-media-token-secret")
os.environ.setdefault("OCTOREVIEW_MEDIA_CORS_ORIGINS", "https://shop.example")
# Each test run writes media under its own temporary directory.
os.environ.setdefault("OCTOREVIEW_MEDIA_ROOT", tempfile.mkdtemp(prefix="octoreview-media-"))

from config.settings import *  # noqa: E402,F403
