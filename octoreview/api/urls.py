from django.urls import path

from octoreview.api import audit_logs, entities, health

urlpatterns = [
    path("health/live", health.live, name="health-live"),
    path("health/ready", health.ready, name="health-ready"),
    path("audit-logs", audit_logs.audit_log_list, name="audit-log-list"),
    path("entities", entities.entity_collection, name="entity-collection"),
    path("entities/lookup", entities.entity_lookup, name="entity-lookup"),
    path("entities/ratings/query", entities.ratings_query, name="entity-ratings-query"),
    path("entities/<uuid:entity_id>", entities.entity_detail, name="entity-detail"),
    path("entities/<uuid:entity_id>/rating", entities.entity_rating, name="entity-rating"),
    path(
        "applications/<str:application_id>/settings",
        entities.application_settings,
        name="application-settings",
    ),
]
