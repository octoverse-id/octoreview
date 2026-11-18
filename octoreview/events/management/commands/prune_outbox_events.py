from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from octoreview.events.dispatch import prune_outbox_events


class Command(BaseCommand):
    help = (
        "Delete published outbox rows and dead letters past retention. An undelivered "
        "author.erased is never pruned."
    )

    def add_arguments(self, parser):
        parser.add_argument("--published-older-than", type=int, default=30, metavar="DAYS")
        parser.add_argument("--dead-letter-older-than", type=int, default=30, metavar="DAYS")

    def handle(self, *args, **options):
        published = options["published_older_than"]
        dead = options["dead_letter_older_than"]
        if published < 1 or dead < 1:
            raise CommandError("Retention must be at least 1 day.")
        result = prune_outbox_events(
            published_older_than_days=published, dead_letter_older_than_days=dead
        )
        self.stdout.write(json.dumps(result, sort_keys=True))
