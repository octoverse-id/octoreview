from django.urls import path

from octoreview.api import audit_logs, entities, health, media, reviews

urlpatterns = [
    path("health/live", health.live, name="health-live"),
    path("health/ready", health.ready, name="health-ready"),
    path("audit-logs", audit_logs.audit_log_list, name="audit-log-list"),
    path("entities", entities.entity_collection, name="entity-collection"),
    path("entities/lookup", entities.entity_lookup, name="entity-lookup"),
    path("entities/ratings/query", entities.ratings_query, name="entity-ratings-query"),
    path("entities/<uuid:entity_id>", entities.entity_detail, name="entity-detail"),
    path("entities/<uuid:entity_id>/rating", entities.entity_rating, name="entity-rating"),
    path("reviews", reviews.review_collection, name="review-collection"),
    path("reviews/<uuid:review_id>", reviews.review_detail, name="review-detail"),
    path("reviews/<uuid:review_id>/publish", reviews.review_publish, name="review-publish"),
    path("reviews/<uuid:review_id>/reject", reviews.review_reject, name="review-reject"),
    path("reviews/<uuid:review_id>/hide", reviews.review_hide, name="review-hide"),
    path("reviews/<uuid:review_id>/revision", reviews.review_revision, name="review-revision"),
    path(
        "reviews/<uuid:review_id>/revision/approve",
        reviews.revision_approve,
        name="review-revision-approve",
    ),
    path(
        "reviews/<uuid:review_id>/revision/reject",
        reviews.revision_reject,
        name="review-revision-reject",
    ),
    path("reviews/<uuid:review_id>/media", media.media_collection, name="media-collection"),
    path(
        "reviews/<uuid:review_id>/media/<uuid:media_id>",
        media.media_detail,
        name="media-detail",
    ),
    path(
        "reviews/<uuid:review_id>/media/<uuid:media_id>/complete",
        media.media_complete,
        name="media-complete",
    ),
    path(
        "reviews/<uuid:review_id>/media/<uuid:media_id>/approve",
        media.media_approve,
        name="media-approve",
    ),
    path(
        "reviews/<uuid:review_id>/media/<uuid:media_id>/reject",
        media.media_reject,
        name="media-reject",
    ),
    # Local backend only: signed-token endpoints, no API key (docs/spec.md 7.5).
    path("media-uploads/<str:token>", media.media_upload, name="media-upload"),
    path("media-files/<str:token>", media.media_file, name="media-file"),
    path("authors/erase", reviews.authors_erase, name="authors-erase"),
    path(
        "applications/<str:application_id>/settings",
        entities.application_settings,
        name="application-settings",
    ),
]
