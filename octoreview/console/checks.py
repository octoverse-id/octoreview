"""System checks for the operator console (copied from octonomy's W001)."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def admin_enabled_in_production(app_configs=None, **kwargs):
    """A deliberate, supported choice, so a Warning that `manage.py check --deploy` surfaces.

    Deploy-tagged and gated on `not DEBUG`: it never fires in development or compose, and the
    image's entrypoint (`manage.py check`, no --deploy) stays quiet.
    """

    if settings.DEBUG or not settings.ADMIN_ENABLED:
        return []
    return [
        Warning(
            "The octoreview admin is enabled with DEBUG=false. It is a trusted operator "
            "console; the REST API remains the product surface.",
            hint="Serve it over HTTPS to trusted operators only (a VPN or an allowlist), or "
            "unset OCTOREVIEW_ADMIN_ENABLED. See docs/operations.md 'Admin console'.",
            id="octoreview.W001",
        )
    ]
