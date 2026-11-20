"""The octoreview admin site: an opt-in, superuser-only operator console (copied from octonomy).

Gated two independent ways:

1. It is mounted at ``/admin/`` only when ``settings.ADMIN_ENABLED`` is true (``config/urls.py``);
   otherwise the route is absent (a resolver 404).
2. Even when mounted, the whole site admits *active superusers* only (``has_permission``).
   Django's default gate only requires ``is_active and is_staff``; an ordinary staff account
   cannot enter or inspect any model. One gate on the site covers every model.

``unfold.apps.BasicAppConfig`` is installed instead of unfold's default app config, which would
swap ``admin.site`` for a bare ``UnfoldAdminSite`` and drop this override. This module imports
only the unfold site base, never ``django.contrib.auth.admin``: that would re-enter it through
``default_site`` resolution before the class exists. The themed User/Group admins live in
``config/auth_admin.py``, wired from ``OctoreviewAdminConfig.ready()``.
"""

from __future__ import annotations

from django.http import HttpRequest
from unfold.sites import UnfoldAdminSite


class OctoreviewAdminSite(UnfoldAdminSite):
    """Superuser-only admin site; branding comes from ``settings.UNFOLD``."""

    def has_permission(self, request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        return bool(user is not None and user.is_active and user.is_superuser)
