"""Admin registrations for the operator console (docs/spec.md section 13).

Every model is read-only here: status, rating, counter and version fields change only through
the lifecycle services. The moderation actions on reviews and media call those services with an
`admin:<username>` actor. Publishing a review or approving a media item goes through a
confirmation page that shows the content and carries the review's `version` and
`media_version`, so the version guard holds: nothing the moderator has not seen goes public
(spec section 4.4).
"""

from __future__ import annotations

from django.contrib import admin
from django.template.response import TemplateResponse
from django.utils import timezone

from octoreview.audit.models import AuditLog
from octoreview.console.base import ModerationModelAdmin, ReadOnlyModelAdmin, pretty_json
from octoreview.entities.models import ApplicationSettings, Entity
from octoreview.events.models import OutboxEvent
from octoreview.lifecycle import transitions
from octoreview.lifecycle.media import moderate_media
from octoreview.lifecycle.services import transition_review
from octoreview.media import selectors as media_selectors
from octoreview.media.models import MediaStatus, ReviewMedia
from octoreview.reviews.models import Review, ReviewRevision
from octoreview.service_auth.models import ServiceClient, ServiceClientGrant

# --- service keys: an allowlist, so `hashed_key` (or any later secret column) never renders ---


@admin.register(ServiceClient)
class ServiceClientAdmin(ReadOnlyModelAdmin):
    list_display = ("name", "key_prefix", "is_active", "expires_at", "last_used_at", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "key_prefix")
    ordering = ("name", "id")
    fields = (
        "name",
        "key_prefix",
        "is_active",
        "expires_at",
        "last_used_at",
        "created_at",
        "updated_at",
        "metadata_pretty",
    )
    readonly_fields = fields

    @admin.display(description="Metadata")
    def metadata_pretty(self, obj) -> str:
        return pretty_json(obj.metadata)


@admin.register(ServiceClientGrant)
class ServiceClientGrantAdmin(ReadOnlyModelAdmin):
    list_display = (
        "service_client",
        "tenant_id",
        "application_id",
        "namespace_type",
        "namespace_id",
        "namespace_wildcard",
    )
    list_filter = ("namespace_wildcard",)
    search_fields = ("tenant_id", "application_id")
    ordering = ("tenant_id", "application_id", "id")
    fields = (
        "service_client",
        "tenant_id",
        "application_id",
        "namespace_type",
        "namespace_id",
        "namespace_wildcard",
        "scopes",
    )
    readonly_fields = fields


# --- diagnostics ---------------------------------------------------------------------------


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "action", "aggregate_type", "aggregate_id", "actor_id")
    list_filter = ("action", "aggregate_type")
    search_fields = ("aggregate_id", "request_id")
    ordering = ("-created_at", "-id")
    exclude = ("changes", "metadata")
    readonly_fields = ("changes_pretty", "metadata_pretty")

    @admin.display(description="Changes")
    def changes_pretty(self, obj) -> str:
        return pretty_json(obj.changes)

    @admin.display(description="Metadata")
    def metadata_pretty(self, obj) -> str:
        return pretty_json(obj.metadata)


@admin.register(OutboxEvent)
class OutboxEventAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "event_type", "resource_type", "resource_id", "status")
    list_filter = ("status", "event_type")
    search_fields = ("resource_id",)
    ordering = ("-created_at", "-id")
    exclude = ("payload", "metadata")
    readonly_fields = ("payload_pretty",)

    @admin.display(description="Payload")
    def payload_pretty(self, obj) -> str:
        return pretty_json(obj.payload)


@admin.register(ApplicationSettings)
class ApplicationSettingsAdmin(ReadOnlyModelAdmin):
    list_display = ("tenant_id", "application_id", "moderation_mode", "max_storage_bytes")
    search_fields = ("tenant_id", "application_id")
    ordering = ("tenant_id", "application_id")


@admin.register(Entity)
class EntityAdmin(ReadOnlyModelAdmin):
    list_display = (
        "entity_type",
        "external_id",
        "tenant_id",
        "application_id",
        "is_active",
        "rating_count",
        "created_at",
    )
    list_filter = ("is_active",)
    search_fields = ("external_id",)
    ordering = ("tenant_id", "application_id", "entity_type", "external_id")


# --- moderation ------------------------------------------------------------------------------


def confirm_page(modeladmin, request, queryset, *, action: str, title: str, rows: list[dict]):
    """The confirmation step: shows what will go public and carries the versions it showed."""

    return TemplateResponse(
        request,
        "console/confirm.html",
        {
            **modeladmin.admin_site.each_context(request),
            "title": title,
            "action": action,
            "rows": rows,
            "opts": modeladmin.model._meta,
            "queryset": queryset,
        },
    )


def versions_from(request, review_id) -> tuple[int | None, int | None]:
    """The review versions the confirmation page showed; None (refused) when absent."""

    def number(name):
        value = request.POST.get(f"{name}_{review_id}")
        return int(value) if value and value.isdigit() else None

    return number("version"), number("media_version")


