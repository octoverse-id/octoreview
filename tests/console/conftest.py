"""Fixtures for the operator console (ported from octonomy's tests/admin/conftest.py)."""

from __future__ import annotations

import contextlib
import importlib

import pytest
from django.test import override_settings
from django.urls import clear_url_caches


def _reload_urlconf() -> None:
    clear_url_caches()
    importlib.reload(importlib.import_module("config.urls"))


@contextlib.contextmanager
def admin_enabled(enabled: bool):
    """`config/urls.py` mounts /admin/ at import time, so toggling the flag reloads it."""

    try:
        with override_settings(ADMIN_ENABLED=enabled):
            _reload_urlconf()
            yield
    finally:
        _reload_urlconf()


@pytest.fixture
def console():
    with admin_enabled(True):
        yield


@pytest.fixture
def superuser(django_user_model):
    return django_user_model.objects.create_superuser(
        username="root", email="root@example.com", password="pw-root-123456"
    )


@pytest.fixture
def staff_user(django_user_model):
    """Passes Django's default admin gate, but not the console's superuser-only one."""

    return django_user_model.objects.create_user(
        username="staffer", email="staff@example.com", password="pw-staff-123456", is_staff=True
    )


@pytest.fixture
def root_client(client, superuser):
    client.force_login(superuser)
    return client
