"""The `s3` backend (docs/spec.md sections 7.1-7.4 and 7.8).

The first half runs everywhere: presigning, URL lifetimes and error mapping need no network
(presigning is local computation; error responses are stubbed). The second half, marked `s3`,
drives the media API against the S3-compatible emulator (`make test-storage`).
"""

from __future__ import annotations

import http.client
import io
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from django.core.management import CommandError, call_command
from django.utils import timezone

from octoreview.lifecycle.media import cleanup_media
from octoreview.media import tokens
from octoreview.media.management.commands.check_storage import http as send
from octoreview.media.models import MediaStatus, ReviewMedia
from octoreview.media.storage import (
    CredentialsExpiring,
    PreconditionFailed,
    StorageError,
    get_backend,
)
from octoreview.media.storage.s3 import ExpirationRule, credential_expiry
from tests.conftest import s3_keys
from tests.test_media import APP, JPEG, PNG, SCOPES, complete, create_review, intent, set_settings

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def admin(make_key):
    return make_key({"application_id": "app-1", "namespace_wildcard": True, "scopes": SCOPES})


@pytest.fixture
def offline_s3(settings, monkeypatch):
    """The s3 backend with static fake keys; only presigning and stubbed calls may run."""

    for name, value in {
        "AWS_ACCESS_KEY_ID": "AKIAOCTOREVIEWTEST",
        "AWS_SECRET_ACCESS_KEY": "octoreview-test-secret",
        "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent",
        "AWS_CONFIG_FILE": "/nonexistent",
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    settings.STORAGE_BACKEND = "s3"
    settings.STORAGE_S3_BUCKET = "reviews"
    settings.STORAGE_S3_REGION = "ap-southeast-3"
    settings.STORAGE_S3_ENDPOINT_URL = "http://storage.internal:9000"
    settings.STORAGE_S3_PUBLIC_ENDPOINT_URL = "http://localhost:9000"
    settings.STORAGE_S3_ADDRESSING_STYLE = "path"
    get_backend.cache_clear()
    yield get_backend()
    get_backend.cache_clear()


def expiring_credentials(expires_in: timedelta) -> RefreshableCredentials:
    """Temporary credentials (IRSA or an instance role) whose refresh yields the same expiry."""

    metadata = {
        "access_key": "ASIAOCTOREVIEWTEST",
        "secret_key": "octoreview-test-secret",
        "token": "session-token",
        "expiry_time": (datetime.now(UTC) + expires_in).isoformat(),
    }
    return RefreshableCredentials.create_from_metadata(
        metadata=metadata, refresh_using=lambda: metadata, method="test"
    )


def query(url: str) -> dict:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


# --- presigning and URL lifetimes (no network) --------------------------------------------------


def test_the_upload_url_signs_type_and_length_for_the_public_host(offline_s3):
    expires_at = timezone.now() + timedelta(seconds=600)
    target = offline_s3.create_upload(
        key="_staging/t/a/m",
        content_type="image/png",
        size_bytes=64,
        expires_at=expires_at,
        media_id=uuid.uuid4(),
    )
    parts = urlsplit(target.url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "http",
        "localhost:9000",
        "/reviews/_staging/t/a/m",
    )
    params = query(target.url)
    assert params["X-Amz-SignedHeaders"] == "content-length;content-type;host"
    assert 590 <= int(params["X-Amz-Expires"]) <= 600
    assert target.headers == {"Content-Type": "image/png"}
    assert target.expires_at == expires_at


def test_the_download_url_is_presigned_for_the_public_host(offline_s3):
    url = offline_s3.download_url(
        "media/t/a/e/r/m.png",
        media_id=uuid.uuid4(),
        audience="public",
        expires_at=timezone.now() + timedelta(seconds=900),
    )
    assert urlsplit(url).netloc == "localhost:9000"
    assert query(url)["X-Amz-SignedHeaders"] == "host"


def test_static_keys_cap_urls_at_seven_days(offline_s3):
    now = timezone.now()
    short = now + timedelta(minutes=15)
    assert offline_s3.url_expiry(short) == short
    capped = offline_s3.url_expiry(now + timedelta(days=30))
    assert timedelta(days=7) - timedelta(seconds=5) < capped - now <= timedelta(days=7, seconds=1)


def test_temporary_credentials_end_urls_a_minute_early(offline_s3, monkeypatch):
    credentials = expiring_credentials(timedelta(hours=1))
    monkeypatch.setattr(offline_s3.session, "get_credentials", lambda: credentials)
    expiry = credential_expiry(credentials)
    assert offline_s3.url_expiry(timezone.now() + timedelta(hours=2)) == expiry - timedelta(
        seconds=60
    )


def test_a_nearly_spent_credential_refuses_to_sign(offline_s3, monkeypatch):
    credentials = expiring_credentials(timedelta(seconds=90))
    monkeypatch.setattr(offline_s3.session, "get_credentials", lambda: credentials)
    with pytest.raises(CredentialsExpiring):
        offline_s3.url_expiry(timezone.now() + timedelta(minutes=15))


def test_missing_credentials_are_a_storage_error():
    with pytest.raises(StorageError) as error:
        credential_expiry(None)
    assert error.value.operation == "credentials"


@pytest.mark.django_db
def test_an_intent_on_a_spent_credential_answers_503(
    offline_s3, monkeypatch, api_client, headers, admin
):
    review = create_review(api_client, headers, admin)
    credentials = expiring_credentials(timedelta(seconds=90))
    monkeypatch.setattr(offline_s3.session, "get_credentials", lambda: credentials)
    response = intent(api_client, headers, admin, review["id"])
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_credentials_expiring"
    assert response["Retry-After"]
    assert not ReviewMedia.objects.exists()


@pytest.mark.django_db
def test_a_replayed_intent_url_ends_before_its_credential(
    offline_s3, monkeypatch, api_client, headers, admin
):
    review = create_review(api_client, headers, admin)
    first = intent(api_client, headers, admin, review["id"], client_upload_id="c-1")
    assert first.status_code == 201
    assert urlsplit(first.json()["data"]["upload"]["url"]).netloc == "localhost:9000"
    credentials = expiring_credentials(timedelta(minutes=10))
    monkeypatch.setattr(offline_s3.session, "get_credentials", lambda: credentials)
    replay = intent(api_client, headers, admin, review["id"], client_upload_id="c-1")
    assert replay.status_code == 200
    expires_at = datetime.fromisoformat(replay.json()["data"]["upload"]["expires_at"])
    assert expires_at == credential_expiry(credentials) - timedelta(seconds=60)
    row = ReviewMedia.objects.get()
    assert expires_at < row.upload_expires_at


# --- error mapping (stubbed responses) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [(404, "404", None), (403, "403", "ListBucket"), (500, "InternalError", "InternalError")],
)
def test_stat_maps_errors(offline_s3, status, code, expected):
    with Stubber(offline_s3.client) as stub:
        stub.add_client_error("head_object", service_error_code=code, http_status_code=status)
        if expected is None:
            assert offline_s3.stat("k") is None
            return
        with pytest.raises(StorageError) as error:
            offline_s3.stat("k")
    assert error.value.operation == "stat"
    assert expected in str(error.value)


