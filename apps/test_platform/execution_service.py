"""Queue and execute one approved execution-plan artifact.

The admin request only persists a queued run and starts a worker command.  The
worker reloads every approved artifact from the database, revalidates the live
resource configuration, and then calls the ordinary execution coordinator.
Secrets remain process configuration and never enter command-line arguments.
"""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .intent.contracts import ApprovedTestDesignBundle
from .intent.contracts import contains_secret_value
from .input_contracts import validate_runtime_input
from .models import ExecutionPlanArtifact, TestExecutionRun
from .planning.catalogs import PlanningCatalogSnapshot
from .planning.contracts import ApprovedTestPlanBundle
from .planning.resources import resolve_test_resources
from .run_history import generate_run_id
from .service_factory import get_runtime_context, get_workflow


def _canonical_resolved_path(path: Path) -> Path:
    r"""Resolve one path into a stable namespace for containment checks.

    While another thread creates a parent directory, Windows may return the
    same path once as ``C:\...`` and once as ``\\?\C:\...``.  Comparing those
    spellings directly produces a false traversal result.  Normalize only the
    two equivalent Win32 extended-path forms; real ``..``, symlink, drive and
    UNC escapes remain resolved and are still rejected by ``relative_to``.
    """

    resolved = path.resolve()
    if os.name != "nt":
        return resolved
    value = str(resolved)
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return resolved


def _artifact_root(artifact: ExecutionPlanArtifact) -> Path:
    storage_root = _canonical_resolved_path(
        Path(settings.TEST_PLATFORM_ARTIFACT_ROOT)
    )
    artifact_root = _canonical_resolved_path(
        storage_root / Path(artifact.artifact_root_ref)
    )
    try:
        artifact_root.relative_to(storage_root)
    except ValueError as exc:
        raise ValidationError("执行产物目录超出平台存储范围") from exc
    return artifact_root


def _validated_execution_inputs(artifact: ExecutionPlanArtifact):
    if artifact.status != ExecutionPlanArtifact.Status.APPROVED:
        raise ValidationError("只能执行已审批的执行计划")
    approved_plan = ApprovedTestPlanBundle.model_validate(artifact.approved_bundle)
    approved_design = ApprovedTestDesignBundle.model_validate(
        artifact.source_test_plan.approved_bundle
    )
    frozen_catalog = PlanningCatalogSnapshot.model_validate(artifact.catalog_snapshot)
    current_resources = resolve_test_resources(artifact.resource_profile, approved_design)
    if current_resources.catalog.content_hash != frozen_catalog.content_hash:
        raise ValidationError("测试能力已经变化，请重新生成并审批执行计划")
    if (
        not artifact.runtime_config_hash
        or current_resources.runtime_config_hash != artifact.runtime_config_hash
    ):
        raise ValidationError(
            "测试运行地址、查询或负载配置已经变化，请重新生成并审批执行计划"
        )
    return approved_plan, current_resources


def mark_run_error(run_id: str, message: str) -> None:
    """Put a queued/running worker failure into an auditable terminal state."""

    updated = TestExecutionRun.objects.filter(
        run_id=run_id,
        status__in=(TestExecutionRun.Status.QUEUED, TestExecutionRun.Status.RUNNING),
    ).update(
        status=TestExecutionRun.Status.ERROR,
        report_status=TestExecutionRun.ReportStatus.FAILED,
        finished_at=timezone.now(),
        errors=["EXECUTION_WORKER_FAILED"],
        updated_at=timezone.now(),
    )
    if not updated:
        return
    run = TestExecutionRun.objects.filter(run_id=run_id).select_related(
        "execution_plan"
    ).first()
    if run is not None and run.execution_plan_id:
        run.execution_plan.last_error = str(message)[:20_000]
        run.execution_plan.save(update_fields=("last_error", "updated_at"))