@admin.register(Review)
class ReviewAdmin(ModerationModelAdmin):
    list_display = (
        "title",
        "rating",
        "status",
        "is_verified_purchase",
        "ready_media_count",
        "tenant_id",
        "application_id",
        "created_at",
    )
    list_filter = ("status", "rating", "is_verified_purchase")
    search_fields = ("id", "author_id")
    ordering = ("-created_at", "-id")
    actions = ("publish_selected", "reject_selected", "hide_selected", "remove_selected")

    def _transition(self, request, queryset, action: str, done: str) -> None:
        self.run_each(
            request,
            queryset,
            lambda review, ctx: transition_review(
                review.id, action, ctx, tenant_id=review.tenant_id
            ),
            done,
        )

    @admin.action(description="Publish selected reviews (after confirming their content)")
    def publish_selected(self, request, queryset):
        if request.POST.get("confirm") != "yes":
            now = timezone.now()
            rows = []
            for review in queryset:
                # Publishing makes the review's ready attachments public too (media uploaded
                # while it was pending carry moderation status `none`), so the moderator sees
                # each one here, not only a count.
                attachments = [
                    {
                        "label": f"{media.kind} {media.content_type}, {media.size_bytes} bytes",
                        "url": media_selectors.media_url(
                            media, media_selectors.MODERATOR, now=now
                        ).get("url"),
                    }
                    for media in review.media.filter(status=MediaStatus.READY).order_by(
                        "position", "created_at", "id"
                    )
                ]
                rows.append(
                    {
                        "id": review.id,
                        "label": f"{'★' * review.rating} {review.title or '(no title)'}",
                        "details": [review.body or "(no body)", f"status {review.status}"],
                        "attachments": attachments,
                        "version": review.version,
                        "media_version": review.media_version,
                    }
                )
            return confirm_page(
                self, request, queryset, action="publish_selected", title="Publish", rows=rows
            )

        def publish(review, ctx):
            expected_version, expected_media_version = versions_from(request, review.id)
            transition_review(
                review.id,
                transitions.PUBLISH,
                ctx,
                tenant_id=review.tenant_id,
                expected_version=expected_version,
                expected_media_version=expected_media_version,
            )

        self.run_each(request, queryset, publish, "Published")
        return None

    @admin.action(description="Reject selected reviews")
    def reject_selected(self, request, queryset):
        self._transition(request, queryset, transitions.REJECT, "Rejected")

    @admin.action(description="Hide selected reviews")
    def hide_selected(self, request, queryset):
        self._transition(request, queryset, transitions.HIDE, "Hid")

    @admin.action(description="Remove selected reviews")
    def remove_selected(self, request, queryset):
        self._transition(request, queryset, transitions.REMOVE, "Removed")


@admin.register(ReviewRevision)
class ReviewRevisionAdmin(ReadOnlyModelAdmin):
    list_display = ("review", "rating", "title", "base_version", "created_at")
    ordering = ("-created_at", "-id")


@admin.register(ReviewMedia)
class ReviewMediaAdmin(ModerationModelAdmin):
    list_display = (
        "review",
        "kind",
        "status",
        "moderation_status",
        "content_type",
        "size_bytes",
        "created_at",
    )
    list_filter = ("status", "moderation_status", "kind")
    search_fields = ("id",)
    ordering = ("-created_at", "-id")
    actions = ("approve_selected", "reject_selected")

    @admin.action(description="Approve selected media (after viewing them)")
    def approve_selected(self, request, queryset):
        queryset = queryset.select_related("review")
        if request.POST.get("confirm") != "yes":
            now = timezone.now()
            rows = []
            for media in queryset:
                review = media.review
                url = media_selectors.media_url(media, media_selectors.MODERATOR, now=now)
                rows.append(
                    {
                        "id": media.id,
                        "label": f"{media.kind} {media.content_type}, {media.size_bytes} bytes",
                        "details": [f"review: {review.title or review.id}"],
                        "url": url.get("url"),
                        # The guard is on the review: approving shows media publicly.
                        "version": review.version,
                        "media_version": review.media_version,
                    }
                )
            return confirm_page(
                self, request, queryset, action="approve_selected", title="Approve", rows=rows
            )

        # Each approval adds exactly 1 to its review's media_version (touch_review). Several
        # items of one review approved together therefore chain: the first is checked against
        # the versions the page showed, each later one against the previous approval's result.
        # A change by anyone else still mismatches under the review lock and is refused.
        chained: dict = {}

        def approve(media, ctx):
            expected = chained.get(media.review_id) or versions_from(request, media.id)
            moderate_media(
                media.review_id,
                media.id,
                "approve",
                ctx,
                tenant_id=media.tenant_id,
                expected_version=expected[0],
                expected_media_version=expected[1],
            )
            chained[media.review_id] = (expected[0], expected[1] + 1)

        self.run_each(request, queryset, approve, "Approved")
        return None

    @admin.action(description="Reject selected media")
    def reject_selected(self, request, queryset):
        self.run_each(
            request,
            queryset,
            lambda media, ctx: moderate_media(
                media.review_id, media.id, "reject", ctx, tenant_id=media.tenant_id
            ),
            "Rejected",
        )
