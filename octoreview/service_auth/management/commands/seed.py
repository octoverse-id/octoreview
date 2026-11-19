"""`manage.py seed` (`make seed`): a demo tenant, application and service key (spec section 14).

Development only: it refuses unless DEBUG is on, and refuses a real AWS bucket. The key carries
every scope. Without `--dev-env` it prints the key once with a working `curl`. With
`--dev-env PATH` (the compose stack) it writes `.octoreview-dev.env` instead and prints only the
path. A rerun keeps the key while the file holds a live demo key, refreshing the base URL if it
changed (a new OCTOREVIEW_PORT); when the file is gone or its key is dead but the database
persists, the old demo key is revoked and a new one written.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from octoreview.core.auth import SCOPES
from octoreview.service_auth.models import ServiceClient
from octoreview.service_auth.services import (
    create_service_client_token,
    hash_service_token,
    revoke_service_client,
)

DEMO_TENANT = "demo"
DEMO_APPLICATION = "storefront"
DEMO_CLIENT_NAME = "octoreview-demo"


def demo_clients():
    return ServiceClient.objects.filter(metadata__demo_seed=True, is_active=True)


class Command(BaseCommand):
    help = "Create the demo tenant, application and all-scope service key (DEBUG only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dev-env",
            help="Write OCTOREVIEW_BASE_URL, OCTOREVIEW_API_KEY, OCTOREVIEW_TENANT_ID and "
            "OCTOREVIEW_APPLICATION_ID to this file instead of printing the key.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed creates an all-scope demo key, so it runs only in development; set "
                "DJANGO_DEBUG=true."
            )
        if settings.STORAGE_BACKEND == "s3" and not settings.STORAGE_S3_ENDPOINT_URL:
            raise CommandError(
                "seed refuses a real AWS bucket; use the local backend or the compose S3 emulator."
            )
        base_url = settings.PUBLIC_BASE_URL or "http://localhost:8000"
        dev_env = Path(options["dev_env"]) if options["dev_env"] else None
        saved = self._read_dev_env(dev_env)
        if self._is_live_demo_key(saved.get("OCTOREVIEW_API_KEY")):
            if saved.get("OCTOREVIEW_BASE_URL") != base_url:
                self._write_dev_env(dev_env, base_url, saved["OCTOREVIEW_API_KEY"])
                self.stdout.write(f"seed: kept the demo key; base URL is now {base_url}")
            else:
                self.stdout.write(f"seed: already seeded; the demo key is in {dev_env}")
            return

        # A lost dev env file means a lost key: rotate instead of minting a second live one.
        for client in demo_clients():
            revoke_service_client(client.key_prefix)
        token, _client = create_service_client_token(
            name=DEMO_CLIENT_NAME,
            grants=[
                {
                    "tenant_id": DEMO_TENANT,
                    "application_id": DEMO_APPLICATION,
                    "namespace_wildcard": True,
                    "scopes": sorted(SCOPES),
                }
            ],
            metadata={"demo_seed": True},
        )
        if dev_env is not None:
            self._write_dev_env(dev_env, base_url, token)
            self.stdout.write(f"seed: wrote {dev_env}; copy it out with `make dev-env`")
            return
        self.stdout.write(
            f"Demo key for tenant {DEMO_TENANT!r}, application {DEMO_APPLICATION!r} (shown once):"
        )
        self.stdout.write(f"  {token}")
        self.stdout.write("Create a review:")
        self.stdout.write(self._curl(base_url, token))

    @staticmethod
    def _read_dev_env(path: Path | None) -> dict[str, str]:
        if path is None or not path.exists():
            return {}
        pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
        return {key: value for key, value in pairs}

    @staticmethod
    def _is_live_demo_key(token: str | None) -> bool:
        if not token:
            return False
        return demo_clients().filter(hashed_key=hash_service_token(token)).exists()

    @staticmethod
    def _write_dev_env(path: Path, base_url: str, token: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"OCTOREVIEW_BASE_URL={base_url}",
            f"OCTOREVIEW_API_KEY={token}",
            f"OCTOREVIEW_TENANT_ID={DEMO_TENANT}",
            f"OCTOREVIEW_APPLICATION_ID={DEMO_APPLICATION}",
        ]
        part = path.with_name(f"{path.name}.part")
        # Readable by its owner only: it holds a live key.
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(part, path)

    @staticmethod
    def _curl(base_url: str, token: str) -> str:
        body = (
            f'{{"application_id": "{DEMO_APPLICATION}", "entity_type": "product", '
            '"external_id": "sku-1", "author_id": "u1", "rating": 5, "title": "Great"}'
        )
        return " \\\n  ".join(
            [
                f"curl -sS -X POST {base_url}/api/v1/reviews",
                f"-H {shlex.quote(f'Authorization: Bearer {token}')}",
                f"-H 'X-Tenant-ID: {DEMO_TENANT}'",
                "-H 'Content-Type: application/json'",
                f"-d {shlex.quote(body)}",
            ]
        )
