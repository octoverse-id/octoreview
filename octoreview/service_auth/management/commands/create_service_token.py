from __future__ import annotations

import json
import sys

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from octoreview.core.auth import SCOPES
from octoreview.service_auth.services import create_service_client_token, unknown_scopes


class Command(BaseCommand):
    help = "Create a service key with one grant and print the key once."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--application")
        parser.add_argument("--namespace-type")
        parser.add_argument("--namespace-id")
        parser.add_argument("--namespace-wildcard", action="store_true")
        parser.add_argument("--scope", action="append", required=True, help="Repeat per scope.")
        parser.add_argument("--expires-at", help="ISO 8601 datetime.")
        parser.add_argument("--metadata", help="Optional JSON object.")
        parser.add_argument("--json", action="store_true", help="Print one JSON object.")

    def handle(self, *args, **options):
        unknown = unknown_scopes(options["scope"])
        if unknown:
            # Exit 2 with the valid list, so a script (or an agent) can correct itself.
            valid = ", ".join(sorted(SCOPES))
            sys.stderr.write(f"Unknown scope(s): {', '.join(unknown)}. Valid scopes: {valid}\n")
            sys.exit(2)

        namespace_type = options.get("namespace_type")
        namespace_id = options.get("namespace_id")
        namespace_wildcard = options["namespace_wildcard"]
        if namespace_wildcard and (namespace_type is not None or namespace_id is not None):
            raise CommandError(
                "--namespace-wildcard cannot be combined with --namespace-type or --namespace-id."
            )
        if (namespace_type is None) != (namespace_id is None):
            raise CommandError("--namespace-type and --namespace-id must be supplied together.")
        if namespace_type is not None and not options.get("application"):
            raise CommandError("An exact namespace grant requires --application.")

        expires_at = None
        if options.get("expires_at"):
            expires_at = parse_datetime(options["expires_at"])
            if expires_at is None:
                raise CommandError("--expires-at must be an ISO 8601 datetime.")

        metadata = {}
        if options.get("metadata"):
            try:
                metadata = json.loads(options["metadata"])
            except json.JSONDecodeError as exc:
                raise CommandError("--metadata must be a JSON object.") from exc
            if not isinstance(metadata, dict):
                raise CommandError("--metadata must be a JSON object.")

        try:
            token, client = create_service_client_token(
                name=options["name"],
                expires_at=expires_at,
                metadata=metadata,
                grants=[
                    {
                        "tenant_id": options["tenant"],
                        "application_id": options.get("application"),
                        "namespace_type": namespace_type,
                        "namespace_id": namespace_id,
                        "namespace_wildcard": namespace_wildcard,
                        "scopes": options["scope"],
                    }
                ],
            )
        except DjangoValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        if options["json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "service_client_id": str(client.id),
                        "key_prefix": client.key_prefix,
                        "token": token,
                    }
                )
            )
            return
        self.stdout.write(f"Service client: {client.id}")
        self.stdout.write(f"Key prefix: {client.key_prefix}")
        self.stdout.write(f"Token: {token}")
        self.stdout.write("Store this token now. It cannot be shown again.")
