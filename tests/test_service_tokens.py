import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from octoreview.service_auth.models import ServiceClient, ServiceClientGrant
from octoreview.service_auth.services import (
    authenticate_service_token,
    hash_service_token,
    parse_service_token,
)

pytestmark = pytest.mark.django_db


def run(*args):
    from io import StringIO

    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


def test_create_prints_the_key_once_and_stores_only_its_hash():
    output = run(
        "create_service_token", "--name", "svc", "--tenant", "t1", "--scope", "reviews:read"
    )
    token = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("Token"))
    assert token.startswith("ocr_")
    client = ServiceClient.objects.get(name="svc")
    assert client.hashed_key == hash_service_token(token)
    assert token not in json.dumps(
        list(ServiceClient.objects.values("key_prefix", "hashed_key", "metadata"))
    )
    assert authenticate_service_token(token) == client


def test_create_json_output():
    output = run(
        "create_service_token",
        "--name",
        "svc-json",
        "--tenant",
        "t1",
        "--application",
        "app-1",
        "--namespace-wildcard",
        "--scope",
        "reviews:read",
        "--scope",
        "reviews:read",
        "--json",
    )
    data = json.loads(output)
    assert set(data) == {"service_client_id", "key_prefix", "token"}
    grant = ServiceClientGrant.objects.get(service_client_id=data["service_client_id"])
    assert grant.namespace_wildcard is True
    assert grant.scopes == ["reviews:read"]


def test_unknown_scope_exits_2_and_lists_valid_scopes(capsys):
    with pytest.raises(SystemExit) as exc:
        call_command(
            "create_service_token", "--name", "x", "--tenant", "t1", "--scope", "reviews:everything"
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "reviews:everything" in err
    assert "privacy:erase" in err
    assert not ServiceClient.objects.exists()


def test_exact_namespace_grant_requires_an_application():
    with pytest.raises(CommandError):
        run(
            "create_service_token",
            "--name",
            "x",
            "--tenant",
            "t1",
            "--namespace-type",
            "merchant",
            "--namespace-id",
            "m-1",
            "--scope",
            "reviews:read",
        )


def test_revoke_makes_the_key_unusable():
    output = run(
        "create_service_token", "--name", "svc", "--tenant", "t1", "--scope", "reviews:read"
    )
    token = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("Token"))
    prefix = token.split("_")[1]
    run("revoke_service_token", prefix)
    assert authenticate_service_token(token) is None
    with pytest.raises(CommandError):
        run("revoke_service_token", "00000000")


@pytest.mark.parametrize(
    "token",
    ["", "octo_abcd1234_secret", "ocr_abcd1234", "ocr__secret", "ocr_x_" + "s" * 200],
)
def test_parse_rejects_malformed_keys(token):
    assert parse_service_token(token) is None
