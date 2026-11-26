"""`manage.py seed`: the demo key for development and compose (docs/spec.md section 14)."""

from __future__ import annotations

import io
import stat

import pytest
from django.core.management import CommandError, call_command

from octoreview.service_auth.models import ServiceClient
from octoreview.service_auth.services import authenticate_service_token

pytestmark = pytest.mark.django_db


def seed(*args) -> str:
    out = io.StringIO()
    call_command("seed", *args, stdout=out)
    return out.getvalue()


def read_env(path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


@pytest.fixture
def debug(settings):
    settings.DEBUG = True
    return settings


def test_seed_refuses_without_debug(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="DJANGO_DEBUG=true"):
        seed()
    assert not ServiceClient.objects.exists()


def test_seed_refuses_a_real_aws_bucket(debug):
    debug.STORAGE_BACKEND = "s3"
    debug.STORAGE_S3_ENDPOINT_URL = ""
    with pytest.raises(CommandError, match="real AWS bucket"):
        seed()


def test_seed_prints_a_working_key_once(debug, api_client):
    out = seed()
    token = next(line.strip() for line in out.splitlines() if line.strip().startswith("ocr_"))
    assert authenticate_service_token(token) is not None
    assert "curl -sS -X POST http://testserver/api/v1/reviews" in out
    response = api_client.post(
        "/api/v1/reviews",
        {
            "application_id": "storefront",
            "entity_type": "product",
            "external_id": "sku-1",
            "author_id": "u1",
            "rating": 5,
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_TENANT_ID="demo",
    )
    assert response.status_code == 201, response.json()


def test_seed_writes_the_dev_env_file_and_is_idempotent(debug, tmp_path):
    path = tmp_path / "dev" / ".octoreview-dev.env"
    out = seed("--dev-env", str(path))
    assert "ocr_" not in out  # the key goes to the file, never to the (compose) log
    values = read_env(path)
    assert set(values) == {
        "OCTOREVIEW_BASE_URL",
        "OCTOREVIEW_API_KEY",
        "OCTOREVIEW_TENANT_ID",
        "OCTOREVIEW_APPLICATION_ID",
    }
    assert (values["OCTOREVIEW_TENANT_ID"], values["OCTOREVIEW_APPLICATION_ID"]) == (
        "demo",
        "storefront",
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert authenticate_service_token(values["OCTOREVIEW_API_KEY"]) is not None

    assert "already seeded" in seed("--dev-env", str(path))
    assert read_env(path) == values
    assert ServiceClient.objects.filter(is_active=True).count() == 1


@pytest.mark.parametrize(("flags", "hint"), [((), False), (("--copy-hint",), True)])
def test_the_copy_hint_is_only_for_the_compose_stack(debug, tmp_path, flags, hint):
    # compose-start.sh passes --copy-hint; `make run` writes the file in place and does not.
    path = tmp_path / ".octoreview-dev.env"
    out = seed("--dev-env", str(path), *flags)
    assert f"seed: wrote {path}" in out
    assert ("make dev-env" in out) is hint


def test_a_lost_dev_env_file_rotates_the_key(debug, tmp_path):
    path = tmp_path / ".octoreview-dev.env"
    seed("--dev-env", str(path))
    old = read_env(path)["OCTOREVIEW_API_KEY"]
    path.unlink()
    seed("--dev-env", str(path))
    new = read_env(path)["OCTOREVIEW_API_KEY"]
    assert new != old
    assert authenticate_service_token(old) is None
    assert authenticate_service_token(new) is not None
    assert ServiceClient.objects.filter(is_active=True).count() == 1


def test_a_new_port_refreshes_the_base_url_and_keeps_the_key(debug, tmp_path):
    path = tmp_path / ".octoreview-dev.env"
    debug.PUBLIC_BASE_URL = "http://localhost:8000"
    seed("--dev-env", str(path))
    before = read_env(path)
    debug.PUBLIC_BASE_URL = "http://localhost:18000"  # restarted with OCTOREVIEW_PORT=18000
    assert "base URL is now http://localhost:18000" in seed("--dev-env", str(path))
    after = read_env(path)
    assert after["OCTOREVIEW_BASE_URL"] == "http://localhost:18000"
    assert after["OCTOREVIEW_API_KEY"] == before["OCTOREVIEW_API_KEY"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_dead_key_in_the_file_is_rotated(debug, tmp_path):
    path = tmp_path / ".octoreview-dev.env"
    seed("--dev-env", str(path))
    old = read_env(path)["OCTOREVIEW_API_KEY"]
    ServiceClient.objects.update(is_active=False)  # revoked by hand, for example
    seed("--dev-env", str(path))
    new = read_env(path)["OCTOREVIEW_API_KEY"]
    assert new != old
    assert authenticate_service_token(new) is not None
