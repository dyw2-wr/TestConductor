"""Minimal public routes for the new test-platform boundary."""

from django.urls import path

from . import views


urlpatterns = [
    path("", views.health, name="test_platform_health"),
    path("health/", views.health, name="test_platform_health_detail"),
    path("version/", views.version, name="test_platform_version"),
    path(
        "uploads/<path:path_ref>",
        views.uploaded_file,
        name="test_platform_uploaded_file",
    ),
    path(
        "reports/<str:run_id>/<str:kind>/",
        views.report,
        name="test_platform_report",
    ),
    path(
        "reports/<str:run_id>/evidence/<str:name>/",
        views.evidence,
        name="test_platform_evidence",
    ),
    path(
        "reports/<str:run_id>/artifact/<path:path_ref>/",
        views.artifact,
        name="test_platform_artifact",
    ),
]