@pytest.mark.parametrize(("status", "code"), [(412, "PreconditionFailed"), (404, "NoSuchKey")])
def test_a_changed_or_vanished_object_is_a_precondition_failure(offline_s3, status, code):
    with Stubber(offline_s3.client) as stub:
        stub.add_client_error("get_object", service_error_code=code, http_status_code=status)
        with pytest.raises(PreconditionFailed):
            offline_s3.read_head("k", 8, if_version='"v"')
    with Stubber(offline_s3.copy_client) as stub:
        stub.add_client_error("copy_object", service_error_code=code, http_status_code=status)
        with pytest.raises(PreconditionFailed):
            offline_s3.finalize("s", "f", content_type="image/png", if_version='"v"')


def test_copy_sends_the_precondition_and_replaces_metadata(offline_s3):
    with Stubber(offline_s3.copy_client) as stub:
        stub.add_response(
            "copy_object",
            {},
            {
                "Bucket": "reviews",
                "Key": "media/f.png",
                "CopySource": {"Bucket": "reviews", "Key": "_staging/s"},
                "CopySourceIfMatch": '"v1"',
                "MetadataDirective": "REPLACE",
                "ContentType": "image/png",
            },
        )
        offline_s3.finalize(
            "_staging/s", "media/f.png", content_type="image/png", if_version='"v1"'
        )
        stub.assert_no_pending_responses()


