from __future__ import annotations

import pytest
from django.db import connection
from rest_framework.test import APIClient

from octoreview.service_auth.services import create_service_client_token


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def make_key(db):
    """Create a service key with one or more grants; returns the raw key."""

    def factory(*grants: dict, name: str = "test-client", **client_kwargs) -> str:
        normalized = []
        for grant in grants or ({"scopes": []},):
            normalized.append(
                {
                    "tenant_id": grant.get("tenant_id", "tenant-a"),
                    "application_id": grant.get("application_id"),
                    "namespace_type": grant.get("namespace_type"),
                    "namespace_id": grant.get("namespace_id"),
                    "namespace_wildcard": grant.get("namespace_wildcard", False),
                    "scopes": grant.get("scopes", []),
                }
            )
        token, _client = create_service_client_token(name=name, grants=normalized, **client_kwargs)
        return token

    return factory


@pytest.fixture
def headers():
    def build(token: str | None = None, tenant: str | None = "tenant-a", **extra) -> dict:
        result = {}
        if token is not None:
            result["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        if tenant is not None:
            result["HTTP_X_TENANT_ID"] = tenant
        for key, value in extra.items():
            result[f"HTTP_{key.upper().replace('-', '_')}"] = value
        return result

    return build


postgres_only = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="needs PostgreSQL behavior"
)
