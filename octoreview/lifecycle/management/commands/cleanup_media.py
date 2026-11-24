from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from octoreview.lifecycle.media import cleanup_media


class Command(BaseCommand):
    help = (
        "Delete expired pending uploads, media of removed reviews, `deleting` retries and the "
        "objects of rejected uploads whose delete failed, then sweep orphaned staging files. Run "
        "it from a CronJob with concurrencyPolicy: Forbid."
    )

    def handle(self, *args, **options):
        result = cleanup_media()
        self.stdout.write(
            json.dumps(
                {"deleted": result.deleted, "swept": result.swept, "failed": len(result.failed)},
                sort_keys=True,
            )
        )
        if result.failed:
            # Per-object errors do not stop the run; the exit code tells the scheduler (spec 7.7).
            for key in result.failed:
                self.stderr.write(f"cleanup_media: failed {key}")
            raise SystemExit(1)