def test_other_failures_are_storage_errors(offline_s3, monkeypatch):
    with Stubber(offline_s3.copy_client) as stub:
        stub.add_client_error("copy_object", service_error_code="SlowDown", http_status_code=503)
        with pytest.raises(StorageError) as error:
            offline_s3.finalize("s", "f", content_type="image/png", if_version='"v"')
    assert error.value.operation == "finalize"
    with Stubber(offline_s3.client) as stub:
        stub.add_client_error(
            "delete_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(StorageError) as error:
            offline_s3.delete("k")
    assert error.value.operation == "delete"

    def unreachable(**_kwargs):
        raise EndpointConnectionError(endpoint_url="http://storage.internal:9000")

    monkeypatch.setattr(offline_s3.client, "head_object", unreachable)
    with pytest.raises(StorageError) as error:
        offline_s3.stat("k")
    assert error.value.operation == "stat"


def test_lifecycle_rules_are_read_in_every_filter_form(offline_s3):
    rules = [
        {
            "ID": "staging",
            "Filter": {"Prefix": "_staging/"},
            "Status": "Enabled",
            "Expiration": {"Days": 1},
        },
        {
            "ID": "tagged",
            "Filter": {"And": {"Prefix": "tmp/", "Tags": []}},
            "Status": "Enabled",
            "Expiration": {"Days": 3},
        },
        {"ID": "legacy", "Prefix": "old/", "Status": "Enabled", "Expiration": {"Days": 9}},
        {"ID": "off", "Filter": {"Prefix": ""}, "Status": "Disabled", "Expiration": {"Days": 1}},
        {
            "ID": "transition",
            "Filter": {"Prefix": ""},
            "Status": "Enabled",
            "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}],
        },
    ]
    with Stubber(offline_s3.client) as stub:
        stub.add_response("get_bucket_lifecycle_configuration", {"Rules": rules})
        found = offline_s3.expiration_rules()
    assert found == [
        ExpirationRule("staging", "_staging/", 1),
        ExpirationRule("tagged", "tmp/", 3),
        ExpirationRule("legacy", "old/", 9),
    ]
    with Stubber(offline_s3.client) as stub:
        stub.add_client_error(
            "get_bucket_lifecycle_configuration",
            service_error_code="NoSuchLifecycleConfiguration",
            http_status_code=404,
        )
        assert offline_s3.expiration_rules() == []


def test_a_rule_covers_a_prefix_when_either_contains_the_other():
    assert ExpirationRule("all", "", 30).covers("media/")
    assert ExpirationRule("tenant", "media/tenant-a/", 30).covers("media/")
    assert ExpirationRule("staging", "_staging/", 1).covers("_staging/")
    assert not ExpirationRule("staging", "_staging/", 1).covers("media/")


# --- the local token paths are closed on a cloud backend ------------------------------------------


@pytest.mark.django_db
def test_token_paths_answer_404_on_a_cloud_backend(offline_s3, api_client):
    put = api_client.generic("PUT", "/api/v1/media-uploads/c.e30.x", b"x", content_type="image/png")
    assert put.status_code == 404
    assert put.json()["error"]["code"] == "not_found"
    assert api_client.get("/api/v1/media-files/c.e30.x").status_code == 404


def test_an_empty_token_secret_verifies_nothing(settings):
    settings.MEDIA_TOKEN_SECRET = ""
    payload = tokens._b64(b'{"e":4102444800,"k":"k","m":"m","s":1}')
    forged = f"c.{payload}.{tokens._sign('', tokens.UPLOAD, payload)}"
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(forged, tokens.UPLOAD, now=timezone.now())
    with pytest.raises(RuntimeError):
        tokens.upload_token(media_id="m", key="k", size_bytes=1, expires_at=timezone.now())


# --- boot errors ----------------------------------------------------------------------------------


def boot(extra_env: dict, pythonpath: str | None = None) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OCTOREVIEW_STORAGE_", "DJANGO_SETTINGS_MODULE"))
    }
    env.update(extra_env)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, "-c", "import config.settings_pytest"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


S3_ENV = {
    "OCTOREVIEW_STORAGE_BACKEND": "s3",
    "OCTOREVIEW_STORAGE_S3_BUCKET": "reviews",
    "OCTOREVIEW_STORAGE_S3_REGION": "ap-southeast-3",
}


def test_s3_boots_with_a_bucket_and_region():
    result = boot(S3_ENV)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"OCTOREVIEW_STORAGE_S3_BUCKET": ""}, "OCTOREVIEW_STORAGE_S3_BUCKET is required"),
        ({"OCTOREVIEW_STORAGE_S3_REGION": ""}, "OCTOREVIEW_STORAGE_S3_REGION is required"),
        ({"OCTOREVIEW_STORAGE_S3_ADDRESSING_STYLE": "dns"}, "must be auto, path or virtual"),
        ({"OCTOREVIEW_STORAGE_BACKEND": "azure"}, "must be one of local, s3"),
    ],
)
def test_s3_boot_errors_name_the_variable(override, message):
    result = boot({**S3_ENV, **override})
    assert result.returncode != 0
    assert message in result.stderr


