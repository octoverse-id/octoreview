from __future__ import annotations

from django.core.management.base import BaseCommand

from octoreview.openapi import contract


class Command(BaseCommand):
    help = (
        "Check docs/openapi.yaml, errors.md, events.md and sdk-surface.yaml against the server "
        "(make openapi-check). --write regenerates docs/sdk-surface.md first."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--write", action="store_true", help="Regenerate docs/sdk-surface.md, then check."
        )

    def handle(self, *args, **options):
        if options["write"]:
            surface = contract.load_yaml(contract.SURFACE)
            contract.SURFACE_MD.write_text(contract.render_surface_md(surface))
            self.stdout.write(f"wrote {contract.SURFACE_MD.relative_to(contract.DOCS.parent)}")
        problems = contract.run_all()
        for problem in problems:
            self.stderr.write(f"openapi-check: {problem}")
        if problems:
            raise SystemExit(1)
        self.stdout.write("openapi-check OK")
