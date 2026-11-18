from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("octoreview.api.urls")),
]

handler404 = "octoreview.core.errors.handler404"
handler500 = "octoreview.core.errors.handler500"