def test_s3_without_boto3_names_the_extra(tmp_path):
    (tmp_path / "boto3").mkdir()
    (tmp_path / "boto3" / "__init__.py").write_text("raise ImportError('simulated')\n")
    result = boot(S3_ENV, pythonpath=str(tmp_path))
    assert result.returncode != 0
    assert "uv sync --extra s3" in result.stderr


# --- the media API on the emulator --------------------------------------------------------------


def upload_via_url(data: dict, body: bytes) -> int:
    upload = data["upload"]
    status, _ = send("PUT", upload["url"], body=body, headers=upload["headers"])
    return status


@pytest.mark.s3
@pytest.mark.django_db
def test_the_media_flow_on_s3(s3_backend, s3_bucket, api_client, headers, admin):
    review = create_review(api_client, headers, admin)
    created = intent(api_client, headers, admin, review["id"])
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["upload"]["url"].startswith(s3_bucket.client.meta.endpoint_url)
    assert upload_via_url(data, PNG) == 200
    done = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert done.status_code == 200, done.json()
    media = done.json()["data"]
    assert media["status"] == "ready"
    row = ReviewMedia.objects.get(pk=media["id"])
    assert s3_keys(s3_bucket, row.storage_key) == {row.storage_key}
    assert s3_keys(s3_bucket, row.staging_key) == set()  # deleted after phase 3

    status, served = send("GET", media["url"])
    assert (status, served) == (200, PNG)

    deleted = api_client.delete(
        f"/api/v1/reviews/{review['id']}/media/{media['id']}?{APP}", **headers(admin)
    )
    assert deleted.status_code in (200, 204)
    cleanup_media()
    row.refresh_from_db()
    assert row.status == MediaStatus.DELETED
    assert s3_keys(s3_bucket, row.storage_key) == set()


@pytest.mark.s3
@pytest.mark.django_db
def test_a_type_mismatch_is_rejected_and_both_objects_go(
    s3_backend, s3_bucket, api_client, headers, admin
):
    review = create_review(api_client, headers, admin)
    data = intent(api_client, headers, admin, review["id"], size_bytes=len(JPEG)).json()["data"]
    assert upload_via_url(data, JPEG) == 200  # signed as image/png; the bytes are a JPEG
    done = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert done.status_code == 422
    assert done.json()["error"]["details"]["reason"] == "type_mismatch"
    row = ReviewMedia.objects.get(pk=data["media"]["id"])
    assert row.status == MediaStatus.INVALID
    assert s3_keys(s3_bucket, f"_staging/tenant-a/app-1/{row.id}") == set()


@pytest.mark.s3
@pytest.mark.django_db
def test_a_re_put_during_complete_is_never_served(
    s3_backend, s3_bucket, api_client, headers, admin, monkeypatch
):
    review = create_review(api_client, headers, admin)
    data = intent(api_client, headers, admin, review["id"]).json()["data"]
    assert upload_via_url(data, PNG) == 200
    other = b"\x89PNG\r\n\x1a\n" + bytes(range(200, 256))
    original = s3_backend.finalize

    def re_put_then_finalize(*args, **kwargs):
        # The client PUTs again between validation and the copy (same signed size).
        assert upload_via_url(data, other) == 200
        monkeypatch.setattr(s3_backend, "finalize", original)
        return original(*args, **kwargs)

    monkeypatch.setattr(s3_backend, "finalize", re_put_then_finalize)
    raced = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert raced.status_code == 409
    assert raced.json()["error"]["code"] == "upload_changed"
    row = ReviewMedia.objects.get(pk=data["media"]["id"])
    assert row.status == MediaStatus.PENDING
    assert s3_keys(s3_bucket, f"media/tenant-a/app-1/{row.review.entity_id}") == set()

    retried = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert retried.status_code == 200, retried.json()
    status, served = send("GET", retried.json()["data"]["url"])
    assert (status, served) == (200, other)


