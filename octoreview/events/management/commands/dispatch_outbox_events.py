from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from octoreview.events.dispatch import dispatch_outbox_events


class Command(BaseCommand):
    help = "Deliver pending outbox events; --retry-failed also replays failed and dead letters."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--retry-failed", action="store_true")

    def handle(self, *args, **options):
        summary = dispatch_outbox_events(
            limit=options["limit"], retry_failed=options["retry_failed"]
        )
        self.stdout.write(json.dumps(summary, sort_keys=True))
