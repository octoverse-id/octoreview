from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

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


# --- S3-compatible storage (spec section 7; `make test-storage`, CI `storage-emulators`) -------

S3_ENDPOINT = os.getenv("OCTOREVIEW_TEST_S3_ENDPOINT_URL", "")
# CI sets this so a misconfigured job fails instead of skipping every S3 test into a green run.
S3_REQUIRED = os.getenv("OCTOREVIEW_TEST_S3_REQUIRED") == "1"


@pytest.fixture(scope="session")
def s3_bucket():
    """A fresh bucket on the emulator named by OCTOREVIEW_TEST_S3_ENDPOINT_URL."""

    if not S3_ENDPOINT:
        message = "set OCTOREVIEW_TEST_S3_ENDPOINT_URL (make test-storage) to run S3 tests"
        if S3_REQUIRED:
            pytest.fail(message)
        pytest.skip(message)
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    bucket = f"octoreview-test-{uuid.uuid4().hex[:12]}"
    client.create_bucket(Bucket=bucket)  # scripts/test-storage.sh waits until the store answers
    return SimpleNamespace(name=bucket, client=client)


@pytest.fixture
def s3_backend(settings, s3_bucket):
    """Switch the storage backend to `s3` on the emulator for one test."""

    from octoreview.media.storage import get_backend

    settings.STORAGE_BACKEND = "s3"
    settings.STORAGE_S3_BUCKET = s3_bucket.name
    settings.STORAGE_S3_REGION = "us-east-1"
    settings.STORAGE_S3_ENDPOINT_URL = S3_ENDPOINT
    settings.STORAGE_S3_PUBLIC_ENDPOINT_URL = S3_ENDPOINT
    settings.STORAGE_S3_ADDRESSING_STYLE = "path"
    get_backend.cache_clear()
    yield get_backend()
    get_backend.cache_clear()


def s3_keys(bucket, prefix: str = "") -> set[str]:
    listed = bucket.client.list_objects_v2(Bucket=bucket.name, Prefix=prefix)
    return {item["Key"] for item in listed.get("Contents", [])}