@pytest.mark.s3
@pytest.mark.s3_large
@pytest.mark.django_db
def test_a_500_mib_video_completes(s3_backend, s3_bucket, api_client, headers, admin):
    if os.getenv("OCTOREVIEW_TEST_S3_LARGE") != "1":
        pytest.skip("set OCTOREVIEW_TEST_S3_LARGE=1 to upload 500 MiB")
    size = 524_288_000  # the max_video_bytes ceiling
    set_settings(max_video_bytes=size)
    review = create_review(api_client, headers, admin)
    head = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00mp41"
    created = intent(
        api_client,
        headers,
        admin,
        review["id"],
        kind="video",
        content_type="video/mp4",
        size_bytes=size,
    )
    assert created.status_code == 201, created.json()
    data = created.json()["data"]
    parts = urlsplit(data["upload"]["url"])
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=300)
    connection.putrequest("PUT", f"{parts.path}?{parts.query}", skip_accept_encoding=True)
    for name, value in {**data["upload"]["headers"], "Content-Length": str(size)}.items():
        connection.putheader(name, value)
    connection.endheaders()
    connection.send(head)
    chunk = bytes(8 * 1024 * 1024)
    remaining = size - len(head)
    while remaining:
        connection.send(chunk[: min(len(chunk), remaining)])
        remaining -= min(len(chunk), remaining)
    response = connection.getresponse()
    response.read()
    connection.close()
    assert response.status == 200
    done = complete(api_client, headers, admin, review["id"], data["media"]["id"])
    assert done.status_code == 200, done.json()
    row = ReviewMedia.objects.get(pk=data["media"]["id"])
    stored = s3_bucket.client.head_object(Bucket=s3_bucket.name, Key=row.storage_key)
    assert stored["ContentLength"] == size
    assert stored["ContentType"] == "video/mp4"


# --- check_storage on the emulator ---------------------------------------------------------------


def run_check_storage():
    out, err = io.StringIO(), io.StringIO()
    call_command("check_storage", stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


@pytest.fixture
def lifecycle(s3_bucket):
    def apply(*rules):
        s3_bucket.client.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket.name, LifecycleConfiguration={"Rules": list(rules)}
        )

    yield apply
    s3_bucket.client.delete_bucket_lifecycle(Bucket=s3_bucket.name)


@pytest.fixture
def bucket_cors(s3_bucket):
    def apply(*origins):
        rule = {
            "AllowedOrigins": list(origins),
            "AllowedMethods": ["GET", "PUT"],
            "AllowedHeaders": ["content-type", "range"],
            "ExposeHeaders": ["ETag", "Content-Length", "Content-Range"],
            "MaxAgeSeconds": 600,
        }
        s3_bucket.client.put_bucket_cors(
            Bucket=s3_bucket.name, CORSConfiguration={"CORSRules": [rule]}
        )

    yield apply
    s3_bucket.client.delete_bucket_cors(Bucket=s3_bucket.name)


def rule(rule_id: str, prefix: str, days: int) -> dict:
    return {
        "ID": rule_id,
        "Filter": {"Prefix": prefix},
        "Status": "Enabled",
        "Expiration": {"Days": days},
    }


@pytest.mark.s3
def test_check_storage_round_trips_and_warns_without_a_staging_rule(s3_backend, s3_bucket):
    out, err = run_check_storage()
    for step in ("stat_missing", "size_cap", "put", "conditional_copy", "final_metadata", "get"):
        assert f"  {step}: ok" in out
    assert "check_storage: ok" in out
    assert "no lifecycle rule expires _staging/" in err
    assert "does not allow https://shop.example" in err  # pytest settings list this origin
    assert "accepted a body larger" not in err
    assert s3_keys(s3_bucket, "_staging/_check/") == set()
    assert s3_keys(s3_bucket, "media/_check/") == set()


@pytest.mark.s3
def test_check_storage_accepts_a_bucket_set_up_as_documented(s3_backend, lifecycle, bucket_cors):
    lifecycle(rule("octoreview-staging", "_staging/", 1))
    bucket_cors("https://shop.example")
    out, err = run_check_storage()
    assert "  cors: ok" in out
    assert "lifecycle: ok (_staging/ expires after 1 day(s))" in out
    assert "warning" not in err


@pytest.mark.s3
def test_check_storage_fails_on_a_rule_that_expires_media(s3_backend, lifecycle):
    lifecycle(rule("everything", "", 30))
    with pytest.raises(CommandError, match="'everything' expire objects under media/"):
        run_check_storage()


@pytest.mark.s3
def test_check_storage_fails_when_the_store_ignores_the_copy_precondition(
    s3_backend, s3_bucket, monkeypatch
):
    def unconditional(staging_key, final_key, *, content_type, if_version):
        s3_backend.copy_client.copy_object(
            Bucket=s3_bucket.name,
            Key=final_key,
            CopySource={"Bucket": s3_bucket.name, "Key": staging_key},
            MetadataDirective="REPLACE",
            ContentType=content_type,
        )

    monkeypatch.setattr(s3_backend, "finalize", unconditional)
    with pytest.raises(CommandError, match="ignores CopySourceIfMatch"):
        run_check_storage()
    assert s3_keys(s3_bucket, "media/_check/") == set()
