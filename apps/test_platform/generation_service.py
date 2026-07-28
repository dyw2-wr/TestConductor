"""Background jobs for model-backed artifact generation.

The admin request only creates or claims a database record.  The worker then
loads that record, calls the ordinary workflow service, and writes a terminal
artifact state.  This keeps a slow or disconnected browser from changing the
meaning of a generation job.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .intent.contracts import ApprovedTestDesignBundle, compute_design_content_hash
from .file_lock import nonblocking_file_lock
from .ingestion import IngestionLimits, InputFile
from .models import (
    ExecutionPlanArtifact,
    TestPlanArtifact,
    TestWorkflow,
)
from .planning.compiler import TestPlanCompiler
from .planning.contracts import ApprovedTestPlanBundle, PlanStatus
from .planning.resources import resolve_test_resources
from .service_factory import get_model_gateway, get_workflow


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _progress_payload(phase: str, message: str, percent: int) -> dict[str, Any]:
    return {
        "phase": str(phase),
        "message": str(message),
        "percent": max(0, min(100, int(percent))),
        "updated_at": timezone.now().isoformat(),
    }


def _set_workflow_progress(
    workflow_id: int,
    phase: str,
    message: str,
    percent: int,
) -> None:
    TestWorkflow.objects.filter(pk=workflow_id).update(
        generation_progress=_progress_payload(phase, message, percent),
        updated_at=timezone.now(),
    )


def _set_execution_progress(
    artifact_id: int,
    phase: str,
    message: str,
    percent: int,
) -> None:
    ExecutionPlanArtifact.objects.filter(pk=artifact_id).update(
        generation_progress=_progress_payload(phase, message, percent),
        updated_at=timezone.now(),
    )


def _execution_progress_percent(artifact_id: int) -> int:
    progress = (
        ExecutionPlanArtifact.objects.filter(pk=artifact_id)
        .values_list("generation_progress", flat=True)
        .first()
        or {}
    )
    try:
        return max(0, min(100, int(progress.get("percent") or 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def artifact_generation_lock(root: Path, identity: str):
    lock_root = root / ".generation-locks"
    name = str(identity or "").strip()
    if not name or Path(name).name != name:
        raise ValidationError("产物锁标识无效")
    return nonblocking_file_lock(
        lock_root / f"{name}.lock",
        conflict_message="该产物正在生成，请稍后查看结果",
    )


def _job_log_root(name: str) -> Path:
    root = Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve() / "generation-jobs" / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _spawn(command: list[str], log_root: Path, name: str) -> None:
    log_path = log_root / f"{name}.log"
    with log_path.open("ab") as output:
        subprocess.Popen(
            command,
            cwd=str(settings.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _read_workflow_files(obj: TestWorkflow) -> list[InputFile]:
    limits = IngestionLimits().validate()
    files: list[InputFile] = []
    total_bytes = 0
    document_fields = (("requirement", obj.requirement_file),)
    for category, uploaded in document_fields:
        if not uploaded:
            continue
        uploaded.open("rb")
        try:
            original_name = Path(uploaded.name).name
            declared_size = int(getattr(uploaded, "size", 0) or 0)
            if declared_size > limits.max_file_bytes:
                raise ValidationError(f"{original_name} 超过单文件大小限制")
            data = uploaded.read(limits.max_file_bytes + 1)
            if len(data) > limits.max_file_bytes:
                raise ValidationError(f"{original_name} 超过单文件大小限制")
            total_bytes += len(data)
            if total_bytes > limits.max_total_file_bytes:
                raise ValidationError("上传文件总大小超过限制")
            files.append(InputFile(f"{category}-{original_name}", data))
        finally:
            uploaded.close()
    return files


def generate_design_artifact(
    workflow_id: int,
    *,
    previous_artifact_id: int | None = None,
) -> TestPlanArtifact:
    obj = TestWorkflow.objects.select_related("resource_profile").get(pk=workflow_id)
    _set_workflow_progress(workflow_id, "loading_input", "正在读取需求和测试资源", 10)
    previous_artifact = None
    if previous_artifact_id is not None:
        previous_artifact = obj.test_plan_artifacts.filter(
            pk=previous_artifact_id
        ).first()
        if previous_artifact is None:
            raise ValidationError("重新生成来源测试计划不属于当前测试意图")
    if obj.resource_profile is None or not obj.resource_profile.enabled:
        raise ValidationError("请先选择一个已启用的测试资源配置")

    service = get_workflow(
        progress_callback=lambda phase, message, percent: _set_workflow_progress(
            workflow_id,
            phase,
            message,
            percent,
        )
    )
    limits = IngestionLimits().validate()
    ingested = service.prepare_design_request(
        frontend_text=obj.requirement_text or None,
        files=_read_workflow_files(obj),
        target={"system_id": obj.system_id, "environment": obj.target_environment},
        selections={
            "techniques": sorted(
                {
                    technique
                    for values in (obj.coverage_by_category or {}).values()
                    for technique in values
                }
            ),
            "techniques_by_channel": obj.coverage_by_category or {},
            "allowed_channels": obj.allowed_channels,
            "required_channels": obj.allowed_channels,
            "knowledge_scope_ids": obj.knowledge_scope_ids,
        },
        request_id=None,
        limits=limits,
    )
    _set_workflow_progress(workflow_id, "preparing_context", "需求已读取，正在准备生成上下文", 25)
    previous = (previous_artifact.generation_result if previous_artifact else {}) or {}
    previous_design = previous.get("design") or {}
    version = int(previous_artifact.version if previous_artifact else 0) + 1
    review_feedback = None
    if previous_artifact is not None:
        review_feedback = (
            str(previous_artifact.review_comments or "").strip()
            or str((previous_artifact.review_payload or {}).get("comments") or "").strip()
            or "请根据审核意见修订测试计划"
        )
    result = service.generate_design(
        ingested.request,
        design_id=previous_design.get("design_id") or None,
        version=version,
        review_feedback=review_feedback,
    )
    _set_workflow_progress(workflow_id, "validating_design", "正在校验测试计划完整性", 82)
    validation = _json(result.validation)
    _set_workflow_progress(workflow_id, "saving_design", "正在保存测试计划和审核快照", 92)
    artifact = TestPlanArtifact.objects.create(
        source_intent=obj,
        resource_profile=obj.resource_profile,
        title=result.design.title,
        test_categories=list(obj.allowed_channels),
        design_id=result.design.design_id,
        version=result.design.version,
        content_hash=compute_design_content_hash(result.design),
        generation_result={
            key: _json(getattr(result, key))
            for key in ("request", "candidate", "design", "input_snapshot", "validation")
        }
        | {"ingestion": ingested.as_dict()},
        status=(
            TestPlanArtifact.Status.REVIEW
            if validation.get("passed") is True
            else TestPlanArtifact.Status.BLOCKED
        ),
        last_error=""
        if validation.get("passed") is True
        else "；".join(
            str(item.get("message"))
            for item in validation.get("findings", [])
            if item.get("blocking")
        ),
    )
    obj.status = TestWorkflow.Status.DESIGN_REVIEW
    obj.last_error = ""
    obj.generation_progress = _progress_payload(
        "completed",
        "测试计划已生成，等待审批",
        100,
    )
    obj.save(
        update_fields=(
            "status",
            "last_error",
            "generation_progress",
            "updated_at",
        )
    )
    return artifact


def queue_design_generation(
    workflow: TestWorkflow,
    *,
    count: int = 1,
    previous_artifact_id: int | None = None,
) -> None:
    if not 1 <= int(count) <= 10:
        raise ValidationError("一次最多生成 10 个测试计划")
    allowed_statuses = set(TestWorkflow.Status.values) - {
        TestWorkflow.Status.DESIGN_GENERATING,
    }
    # A conditional UPDATE is an atomic claim even when two requests hold stale
    # copies of the same workflow.  Saving the passed instance allowed both
    # requests to spawn an expensive model worker.
    claimed = TestWorkflow.objects.filter(
        pk=workflow.pk, status__in=allowed_statuses
    ).update(
        status=TestWorkflow.Status.DESIGN_GENERATING,
        generation_progress=_progress_payload(
            "queued",
            "已进入后台队列，等待生成进程启动",
            0,
        ),
        last_error="",
        updated_at=timezone.now(),
    )
    if claimed != 1:
        raise ValidationError("当前测试意图不允许重新生成")
    workflow.refresh_from_db()
    command = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "generate_test_design",
        "--workflow-id",
        str(workflow.pk),
        "--count",
        str(int(count)),
    ]
    if previous_artifact_id is not None:
        command.extend(["--previous-artifact-id", str(previous_artifact_id)])
    try:
        _spawn(command, _job_log_root("design"), workflow.workflow_id)
    except Exception as exc:
        TestWorkflow.objects.filter(
            pk=workflow.pk, status=TestWorkflow.Status.DESIGN_GENERATING
        ).update(
            status=TestWorkflow.Status.ERROR,
            generation_progress=_progress_payload(
                "failed",
                "后台生成进程启动失败",
                0,
            ),
            last_error=f"后台生成进程启动失败: {exc}"[:20_000],
            updated_at=timezone.now(),
        )
        raise ValidationError("后台生成进程启动失败") from exc


def _execution_root(
    test_plan: TestPlanArtifact,
    execution_plan: ExecutionPlanArtifact,
) -> Path:
    root = Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
    value = (
        root
        / "test-plans"
        / test_plan.artifact_id
        / "execution-batches"
        / execution_plan.artifact_id
    )
    value.resolve().relative_to(root)
    return value


def _claim_execution_placeholder(
    test_plan: TestPlanArtifact,
    *,
    feedback: str | None = None,
    execution_input=None,
) -> ExecutionPlanArtifact:
    if test_plan.status != TestPlanArtifact.Status.APPROVED:
        raise ValidationError("只有已审批测试计划可以生成执行计划")
    from .input_contracts import validate_runtime_input

    frozen_input = validate_runtime_input(execution_input).model_dump(
        mode="json",
        exclude_none=True,
    )
    try:
        with transaction.atomic():
            test_plan = TestPlanArtifact.objects.select_for_update().get(pk=test_plan.pk)
            if test_plan.execution_plans.filter(
                status=ExecutionPlanArtifact.Status.GENERATING
            ).exists():
                raise ValidationError("该测试计划已有执行计划正在生成")
            previous = test_plan.execution_plans.order_by("-version", "-id").first()
            version = int(previous.version if previous else 0) + 1
            plan_id = (
                ((previous.compilation_result or {}).get("plan") or {}).get("plan_id")
                if previous
                else None
            ) or f"plan-{test_plan.artifact_id}"
            placeholder = ExecutionPlanArtifact.objects.create(
                source_test_plan=test_plan,
                resource_profile=test_plan.resource_profile,
                title=test_plan.title,
                test_categories=list(test_plan.test_categories),
                plan_id=plan_id,
                version=version,
                content_hash="",
                catalog_snapshot={},
                runtime_config_hash="",
                execution_input=frozen_input,
                compilation_result={},
                artifact_root_ref="",
                status=ExecutionPlanArtifact.Status.GENERATING,
                generation_progress=_progress_payload(
                    "queued",
                    "已进入后台队列，等待生成进程启动",
                    0,
                ),
                review_comments=(feedback or "").strip(),
            )
    except IntegrityError as exc:
        raise ValidationError("该测试计划已有执行计划正在生成") from exc
    return placeholder


def _launch_execution_worker(
    placeholder: ExecutionPlanArtifact,
    *,
    command_name: str,
) -> None:
    command = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        command_name,
        "--artifact-id",
        str(placeholder.pk),
    ]
    try:
        _spawn(command, _job_log_root("execution"), placeholder.artifact_id)
    except Exception as exc:
        ExecutionPlanArtifact.objects.filter(pk=placeholder.pk).update(
            status=ExecutionPlanArtifact.Status.ERROR,
            generation_progress=_progress_payload(
                "failed",
                "后台生成进程启动失败",
                0,
            ),
            last_error=f"后台生成进程启动失败: {exc}"[:20_000],
        )
        raise ValidationError("后台生成进程启动失败") from exc


def queue_execution_plan_generation(
    test_plan: TestPlanArtifact,
    *,
    feedback: str | None = None,
    execution_input=None,
) -> ExecutionPlanArtifact:
    placeholder = _claim_execution_placeholder(
        test_plan,
        feedback=feedback,
        execution_input=execution_input,
    )
    _launch_execution_worker(
        placeholder,
        command_name="generate_execution_plan",
    )
    return placeholder


def queue_execution_plan_rebind(
    test_plan: TestPlanArtifact,
) -> ExecutionPlanArtifact:
    """Recompile an approved mapping against current resources without an LLM."""

    previous = test_plan.execution_plans.filter(
        status=ExecutionPlanArtifact.Status.APPROVED,
    ).exclude(approved_bundle={}).order_by("-version", "-id").first()
    if previous is None:
        raise ValidationError("没有可用于重新绑定的已审批执行计划")
    placeholder = _claim_execution_placeholder(
        test_plan,
        feedback="测试资源已更新，确定性重编译已审批映射",
        execution_input=previous.execution_input,
    )
    _launch_execution_worker(
        placeholder,
        command_name="rebind_execution_plan",
    )
    return placeholder


def _persist_execution_compilation(
    placeholder: ExecutionPlanArtifact,
    *,
    result,
    catalog,
    runtime_config_hash: str,
    artifact_root: Path,
    storage_root: Path,
) -> ExecutionPlanArtifact:
    validation = _json(result.validation)
    passed = validation.get("passed") is True and bool(result.artifacts)
    placeholder.plan_id = result.plan.plan_id
    placeholder.content_hash = result.plan.content_hash()
    placeholder.catalog_snapshot = catalog.model_dump(mode="json")
    placeholder.runtime_config_hash = runtime_config_hash
    placeholder.compilation_result = {
        key: _json(getattr(result, key))
        for key in ("plan", "validation", "artifacts")
    }
    placeholder.artifact_root_ref = (
        artifact_root.relative_to(storage_root).as_posix() if passed else ""
    )
    placeholder.status = (
        ExecutionPlanArtifact.Status.REVIEW
        if passed
        else ExecutionPlanArtifact.Status.BLOCKED
    )
    placeholder.generation_progress = _progress_payload(
        "completed",
        (
            "执行计划已生成，等待审批"
            if passed
            else "执行计划已生成，但规则校验未通过"
        ),
        100,
    )
    placeholder.last_error = "" if passed else "；".join(
        str(item.get("message"))
        for item in validation.get("findings", [])
        if item.get("blocking")
    ) or "没有可审批的执行产物"
    placeholder.save()
    return placeholder


def generate_execution_plan_artifact(
    artifact_id: int,
) -> ExecutionPlanArtifact:
    placeholder = ExecutionPlanArtifact.objects.select_related(
        "source_test_plan", "resource_profile"
    ).get(pk=artifact_id)
    if placeholder.status != ExecutionPlanArtifact.Status.GENERATING:
        raise ValidationError("执行计划生成记录不是生成中状态")
    source = placeholder.source_test_plan
    root = Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
    try:
        _set_execution_progress(
            placeholder.pk,
            "loading_plan",
            "正在读取已审批测试计划",
            10,
        )
        with artifact_generation_lock(root, source.artifact_id):
            approved_design = ApprovedTestDesignBundle.model_validate(
                source.approved_bundle
            )
            previous = source.execution_plans.exclude(pk=placeholder.pk).order_by(
                "-version", "-id"
            ).first()
            previous_plan = (
                (previous.compilation_result or {}).get("plan") or {}
                if previous
                else {}
            )
            progress_callback = lambda phase, message, percent: _set_execution_progress(
                    placeholder.pk,
                    phase,
                    message,
                    percent,
                )
            resolved = get_workflow(progress_callback=progress_callback)
            _set_execution_progress(
                placeholder.pk,
                "resolving_resources",
                "正在解析并校验测试资源",
                22,
            )
            resources = resolve_test_resources(
                source.resource_profile,
                approved_design,
                resource_model_gateway=get_model_gateway(
                    "planning",
                    progress_callback=progress_callback,
                ),
            )
            catalog = resources.catalog
            catalog.require_target(
                approved_design.design.target.system_id,
                approved_design.design.target.environment,
            )
            artifact_root = _execution_root(source, placeholder)
            if artifact_root.exists():
                shutil.rmtree(artifact_root)
            result = resolved.compile_plan(
                approved_design,
                catalog,
                artifact_root,
                plan_id=previous_plan.get("plan_id") or placeholder.plan_id,
                version=placeholder.version,
                review_feedback=placeholder.review_comments or None,
                execution_input=placeholder.execution_input,
            )
            _set_execution_progress(
                placeholder.pk,
                "saving_artifacts",
                "正在保存执行文件和审核快照",
                92,
            )
            return _persist_execution_compilation(
                placeholder,
                result=result,
                catalog=catalog,
                runtime_config_hash=resources.runtime_config_hash,
                artifact_root=artifact_root,
                storage_root=root,
            )
    except Exception as exc:
        placeholder.status = ExecutionPlanArtifact.Status.ERROR
        placeholder.generation_progress = _progress_payload(
            "failed",
            "执行计划生成失败",
            _execution_progress_percent(placeholder.pk),
        )
        placeholder.last_error = str(exc)[:20_000]
        placeholder.save(
            update_fields=(
                "status",
                "generation_progress",
                "last_error",
                "updated_at",
            )
        )
        raise


def rebind_execution_plan_artifact(
    artifact_id: int,
) -> ExecutionPlanArtifact:
    placeholder = ExecutionPlanArtifact.objects.select_related(
        "source_test_plan", "resource_profile"
    ).get(pk=artifact_id)
    if placeholder.status != ExecutionPlanArtifact.Status.GENERATING:
        raise ValidationError("执行计划重新绑定记录不是生成中状态")
    source = placeholder.source_test_plan
    root = Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
    try:
        _set_execution_progress(
            placeholder.pk,
            "loading_plan",
            "正在读取已审批执行计划",
            10,
        )
        with artifact_generation_lock(root, source.artifact_id):
            previous = source.execution_plans.exclude(pk=placeholder.pk).filter(
                status=ExecutionPlanArtifact.Status.APPROVED
            ).order_by("-version", "-id").first()
            if previous is None or not previous.approved_bundle:
                raise ValidationError("没有可用于重新绑定的已审批执行计划")
            approved_design = ApprovedTestDesignBundle.model_validate(
                source.approved_bundle
            )
            approved_plan = ApprovedTestPlanBundle.model_validate(
                previous.approved_bundle
            )
            _set_execution_progress(
                placeholder.pk,
                "resolving_resources",
                "正在解析并校验最新测试资源",
                35,
            )
            resources = resolve_test_resources(
                source.resource_profile,
                approved_design,
                resource_model_gateway=get_model_gateway("planning"),
            )
            catalog = resources.catalog
            catalog.require_target(
                approved_design.design.target.system_id,
                approved_design.design.target.environment,
            )
            rebound_plan = approved_plan.plan.model_copy(
                update={
                    "version": placeholder.version,
                    "status": PlanStatus.DRAFT,
                    "catalog_id": catalog.catalog_id,
                    "catalog_content_hash": catalog.content_hash,
                    "blocked_reasons": [],
                }
            )
            artifact_root = _execution_root(source, placeholder)
            if artifact_root.exists():
                shutil.rmtree(artifact_root)
            _set_execution_progress(
                placeholder.pk,
                "compiling_artifacts",
                "正在根据最新资源重新编译执行文件",
                65,
            )
            result = TestPlanCompiler().compile(
                approved_design,
                rebound_plan,
                catalog,
                artifact_root,
            )
            _set_execution_progress(
                placeholder.pk,
                "saving_artifacts",
                "正在保存重新绑定后的执行文件",
                92,
            )
            return _persist_execution_compilation(
                placeholder,
                result=result,
                catalog=catalog,
                runtime_config_hash=resources.runtime_config_hash,
                artifact_root=artifact_root,
                storage_root=root,
            )
    except Exception as exc:
        placeholder.status = ExecutionPlanArtifact.Status.ERROR
        placeholder.generation_progress = _progress_payload(
            "failed",
            "执行计划重新绑定失败",
            _execution_progress_percent(placeholder.pk),
        )
        placeholder.last_error = str(exc)[:20_000]
        placeholder.save(
            update_fields=(
                "status",
                "generation_progress",
                "last_error",
                "updated_at",
            )
        )
        raise


__all__ = [
    "artifact_generation_lock",
    "generate_design_artifact",
    "generate_execution_plan_artifact",
    "rebind_execution_plan_artifact",
    "queue_design_generation",
    "queue_execution_plan_generation",
    "queue_execution_plan_rebind",
]