def execute_execution_plan_artifact(
    artifact_id: int,
    *,
    run_id: str | None = None,
):
    """Synchronously execute one artifact; intended for the worker command/tests."""

    artifact = ExecutionPlanArtifact.objects.select_related(
        "source_test_plan", "resource_profile"
    ).get(pk=artifact_id)
    approved_plan, resources = _validated_execution_inputs(artifact)
    artifact_root = _artifact_root(artifact)
    artifact_root.mkdir(parents=True, exist_ok=True)
    context = get_runtime_context(
        evidence_dir=artifact_root / "evidence",
        runtime_config=resources.runtime_config,
        execution_input=artifact.execution_input,
    )
    summary = get_workflow().execute(
        approved_plan,
        artifact_root,
        context,
        run_id=run_id,
    )
    run = TestExecutionRun.objects.filter(run_id=summary.run_id).first()
    if run is not None:
        changed = []
        if run.resource_profile_id != artifact.resource_profile_id:
            run.resource_profile = artifact.resource_profile
            changed.append("resource_profile")
        if run.execution_plan_id != artifact.pk:
            run.execution_plan = artifact
            changed.append("execution_plan")
        if changed:
            run.save(update_fields=(*changed, "updated_at"))

    status = getattr(summary.status, "value", str(summary.status))
    if status == "passed":
        artifact.last_error = ""
    else:
        artifact.last_error = "；".join(summary.errors) or "测试未通过，详见运行报告"
    artifact.save(update_fields=("last_error", "updated_at"))
    return summary


def queue_execution_plan_artifact(
    artifact: ExecutionPlanArtifact,
) -> TestExecutionRun:
    """Validate, persist, and launch one background worker process."""

    submitted = validate_runtime_input(artifact.execution_input)
    submitted_payload = submitted.model_dump(mode="json", exclude_none=True)
    if contains_secret_value(submitted_payload):
        raise ValidationError("本次运行变量不能包含秘密值")
    # Fail obvious approval/resource drift in the web request before recording a job.
    _validated_execution_inputs(artifact)
    artifact_root = _artifact_root(artifact)
    artifact_root.mkdir(parents=True, exist_ok=True)
    run_id = generate_run_id()
    try:
        with transaction.atomic():
            artifact = (
                ExecutionPlanArtifact.objects.select_for_update()
                .select_related("resource_profile")
                .get(pk=artifact.pk)
            )
            if TestExecutionRun.objects.filter(
                execution_plan=artifact,
                status__in=(
                    TestExecutionRun.Status.QUEUED,
                    TestExecutionRun.Status.RUNNING,
                ),
            ).exists():
                raise ValidationError("该执行计划已有排队中或运行中的批次")
            run = TestExecutionRun.objects.create(
                run_id=run_id,
                status=TestExecutionRun.Status.QUEUED,
                report_status=TestExecutionRun.ReportStatus.PENDING,
                execution_plan=artifact,
                resource_profile=artifact.resource_profile,
                started_at=timezone.now(),
                storage_root_ref=artifact_root.relative_to(
                    _canonical_resolved_path(
                        Path(settings.TEST_PLATFORM_ARTIFACT_ROOT)
                    )
                ).as_posix(),
            )
    except IntegrityError as exc:
        raise ValidationError("该执行计划已有排队中或运行中的批次") from exc

    log_dir = artifact_root / "worker-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"
    command = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "run_test_plan",
        "--execution-plan-id",
        str(artifact.pk),
        "--run-id",
        run_id,
    ]
    worker_environment = os.environ.copy()
    worker_environment.pop("TEST_PLATFORM_EXECUTION_INPUT_JSON", None)
    try:
        with log_path.open("ab") as output:
            subprocess.Popen(
                command,
                cwd=str(settings.BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=worker_environment,
            )
    except Exception as exc:
        mark_run_error(run_id, f"后台执行进程启动失败: {exc}")
        raise ValidationError("后台执行进程启动失败") from exc
    return run


__all__ = [
    "execute_execution_plan_artifact",
    "mark_run_error",
    "queue_execution_plan_artifact",
]
