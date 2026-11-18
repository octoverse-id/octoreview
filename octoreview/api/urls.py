from django.urls import path

from octoreview.api import audit_logs, health

urlpatterns = [
    path("health/live", health.live, name="health-live"),
    path("health/ready", health.ready, name="health-ready"),
    path("audit-logs", audit_logs.audit_log_list, name="audit-log-list"),
]
