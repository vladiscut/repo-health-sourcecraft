from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("health.api.urls", namespace="health-api")),
    path("", include("health.urls", namespace="health")),
]
