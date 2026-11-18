from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from octoreview.service_auth.services import revoke_service_client


class Command(BaseCommand):
    help = "Revoke a service key by its prefix (the 8 hex characters after `ocr_`)."

    def add_arguments(self, parser):
        parser.add_argument("key_prefix")

    def handle(self, *args, **options):
        client = revoke_service_client(options["key_prefix"])
        if client is None:
            raise CommandError(f"No service key with prefix {options['key_prefix']}.")
        self.stdout.write(f"Revoked service client {client.id} ({client.name}).")
