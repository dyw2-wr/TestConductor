from django.apps import AppConfig


class TestPlatformConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.test_platform"
    label = "test_platform"
    verbose_name = "测试流程"


__all__ = ["TestPlatformConfig"]
