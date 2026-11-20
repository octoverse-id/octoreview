from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("octoreview.api.urls")),
]

# The optional operator console (docs/operations.md "Admin console"). When the flag is off the
# route does not exist, so /admin/ answers the API's JSON 404.
if settings.ADMIN_ENABLED:
    urlpatterns.insert(0, path("admin/", admin.site.urls))

handler404 = "octoreview.core.errors.handler404"
handler500 = "octoreview.core.errors.handler500"
