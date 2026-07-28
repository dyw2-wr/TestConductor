from typing import ClassVar

from django.db import migrations

RETIRED_CATALOG_KEYS = frozenset({"procedure_profiles", "sediment_profiles"})


def supersede_incompatible_execution_plans(apps, schema_editor):
    ExecutionPlanArtifact = apps.get_model(
        "test_platform",
        "ExecutionPlanArtifact",
    )
    for artifact in ExecutionPlanArtifact.objects.filter(status="approved").iterator():
        catalog = artifact.catalog_snapshot or {}
        approved_catalog = (artifact.approved_bundle or {}).get("catalog_snapshot") or {}
        if RETIRED_CATALOG_KEYS.intersection(catalog) or RETIRED_CATALOG_KEYS.intersection(
            approved_catalog
        ):
            artifact.status = "superseded"
            artifact.last_error = (
                "该执行计划使用了已移除的旧 UI 编排目录，不能继续运行；"
                "请从原测试计划生成新的执行计划。"
            )
            artifact.save(update_fields=("status", "last_error", "updated_at"))


class Migration(migrations.Migration):
    dependencies: ClassVar[list] = [
        ("test_platform", "0007_rename_database_query_file"),
    ]

    operations: ClassVar[list] = [
        migrations.RunPython(
            supersede_incompatible_execution_plans,
            migrations.RunPython.noop,
        ),
    ]
