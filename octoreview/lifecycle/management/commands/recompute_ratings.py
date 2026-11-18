from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from octoreview.lifecycle.recompute import recompute_ratings


class Command(BaseCommand):
    help = "Recompute entity rating aggregates from published reviews; --check only reports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true", help="Report mismatches, write nothing."
        )

    def handle(self, *args, **options):
        mismatches = recompute_ratings(repair=not options["check"])
        for mismatch in mismatches:
            self.stdout.write(json.dumps(mismatch, sort_keys=True))
        if options["check"] and mismatches:
            self.stderr.write(f"{len(mismatches)} entity aggregate(s) differ; run without --check.")
            raise SystemExit(1)
        verb = "would repair" if options["check"] else "repaired"
        self.stdout.write(f"recompute_ratings: {len(mismatches)} entity aggregate(s) {verb}.")
