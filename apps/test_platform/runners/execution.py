"""Sequential v4 PlanFlow execution coordinator.

The coordinator validates one approved handoff, preflights every stage in a flow,
then executes that flow in declared order. A stage may use exactly one executor;
there is deliberately no DAG or parallel scheduler here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from apps.test_platform.run_history import (
    RunIdConflict,
    generate_run_id,
    validate_run_id,
)

from .base import (
    prepare_artifact,
    prepare_flow_cleanup,
    run_prepared_flow_cleanup,
    write_evidence,
)
from .contracts import (
    ExecutionSummary,
    FlowRunResult,
    RunManifest,
    RunResult,
    RunStatus,
    RunnerError,
    RuntimeContext,
    StepResult,
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _text_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _status_value(value: Any) -> str:
    return _text_value(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result_status(results: list[RunResult]) -> RunStatus:
    values = {_status_value(item.status) for item in results}
    if RunStatus.ERROR.value in values:
        return RunStatus.ERROR
    if RunStatus.FAILED.value in values:
        return RunStatus.FAILED
    if RunStatus.BLOCKED.value in values:
        return RunStatus.BLOCKED
    if values & {RunStatus.INCONCLUSIVE.value, RunStatus.DRY_RUN.value}:
        return RunStatus.INCONCLUSIVE
    if results and values == {RunStatus.PASSED.value}:
        return RunStatus.PASSED
    return RunStatus.INCONCLUSIVE


class ExecutionCoordinator:
    """Execute approved v4 flows sequentially."""

    def __init__(
        self,
        registry: Any | None = None,
        reporter: Any | None = None,
        run_history_recorder: Any | None = None,
    ):
        if registry is None:
            from . import RunnerRegistry

            registry = RunnerRegistry()
        if reporter is None:
            from apps.test_platform.reporting import TestReportGenerator

            reporter = TestReportGenerator()
        self.registry = registry
        self.reporter = reporter
        self.run_history_recorder = run_history_recorder

    def execute(
        self,
        bundle: Any,
        artifact_root: str | Path,
        context: RuntimeContext,
        *,
        run_id: str | None = None,
    ) -> ExecutionSummary:
        run_id = validate_run_id(run_id or generate_run_id())
        started_at = _now()
        root = Path(artifact_root).resolve()
        recorder = self.run_history_recorder
        if recorder is not None:
            try:
                recorder.begin(
                    run_id=run_id,
                    started_at=started_at,
                    artifact_root=root,
                )
            except RunIdConflict as exc:
                return self._blocked_summary(
                    run_id,
                    "RUN_ID_CONFLICT",
                    str(exc),
                    started_at=started_at,
                    finished_at=_now(),
                )
            except Exception as exc:
                return self._blocked_summary(
                    run_id,
                    "RUN_HISTORY_START_FAILED",
                    str(exc),
                    started_at=started_at,
                    finished_at=_now(),
                )

        try:
            return self._execute_started(
                bundle,
                root,
                context,
                run_id=run_id,
                started_at=started_at,
                run_history_recorder=recorder,
            )
        except Exception as exc:
            summary = ExecutionSummary(
                run_id=run_id,
                status=RunStatus.ERROR,
                errors=[f"EXECUTION_UNHANDLED_ERROR: {exc}"],
                started_at=started_at,
                finished_at=_now(),
            )
            return self._attach_report(
                summary,
                artifact_root=root,
                context=context,
                run_history_recorder=recorder,
            )

    def _execute_started(
        self,
        bundle: Any,
        root: Path,
        context: RuntimeContext,
        *,
        run_id: str,
        started_at: str,
        run_history_recorder: Any | None,
    ) -> ExecutionSummary:
        plan = None

        def blocked(code: str, message: str) -> ExecutionSummary:
            summary = self._blocked_summary(
                run_id,
                code,
                message,
                started_at=started_at,
                finished_at=_now(),
            )
            return self._attach_report(
                summary,
                artifact_root=root,
                context=context,
                plan=plan,
                run_history_recorder=run_history_recorder,
            )

        try:
            from apps.test_platform.planning.contracts import ApprovedTestPlanBundle

            payload = (
                bundle.model_dump(mode="json")
                if callable(getattr(bundle, "model_dump", None))
                else bundle
            )
            bundle = ApprovedTestPlanBundle.model_validate(payload)
        except Exception as exc:
            return blocked(
                "PLAN_HANDOFF_INVALID",
                f"approved bundle 校验失败: {exc}",
            )

        plan = bundle.plan
        artifacts = list(bundle.compiled_artifacts)
        if _status_value(plan.status) != "approved":
            return blocked("PLAN_NOT_APPROVED", "第三层只接受 approved 计划")
        if bundle.validation.passed is not True:
            return blocked("PLAN_VALIDATION_FAILED", "计划校验未通过")
        if _status_value(bundle.review.decision) != "approved":
            return blocked("PLAN_REVIEW_REQUIRED", "计划尚未审核通过")
        plan_hash = plan.content_hash()
        if bundle.review.plan_content_hash != plan_hash:
            return blocked(
                "PLAN_REVIEW_HASH_MISMATCH",
                "审核记录与计划内容 hash 不一致",
            )

        artifact_by_stage = {
            (str(item.flow_id), str(item.stage_id)): item for item in artifacts
        }
        expected_keys = {
            (str(flow.flow_id), str(stage.stage_id))
            for flow in plan.flows
            for stage in flow.stages
        }
        if len(artifact_by_stage) != len(artifacts) or set(artifact_by_stage) != expected_keys:
            return blocked(
                "ARTIFACT_STAGE_MISMATCH",
                "计划 stage 与编译产物必须一一对应",
            )
        identity_error = self._validate_artifact_identities(plan, plan_hash, artifacts)
        if identity_error is not None:
            return blocked("ARTIFACT_IDENTITY_MISMATCH", identity_error)

        flow_results: list[FlowRunResult] = []
        for flow in plan.flows:
            flow_results.append(
                self._execute_flow(
                    flow,
                    plan,
                    artifact_by_stage,
                    root,
                    context,
                    run_id,
                )
            )
        return self._finish_summary(
            run_id,
            plan,
            flow_results,
            context,
            started_at,
            artifacts,
            validation_content_hash=str(bundle.validation.validation_content_hash),
            review_content_hash=str(bundle.review.review_content_hash),
            artifact_set_hash=str(bundle.review.artifact_set_hash),
            artifact_root=root,
            run_history_recorder=run_history_recorder,
        )

    @staticmethod
    def _validate_artifact_identities(plan, plan_hash: str, artifacts: list[Any]) -> str | None:
        stages = {
            (str(flow.flow_id), str(stage.stage_id)): stage
            for flow in plan.flows
            for stage in flow.stages
        }
        for artifact in artifacts:
            key = (str(artifact.flow_id), str(artifact.stage_id))
            stage = stages.get(key)
            if stage is None:
                return f"未知 flow/stage: {key}"
            if _text_value(stage.executor_kind) != _text_value(artifact.executor_kind):
                return f"stage {key} 的执行器与产物不一致"
            if (
                str(artifact.plan_id) != str(plan.plan_id)
                or int(artifact.plan_version) != int(plan.version)
                or str(artifact.plan_content_hash) != plan_hash
                or str(artifact.design_id) != str(plan.design_id)
                or int(artifact.design_version) != int(plan.design_version)
                or str(artifact.design_content_hash) != str(plan.design_content_hash)
                or str(artifact.design_input_content_hash)
                != str(plan.design_input_content_hash)
                or str(artifact.catalog_id) != str(plan.catalog_id)
                or str(artifact.catalog_content_hash) != str(plan.catalog_content_hash)
            ):
                return f"stage {key} 的 design/plan/catalog 身份不一致"
        return None

    def _execute_flow(
        self,
        flow: Any,
        plan: Any,
        artifact_by_stage: dict[tuple[str, str], Any],
        root: Path,
        context: RuntimeContext,
        coordinator_run_id: str,
    ) -> FlowRunResult:
        flow_id = str(flow.flow_id)
        started_at = _now()
        stage_results: list[RunResult] = []
        flow_errors: list[str] = []
        flow_evidence: list[str] = []
        cleanup_step = None
        external_action_started = False

        prepared_cleanup = None
        preflight_errors: dict[str, str] = {}
        for resolution in flow.required_state_resolutions:
            if _text_value(resolution.resolution_kind) != "data_guarantee":
                continue
            required_state_id = str(resolution.required_state_id)
            data_id = str(resolution.data_id)
            if context.data_guarantees.get(required_state_id) != data_id:
                preflight_errors["__data_guarantee__"] = (
                    "DATA_GUARANTEE_MISSING: runtime fixture provider 未确认 "
                    f"{required_state_id} -> {data_id}"
                )
        deferred_stage_ids = {
            str(stage.stage_id)
            for stage in flow.stages
            if self.registry.is_deferred(_text_value(stage.executor_kind))
        }
        has_deferred_stage = bool(deferred_stage_ids)
        if not has_deferred_stage:
            try:
                prepared_cleanup = prepare_flow_cleanup(flow.cleanup, context)
            except RunnerError as exc:
                preflight_errors["__cleanup__"] = f"{exc.code}: {exc.message}"

        for stage in flow.stages:
            stage_id = str(stage.stage_id)
            artifact = artifact_by_stage[(flow_id, stage_id)]
            artifact_dir = self._artifact_dir(root, plan, artifact)
            try:
                executor_kind = _text_value(stage.executor_kind)
                prepare_artifact(
                    artifact_dir,
                    artifact,
                    expected_executor=executor_kind,
                )
                if stage_id not in deferred_stage_ids:
                    self.registry.preflight(
                        executor_kind,
                        artifact_dir,
                        artifact,
                        context,
                    )
            except RunnerError as exc:
                preflight_errors[stage_id] = f"{exc.code}: {exc.message}"
            except Exception as exc:
                preflight_errors[stage_id] = f"PREFLIGHT_ERROR: {exc}"

        if preflight_errors:
            shared = (
                preflight_errors.get("__cleanup__")
                or preflight_errors.get("__data_guarantee__")
            )
            for stage in flow.stages:
                stage_id = str(stage.stage_id)
                message = preflight_errors.get(stage_id) or shared or (
                    "未执行：同一 flow 的其它 stage 预检失败"
                )
                stage_results.append(
                    self._blocked_stage(
                        coordinator_run_id,
                        flow_id,
                        stage,
                        "FLOW_PREFLIGHT_FAILED",
                        message,
                    )
                )
            flow_errors.extend(preflight_errors.values())
            return FlowRunResult(
                flow_id=flow_id,
                status=RunStatus.BLOCKED,
                started_at=started_at,
                finished_at=_now(),
                stages=stage_results,
                errors=flow_errors,
            )

        if has_deferred_stage:
            for stage in flow.stages:
                is_deferred = str(stage.stage_id) in deferred_stage_ids
                stage_results.append(
                    self._blocked_stage(
                        coordinator_run_id,
                        flow_id,
                        stage,
                        "EXECUTOR_DEFERRED" if is_deferred else "FLOW_DEFERRED_DUE_TO_RESERVED_EXECUTOR",
                        (
                            "执行器未配置，当前 stage 未执行"
                            if is_deferred
                            else "同一 flow 包含尚未接入的执行器，本 stage 未执行"
                        ),
                    )
                )
            return FlowRunResult(
                flow_id=flow_id,
                status=RunStatus.BLOCKED,
                started_at=started_at,
                finished_at=_now(),
                stages=stage_results,
                errors=[error for stage in stage_results for error in stage.errors],
            )

        halted = False
        try:
            for stage in flow.stages:
                stage_id = str(stage.stage_id)
                executor_kind = _text_value(stage.executor_kind)
                artifact = artifact_by_stage[(flow_id, stage_id)]
                if halted:
                    stage_results.append(
                        self._blocked_stage(
                            coordinator_run_id,
                            flow_id,
                            stage,
                            "UPSTREAM_STAGE_NOT_PASSED",
                            "前置 stage 未通过，本 stage 未执行",
                        )
                    )
                    continue
                artifact_dir = self._artifact_dir(root, plan, artifact)
                try:
                    result = self.registry.run(
                        executor_kind,
                        artifact_dir,
                        artifact,
                        context,
                    )
                except Exception as exc:  # pragma: no cover - registry-specific failures
                    result = RunResult(
                        run_id=f"run-{uuid4().hex}",
                        executor_kind=executor_kind,
                        status=RunStatus.ERROR,
                        started_at=_now(),
                        finished_at=_now(),
                        errors=[f"RUNNER_EXCEPTION: {exc}"],
                        flow_id=flow_id,
                        stage_id=stage_id,
                    )
                result.flow_id = flow_id
                result.stage_id = stage_id
                result.metadata["coordinator_run_id"] = coordinator_run_id
                result.metadata["stage_order"] = int(stage.order)
                stage_results.append(result)
                external_action_started = external_action_started or bool(
                    result.external_action_started
                )
                if _status_value(result.status) != RunStatus.PASSED.value:
                    halted = True
        finally:
            if external_action_started and prepared_cleanup is not None:
                try:
                    cleanup_step, evidence, errors = run_prepared_flow_cleanup(
                        prepared_cleanup,
                        context,
                        run_id=f"{coordinator_run_id}-{flow_id}",
                    )
                    flow_evidence.extend(evidence)
                    flow_errors.extend(errors)
                except Exception as exc:  # Preserve the run summary if audit storage fails.
                    error = f"CLEANUP_AUDIT_ERROR: {prepared_cleanup.action_ref}: {exc}"
                    flow_errors.append(error)
                    cleanup_step = StepResult(
                        step_id=f"cleanup:{prepared_cleanup.action_ref}",
                        status=RunStatus.FAILED,
                        message="flow cleanup audit failed",
                        details={
                            "cleanup_goal_id": prepared_cleanup.cleanup_goal_id,
                            "handler_kind": prepared_cleanup.handler_kind,
                        },
                    )

        status = _result_status(stage_results)
        if (
            cleanup_step is not None
            and cleanup_step.status != RunStatus.PASSED
            and status != RunStatus.ERROR
        ):
            status = RunStatus.FAILED
        flow_errors.extend(error for result in stage_results for error in result.errors)
        return FlowRunResult(
            flow_id=flow_id,
            status=status,
            started_at=started_at,
            finished_at=_now(),
            stages=stage_results,
            cleanup=cleanup_step,
            evidence=flow_evidence,
            errors=flow_errors,
        )

    @staticmethod
    def _artifact_dir(root: Path, plan: Any, artifact: Any) -> Path:
        from apps.test_platform.planning.artifact_paths import generated_files_root

        categorized = (
            generated_files_root(root, artifact.executor_kind)
            / str(plan.plan_id)
            / f"v{plan.version}"
            / str(artifact.flow_id)
            / str(artifact.stage_id)
        )
        if categorized.is_dir():
            return categorized
        return (
            root
            / str(plan.plan_id)
            / f"v{plan.version}"
            / str(artifact.flow_id)
            / str(artifact.stage_id)
        )

    @staticmethod
    def _blocked_stage(
        run_id: str,
        flow_id: str,
        stage: Any,
        code: str,
        message: str,
    ) -> RunResult:
        now = _now()
        stage_id = str(stage.stage_id)
        return RunResult(
            run_id=run_id,
            executor_kind=_text_value(stage.executor_kind),
            status=RunStatus.BLOCKED,
            started_at=now,
            finished_at=now,
            errors=[f"{code}: {message}"],
            metadata={"not_executed": True, "stage_order": int(stage.order)},
            flow_id=flow_id,
            stage_id=stage_id,
        )

    @staticmethod
    def _blocked_summary(
        run_id: str,
        code: str,
        message: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> ExecutionSummary:
        return ExecutionSummary(
            run_id=run_id,
            status=RunStatus.BLOCKED,
            errors=[f"{code}: {message}"],
            started_at=started_at,
            finished_at=finished_at,
        )

    def _finish_summary(
        self,
        run_id: str,
        plan: Any,
        flows: list[FlowRunResult],
        context: RuntimeContext,
        started_at: str,
        artifacts: list[Any],
        *,
        validation_content_hash: str,
        review_content_hash: str,
        artifact_set_hash: str,
        artifact_root: Path,
        run_history_recorder: Any | None,
    ) -> ExecutionSummary:
        stage_results = [stage for flow in flows for stage in flow.stages]
        flow_as_results = [
            RunResult(
                run_id=run_id,
                executor_kind="flow",
                flow_id=flow.flow_id,
                stage_id="__flow__",
                status=flow.status,
                started_at=flow.started_at,
                finished_at=flow.finished_at,
                errors=list(flow.errors),
            )
            for flow in flows
        ]
        status = _result_status(flow_as_results)
        finished_at = _now()
        manifest = RunManifest(
            schema_version="run-manifest.v4",
            run_id=run_id,
            design_id=str(plan.design_id),
            design_version=int(plan.design_version),
            design_content_hash=str(plan.design_content_hash),
            design_input_content_hash=str(plan.design_input_content_hash),
            plan_id=str(plan.plan_id),
            plan_version=int(plan.version),
            plan_content_hash=plan.content_hash(),
            validation_content_hash=validation_content_hash,
            review_content_hash=review_content_hash,
            artifact_set_hash=artifact_set_hash,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            artifacts=[
                {
                    "flow_id": str(artifact.flow_id),
                    "stage_id": str(artifact.stage_id),
                    "executor_kind": _text_value(artifact.executor_kind),
                    "artifact_refs": [
                        {
                            "kind": str(ref.kind),
                            "path_ref": str(ref.path_ref),
                            "sha256": str(ref.sha256),
                        }
                        for ref in artifact.artifact_refs
                    ],
                }
                for artifact in artifacts
            ],
            flows=[flow.as_dict() for flow in flows],
            stages=[stage.as_dict() for stage in stage_results],
            errors=[error for flow in flows for error in flow.errors],
        )
        try:
            manifest_path = write_evidence(
                context,
                f"{run_id}-run-manifest",
                manifest.as_dict(),
            )
        except Exception as exc:
            error = f"RUN_MANIFEST_WRITE_FAILED: {exc}"
            manifest.errors.append(error)
            status = RunStatus.ERROR
            manifest.status = status
            manifest_path = None
        summary = ExecutionSummary(
            run_id=run_id,
            status=status,
            stages=stage_results,
            flows=flows,
            manifest_path=manifest_path,
            errors=list(manifest.errors),
            started_at=started_at,
            finished_at=finished_at,
        )
        return self._attach_report(
            summary,
            artifact_root=artifact_root,
            context=context,
            plan=plan,
            manifest=manifest,
            manifest_path=manifest_path,
            run_history_recorder=run_history_recorder,
        )

    def _attach_report(
        self,
        summary: ExecutionSummary,
        *,
        artifact_root: Path,
        context: RuntimeContext,
        plan: Any | None = None,
        manifest: RunManifest | None = None,
        manifest_path: str | None = None,
        run_history_recorder: Any | None = None,
    ) -> ExecutionSummary:
        summary.started_at = summary.started_at or _now()
        summary.finished_at = summary.finished_at or _now()
        try:
            paths = self.reporter.generate(
                summary=summary,
                artifact_root=artifact_root,
                context=context,
                plan=plan,
                manifest=manifest,
                manifest_path=manifest_path,
            )
            summary.report_paths = (
                paths.as_dict() if callable(getattr(paths, "as_dict", None)) else dict(paths)
            )
        except Exception as exc:
            summary.errors.append(f"REPORT_WRITE_FAILED: {exc}")
        if run_history_recorder is not None:
            try:
                run_history_recorder.finalize(
                    summary=summary,
                    plan=plan,
                    artifact_root=artifact_root,
                    context=context,
                )
            except Exception as exc:
                summary.errors.append(f"RUN_HISTORY_FINALIZE_FAILED: {exc}")
                try:
                    run_history_recorder.mark_failed(
                        run_id=summary.run_id,
                        error_code="RUN_HISTORY_FINALIZE_FAILED",
                    )
                except Exception:
                    # The recorder may be unavailable entirely; the original
                    # finalize error remains on the returned execution summary.
                    pass
        return summary


__all__ = ["ExecutionCoordinator"]
