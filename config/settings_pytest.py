"""Settings pytest runs against: `config.settings` with test-only secrets filled in.

DEBUG stays off, so tests exercise production behavior. Nothing product-shaped belongs here; a
divergence between what tests exercise and what production runs is how bugs ship.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "pytest-secret-key")
os.environ.setdefault("OCTOREVIEW_SERVICE_TOKEN_PEPPER", "pytest-service-token-pepper")
os.environ.setdefault("OCTOREVIEW_ERASURE_HMAC_KEY", "pytest-erasure-hmac-key")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")

from config.settings import *  # noqa: E402,F403
