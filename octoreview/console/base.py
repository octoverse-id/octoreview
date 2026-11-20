"""Shared pieces of the operator console, ported from octonomy's `core/admin_base.py`.

The console is a thin presentation layer: it reads models directly, and every write goes
through the lifecycle services, which enforce the state machine, the version guard and the
audit + outbox side effects. A service refusal becomes an admin message, never a 500.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from django.contrib import admin, messages
from django.http import HttpRequest
from django.utils.html import format_html
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from octoreview.core.audit_context import AuditContext
from octoreview.core.errors import DomainError


def admin_audit_context(
    request: HttpRequest, operation_id: uuid.UUID | None = None
) -> AuditContext:
    """`admin:<username>` as the actor, so console writes are attributable in audit and events.

    One operation id per submission, shared by every row of a bulk action.
    """

    return AuditContext(
        actor_id=f"admin:{request.user.get_username()}"[:255],
        request_id=getattr(request, "request_id", None),
        operation_id=operation_id or uuid.uuid4(),
    )


def pretty_json(value: Any) -> str:
    try:
        text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return format_html('<pre style="white-space:pre-wrap;margin:0">{}</pre>', text)


class ReadOnlyModelAdmin(UnfoldModelAdmin):
    """View, list, search and filter only: every add, change and delete is denied."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_actions(self, request):
        return {}


class ModerationModelAdmin(ReadOnlyModelAdmin):
    """Read-only, plus the moderation actions a subclass lists in `actions`."""

    def get_actions(self, request):
        return admin.ModelAdmin.get_actions(self, request)

    @staticmethod
    def run_each(request, rows, write, done: str) -> None:
        """Apply `write(row, ctx)` per row under one operation id; report successes and refusals."""

        ctx = admin_audit_context(request)
        succeeded = 0
        for row in rows:
            try:
                write(row, ctx)
            except DomainError as exc:
                messages.error(request, f"{row}: {exc.message} ({exc.code})")
            else:
                succeeded += 1
        if succeeded:
            messages.success(request, f"{done} {succeeded} item(s).")
