"""The storage protocol contract, run against every backend (docs/spec.md section 7.1).

`local` always runs. `s3` runs against the S3-compatible emulator that
`OCTOREVIEW_TEST_S3_ENDPOINT_URL` names (`make test-storage`, CI `storage-emulators`). Uploads and
downloads go through each backend's own transport: the token code path for `local`, real HTTP
through presigned URLs for `s3`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from octoreview.media import tokens
from octoreview.media.management.commands.check_storage import http
from octoreview.media.storage import FinalExists, PreconditionFailed
from octoreview.media.storage.local import LocalBackend

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(56))
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(100, 156))


def _local(settings):
    backend = LocalBackend()

    def upload(key, body, content_type="image/png"):
        backend.write_stream(key, [body], limit=len(body))

    def download(key):
        url = backend.download_url(
            key,
            media_id=uuid.uuid4(),
            audience="moderator",
            expires_at=timezone.now() + timedelta(seconds=60),
        )
        claims = tokens.verify(url.rsplit("/", 1)[-1], tokens.DOWNLOAD, now=timezone.now())
        with backend.open(claims.key) as handle:
            return handle.read()

    return SimpleNamespace(name="local", backend=backend, upload=upload, download=download)


def _s3(request):
    backend = request.getfixturevalue("s3_backend")

    def upload(key, body, content_type="image/png"):
        target = backend.create_upload(
            key=key,
            content_type=content_type,
            size_bytes=len(body),
            expires_at=timezone.now() + timedelta(seconds=300),
            media_id=uuid.uuid4(),
        )
        status, reply = http("PUT", target.url, body=body, headers=target.headers)
        assert status == 200, reply

    def download(key):
        url = backend.download_url(
            key,
            media_id=uuid.uuid4(),
            audience="moderator",
            expires_at=timezone.now() + timedelta(seconds=60),
        )
        status, body = http("GET", url)
        assert status == 200, body
        return body

    return SimpleNamespace(name="s3", backend=backend, upload=upload, download=download)


@pytest.fixture(params=["local", pytest.param("s3", marks=pytest.mark.s3)])
def store(request, settings):
    return _local(settings) if request.param == "local" else _s3(request)


@pytest.fixture
def keys():
    probe = uuid.uuid4().hex
    return SimpleNamespace(
        staging=f"_staging/tenant-a/app-1/{probe}", final=f"media/tenant-a/app-1/e/r/{probe}.png"
    )


def test_a_missing_key_stats_as_none(store, keys):
    assert store.backend.stat(keys.staging) is None


def test_an_upload_stats_with_its_size_and_a_version(store, keys):
    store.upload(keys.staging, PNG)
    info = store.backend.stat(keys.staging)
    assert info.size == len(PNG)
    assert info.version


def test_read_head_is_bound_to_the_validated_version(store, keys):
    store.upload(keys.staging, PNG)
    version = store.backend.stat(keys.staging).version
    assert store.backend.read_head(keys.staging, 8, if_version=version) == PNG[:8]
    store.upload(keys.staging, OTHER_PNG)
    with pytest.raises(PreconditionFailed):
        store.backend.read_head(keys.staging, 8, if_version=version)


def test_finalize_publishes_the_validated_bytes(store, keys):
    store.upload(keys.staging, PNG)
    version = store.backend.stat(keys.staging).version
    store.backend.finalize(keys.staging, keys.final, content_type="image/png", if_version=version)
    assert store.download(keys.final) == PNG


def test_a_re_put_after_validation_is_never_published(store, keys):
    store.upload(keys.staging, PNG)
    version = store.backend.stat(keys.staging).version
    store.upload(keys.staging, OTHER_PNG)
    with pytest.raises(PreconditionFailed):
        store.backend.finalize(
            keys.staging, keys.final, content_type="image/png", if_version=version
        )
    assert store.backend.stat(keys.final) is None


def test_a_published_object_is_never_overwritten(store, keys):
    store.upload(keys.staging, PNG)
    first = store.backend.stat(keys.staging).version
    store.backend.finalize(keys.staging, keys.final, content_type="image/png", if_version=first)
    store.upload(keys.staging, OTHER_PNG)
    second = store.backend.stat(keys.staging).version
    with pytest.raises(FinalExists):
        store.backend.finalize(
            keys.staging, keys.final, content_type="image/png", if_version=second
        )
    assert store.download(keys.final) == PNG


def test_finalize_without_a_staged_object_is_a_precondition_failure(store, keys):
    with pytest.raises(PreconditionFailed):
        store.backend.finalize(keys.staging, keys.final, content_type="image/png", if_version="v")


def test_delete_is_idempotent(store, keys):
    store.upload(keys.staging, PNG)
    store.backend.delete(keys.staging)
    store.backend.delete(keys.staging)
    assert store.backend.stat(keys.staging) is None


# --- s3 only: what the store itself guarantees -------------------------------------------------


@pytest.mark.s3
def test_the_presigned_put_refuses_another_size_or_type(s3_backend, keys):
    target = s3_backend.create_upload(
        key=keys.staging,
        content_type="image/png",
        size_bytes=len(PNG),
        expires_at=timezone.now() + timedelta(seconds=300),
        media_id=uuid.uuid4(),
    )
    assert "Content-Length" not in target.headers
    status, _ = http("PUT", target.url, body=PNG + b"x", headers=target.headers)
    assert status == 403
    status, _ = http("PUT", target.url, body=PNG, headers={"Content-Type": "text/html"})
    assert status == 403
    assert s3_backend.stat(keys.staging) is None


@pytest.mark.s3
def test_the_final_object_carries_the_server_chosen_type(s3_backend, s3_bucket, keys):
    s3_bucket.client.put_object(
        Bucket=s3_bucket.name, Key=keys.staging, Body=PNG, ContentType="text/html"
    )
    version = s3_backend.stat(keys.staging).version
    s3_backend.finalize(keys.staging, keys.final, content_type="image/png", if_version=version)
    assert s3_backend.stat(keys.final).content_type == "image/png"
