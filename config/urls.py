"""URL configuration for the active TestDesign v4 platform."""

from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.test_platform.urls")),
]
