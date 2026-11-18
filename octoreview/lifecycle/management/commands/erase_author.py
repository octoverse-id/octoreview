from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand, CommandError

from octoreview.core.audit_context import AuditContext
from octoreview.lifecycle.erasure import erase_author, pending_reviews


class Command(BaseCommand):
    help = "Erase one author's personal data in one application (docs/spec.md section 9)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--application", required=True)
        parser.add_argument("--author", required=True)
        parser.add_argument("--yes", action="store_true", help="Required without a TTY.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        tenant, application, author = options["tenant"], options["application"], options["author"]
        pending = pending_reviews(tenant, application, author).count()
        if options["dry_run"]:
            self.stdout.write(json.dumps({"reviews_to_redact": pending}))
            return
        if not options["yes"]:
            if not sys.stdin.isatty():
                raise CommandError("Refusing to erase without --yes when no TTY is attached.")
            answer = input(f"Erase {pending} review(s) by this author? Type 'erase' to continue: ")
            if answer.strip() != "erase":
                raise CommandError("Aborted.")
        total = 0
        ctx = AuditContext(actor_id="erase_author")
        while True:
            result = erase_author(
                tenant_id=tenant, application_id=application, author_id=author, audit_context=ctx
            )
            total += result.reviews_redacted
            if result.remaining == 0 or result.reviews_redacted == 0:
                break
        self.stdout.write(json.dumps({"reviews_redacted": total, "remaining": result.remaining}))
        if result.remaining:
            self.stderr.write("Some reviews were not erased; re-run the same command.")
            raise SystemExit(1)
