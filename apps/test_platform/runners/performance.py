"""注入式性能执行器（不启动外部压测进程）。

第三层只接受第二层生成的 ``performance-execution-plan.v4``。``dry_run`` 只做
结构和配额预检，不调用 driver；``live`` 必须显式注入 driver。driver 返回已经
聚合好的标量指标，真实 k6/Locust/JMeter 适配器留到后续版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping
from uuid import uuid4

from .base import (
    artifact_stage_identity,
    load_json_payload,
    prepare_artifact,
    write_evidence,
)
from .contracts import RunResult, RunStatus, RunnerError, RuntimeContext, StepResult, finish_result


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    operator = operator.lower()
    if operator == "equals":
        return actual == expected
    if operator == "lte":
        return actual <= expected
    if operator == "lt":
        return actual < expected
    if operator == "gte":
        return actual >= expected
    if operator == "gt":
        return actual > expected
    raise RunnerError("THRESHOLD_INVALID", f"不支持的性能阈值操作符: {operator}")


@dataclass(frozen=True)
class _PreparedPerformanceRun:
    """只读预检得到的运行参数；创建它本身不触发任何外部动作。"""

    workspace: Any
    payload: Mapping[str, Any]
    mode: str
    driver_ref: str
    load_profile_ref: str
    sources: list[dict[str, str]]
    stages: list[dict[str, float | int]]
    thresholds: list[dict[str, Any]]
    driver: Any | None


class PerformanceRunner:
    executor_kind = "performance"
    payload_schema = "performance-execution-plan.v4"

    def preflight(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> None:
        """只读校验产物和运行时依赖，不调用 driver、cleanup 或 evidence writer。"""

        self._prepare(artifact_dir, artifact_bundle, context)

    def run(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> RunResult:
        flow_id, stage_id = artifact_stage_identity(artifact_bundle)
        result = RunResult.new(
            run_id=f"run-{uuid4().hex}",
            executor_kind=self.executor_kind,
            flow_id=flow_id,
            stage_id=stage_id,
        )
        driver_started = False
        try:
            prepared = self._prepare(artifact_dir, artifact_bundle, context)
            payload = prepared.payload
            mode = prepared.mode
            driver_ref = prepared.driver_ref
            load_profile_ref = prepared.load_profile_ref
            stages = prepared.stages
            thresholds = prepared.thresholds

            if mode == "dry_run":
                evidence_name = write_evidence(
                    context,
                    f"{result.run_id}-performance-preview",
                    {"mode": "dry_run", "driver_ref": driver_ref, "load_profile_ref": load_profile_ref, "stages": stages, "thresholds": thresholds},
                )
                if evidence_name:
                    result.evidence.append(evidence_name)
                result.steps.append(
                    StepResult(
                        step_id="performance",
                        status=RunStatus.DRY_RUN,
                        message="dry_run 只完成预检，未调用 driver",
                        details={"mode": "dry_run", "threshold_count": len(thresholds)},
                        evidence=[evidence_name] if evidence_name else [],
                    )
                )
                result.status = RunStatus.DRY_RUN
                return result

            driver = prepared.driver
            # Mark the live boundary so failures are classified as execution errors.
            driver_started = True
            started = perf_counter()
            raw = self._invoke_driver(driver, payload, context)
            metrics = raw.get("metrics", raw) if isinstance(raw, Mapping) else None
            if not isinstance(metrics, Mapping):
                raise RunnerError("PERFORMANCE_RESULT_INVALID", "性能 driver 必须返回 metrics 对象")
            threshold_results, inconclusive = self._evaluate_thresholds(metrics, thresholds)
            if inconclusive:
                status = RunStatus.INCONCLUSIVE
                message = "缺少必要性能指标，无法判定"
            else:
                passed = all(item["passed"] for item in threshold_results)
                status = RunStatus.PASSED if passed else RunStatus.FAILED
                message = "all thresholds passed" if passed else "one or more thresholds failed"
            evidence_name = write_evidence(
                context,
                f"{result.run_id}-performance",
                {
                    "mode": "live",
                    "driver_ref": driver_ref,
                    "load_profile_ref": load_profile_ref,
                    "stages": stages,
                    "metrics": dict(metrics),
                    "thresholds": threshold_results,
                },
            )
            if evidence_name:
                result.evidence.append(evidence_name)
            result.steps.append(
                StepResult(
                    step_id="performance",
                    status=status,
                    message=message,
                    duration_ms=(perf_counter() - started) * 1000,
                    details={"mode": "live", "metric_names": sorted(str(key) for key in metrics), "thresholds": threshold_results},
                    evidence=[evidence_name] if evidence_name else [],
                )
            )
            result.status = status
        except RunnerError as exc:
            result.errors.append(f"{exc.code}: {exc.message}")
            # Once the driver boundary has been crossed, RunnerError is an execution failure.
            if driver_started:
                result.status = RunStatus.ERROR
            else:
                result.status = RunStatus.BLOCKED if exc.code in {
                    "ARTIFACT_DIR_MISSING", "ARTIFACT_PATH_INVALID", "MANIFEST_MISSING", "MANIFEST_INVALID",
                    "MANIFEST_IDENTITY_MISMATCH", "MANIFEST_REF_MISSING", "ARTIFACT_REFS_MISSING",
                    "ARTIFACT_MISSING", "ARTIFACT_HASH_INVALID", "ARTIFACT_HASH_MISMATCH", "EXECUTOR_MISMATCH",
                    "ARTIFACT_SCHEMA_INVALID", "RUNTIME_RESOURCE_MISSING",
                    "PERFORMANCE_DRIVER_UNAVAILABLE", "PERFORMANCE_DRIVER_INVALID", "THRESHOLD_INVALID",
                    "STAGE_INVALID",
                } else RunStatus.ERROR
        except Exception as exc:  # pragma: no cover - driver-specific failures
            result.errors.append(f"PERFORMANCE_RUN_ERROR: {exc}")
            result.status = RunStatus.ERROR
        finally:
            result.external_action_started = driver_started
            finish_result(result, result.status)
        return result

    def _prepare(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> _PreparedPerformanceRun:
        """执行 preflight 和 run 共用的纯只读校验。"""

        workspace = prepare_artifact(artifact_dir, artifact_bundle, expected_executor=self.executor_kind)
        payload_path, payload = load_json_payload(workspace, "payload")
        if payload.get("schema_version") != self.payload_schema:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{payload_path.name} 不是 {self.payload_schema}")
        allowed_payload_keys = {
            "schema_version",
            "executor_kind",
            "flow_id",
            "stage_id",
            "design_id",
            "design_version",
            "plan_id",
            "plan_version",
            "driver_ref",
            "load_profile_ref",
            "sources",
            "stages",
            "input_refs",
            "thresholds",
        }
        if set(payload) != allowed_payload_keys:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "performance payload 字段必须与 v4 adapter 精确一致",
            )

        mode = context.performance_mode
        if "mode" in payload:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "performance mode 是第三层运行参数，不能写入计划 artifact",
            )

        driver_ref = str(payload.get("driver_ref") or "").strip()
        if not driver_ref:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "performance.driver_ref 不能为空")
        load_profile_ref = str(payload.get("load_profile_ref") or "").strip()
        if not load_profile_ref or load_profile_ref not in context.performance_profiles:
            raise RunnerError("RUNTIME_RESOURCE_MISSING", f"未注入 load profile: {load_profile_ref}")

        sources = self._validate_sources(payload.get("sources"))
        stages = self._validate_stages(payload.get("stages"), context)
        thresholds = self._validate_thresholds(payload.get("thresholds"))
        driver = None
        if mode == "live":
            if driver_ref not in context.performance_drivers:
                raise RunnerError("PERFORMANCE_DRIVER_UNAVAILABLE", f"未注入性能 driver: {driver_ref}")
            driver = context.performance_drivers[driver_ref]
            driver_runner = getattr(driver, "run", None)
            if not callable(driver_runner) and not callable(driver):
                raise RunnerError("PERFORMANCE_DRIVER_INVALID", "性能 driver 必须可调用或提供 run 方法")

        input_refs = payload.get("input_refs", {})
        if not isinstance(input_refs, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "performance.input_refs 必须是对象")
        runtime_profile = context.performance_profiles.get(load_profile_ref)
        if not isinstance(runtime_profile, Mapping):
            raise RunnerError(
                "RUNTIME_RESOURCE_MISSING",
                f"load profile 运行配置无效: {load_profile_ref}",
            )
        profile_inputs = runtime_profile.get("inputs", {})
        if not isinstance(profile_inputs, Mapping):
            raise RunnerError(
                "RUNTIME_RESOURCE_MISSING",
                f"load profile inputs 无效: {load_profile_ref}",
            )
        inputs: dict[str, Any] = dict(profile_inputs)
        for input_name, variable_ref in input_refs.items():
            if not isinstance(input_name, str) or not isinstance(variable_ref, str):
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", "performance.input_refs 必须映射变量名")
            current: Any = context.variables
            for part in variable_ref.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    raise RunnerError("RUNTIME_RESOURCE_MISSING", f"未注入变量: {variable_ref}")
                current = current[part]
            inputs[input_name] = current
        resolved_payload = {
            "schema_version": self.payload_schema,
            "executor_kind": self.executor_kind,
            "flow_id": payload["flow_id"],
            "stage_id": payload["stage_id"],
            "design_id": payload["design_id"],
            "design_version": payload["design_version"],
            "plan_id": payload["plan_id"],
            "plan_version": payload["plan_version"],
            "driver_ref": driver_ref,
            "load_profile_ref": load_profile_ref,
            "sources": sources,
            "stages": stages,
            "inputs": inputs,
            "thresholds": thresholds,
        }
        return _PreparedPerformanceRun(
            workspace=workspace,
            payload=resolved_payload,
            mode=mode,
            driver_ref=driver_ref,
            load_profile_ref=load_profile_ref,
            sources=sources,
            stages=stages,
            thresholds=thresholds,
            driver=driver,
        )

    @staticmethod
    def _validate_sources(sources: Any) -> list[dict[str, str]]:
        if not isinstance(sources, list) or not sources:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "performance sources 必须是非空数组")
        normalized: list[dict[str, str]] = []
        for source in sources:
            if not isinstance(source, Mapping) or set(source) != {"source_kind", "source_id"}:
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", "performance source 字段无效")
            source_kind = source.get("source_kind")
            source_id = source.get("source_id")
            if source_kind not in {"operation", "expected_result", "required_state"} or not isinstance(
                source_id, str
            ) or not source_id.strip():
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", "performance source 身份无效")
            normalized.append({"source_kind": source_kind, "source_id": source_id.strip()})
        return normalized

    @staticmethod
    def _validate_stages(stages: Any, context: RuntimeContext) -> list[dict[str, float | int]]:
        if not isinstance(stages, list) or not stages:
            raise RunnerError("STAGE_INVALID", "性能计划必须包含非空 stages")
        normalized: list[dict[str, float | int]] = []
        total_duration = 0.0
        for stage in stages:
            if not isinstance(stage, Mapping) or set(stage) != {
                "duration_seconds",
                "virtual_users",
            }:
                raise RunnerError("STAGE_INVALID", "performance stage 必须是对象")
            duration = stage.get("duration_seconds")
            users = stage.get("virtual_users")
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not isinstance(users, int)
                or isinstance(users, bool)
            ):
                raise RunnerError("STAGE_INVALID", "stage duration/users 类型无效")
            duration = float(duration)
            if not isfinite(duration) or duration <= 0 or users <= 0:
                raise RunnerError("STAGE_INVALID", "stage duration/users 必须大于 0")
            if duration > context.max_performance_duration_seconds or users > context.max_virtual_users:
                raise RunnerError("STAGE_INVALID", "stage 超出运行时负载配额")
            total_duration += duration
            normalized.append({"duration_seconds": duration, "virtual_users": users})
        if total_duration > context.max_performance_duration_seconds:
            raise RunnerError("STAGE_INVALID", "性能计划总时长超出运行时配额")
        return normalized

    @staticmethod
    def _validate_thresholds(thresholds: Any) -> list[dict[str, Any]]:
        if not isinstance(thresholds, list):
            raise RunnerError("THRESHOLD_INVALID", "性能计划 thresholds 必须是数组")
        normalized: list[dict[str, Any]] = []
        for threshold in thresholds:
            expected_keys = {
                "threshold_id",
                "expected_result_id",
                "after_operation_id",
                "metric",
                "operator",
                "value",
                "unit",
                "percentile",
            }
            if not isinstance(threshold, Mapping):
                raise RunnerError("THRESHOLD_INVALID", "threshold 必须是对象")
            if set(threshold) != expected_keys:
                raise RunnerError(
                    "THRESHOLD_INVALID",
                    "threshold 字段不完整或包含额外字段; "
                    f"missing={sorted(expected_keys - set(threshold))}, "
                    f"unknown={sorted(set(threshold) - expected_keys)}",
                )
            metric = str(threshold.get("metric") or "").strip()
            operator = str(threshold.get("operator") or "").strip().lower()
            expected_result_id = str(threshold.get("expected_result_id") or "").strip()
            threshold_id = str(threshold.get("threshold_id") or "").strip()
            after_operation_id = str(threshold.get("after_operation_id") or "").strip()
            value = threshold.get("value")
            if (
                not threshold_id
                or not metric
                or not expected_result_id
                or not after_operation_id
                or operator not in {"lte", "lt", "gte", "gt", "equals"}
            ):
                raise RunnerError(
                    "THRESHOLD_INVALID",
                    "threshold 缺少 threshold_id/metric/expected_result_id/after_operation_id "
                    "或 operator 不受支持",
                )
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(float(value))
            ):
                raise RunnerError("THRESHOLD_INVALID", "threshold value 必须是数字")
            normalized.append(
                {
                    "threshold_id": threshold_id,
                    "expected_result_id": expected_result_id,
                    "after_operation_id": after_operation_id,
                    "metric": metric,
                    "operator": operator,
                    "value": value,
                    "unit": threshold.get("unit"),
                    "percentile": threshold.get("percentile"),
                }
            )
        return normalized

    @staticmethod
    def _invoke_driver(driver: Any, payload: Mapping[str, Any], context: RuntimeContext) -> Any:
        runner = getattr(driver, "run", None)
        if callable(runner):
            return runner(payload, context)
        if callable(driver):
            return driver(payload, context)
        raise RunnerError("PERFORMANCE_DRIVER_INVALID", "性能 driver 必须可调用或提供 run 方法")

    @staticmethod
    def _evaluate_thresholds(
        metrics: Mapping[str, Any],
        thresholds: list[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        results: list[dict[str, Any]] = []
        inconclusive = False
        for threshold in thresholds:
            metric = str(threshold["metric"])
            if metric not in metrics:
                results.append(
                    {
                        "threshold_id": threshold["threshold_id"],
                        "expected_result_id": threshold["expected_result_id"],
                        "after_operation_id": threshold["after_operation_id"],
                        "metric": metric,
                        "passed": False,
                        "status": "inconclusive",
                    }
                )
                inconclusive = True
                continue
            raw_metric = metrics[metric]
            percentile = threshold.get("percentile")
            expected_unit = threshold.get("unit")
            actual_unit = raw_metric.get("unit") if isinstance(raw_metric, Mapping) else None
            if percentile:
                if not isinstance(raw_metric, Mapping) or percentile not in raw_metric:
                    results.append(
                        {
                            "threshold_id": threshold["threshold_id"],
                            "expected_result_id": threshold["expected_result_id"],
                            "after_operation_id": threshold["after_operation_id"],
                            "metric": metric,
                            "percentile": percentile,
                            "passed": False,
                            "status": "inconclusive",
                            "reason": "driver 未返回声明的 percentile",
                        }
                    )
                    inconclusive = True
                    continue
                actual = raw_metric[percentile]
            else:
                actual = raw_metric.get("value") if isinstance(raw_metric, Mapping) else raw_metric
            if expected_unit and actual_unit != expected_unit:
                results.append(
                    {
                        "threshold_id": threshold["threshold_id"],
                        "expected_result_id": threshold["expected_result_id"],
                        "metric": metric,
                        "percentile": percentile,
                        "passed": False,
                        "status": "inconclusive",
                        "reason": "driver metric unit 与计划不一致或缺失",
                    }
                )
                inconclusive = True
                continue
            try:
                passed = _compare(actual, str(threshold["operator"]), threshold["value"])
            except (TypeError, ValueError):
                passed = False
            results.append(
                {
                    "threshold_id": threshold["threshold_id"],
                    "expected_result_id": threshold["expected_result_id"],
                    "after_operation_id": threshold["after_operation_id"],
                    "metric": metric,
                    "operator": threshold["operator"],
                    "expected": threshold["value"],
                    "actual": actual,
                    "unit": expected_unit,
                    "percentile": percentile,
                    "passed": bool(passed),
                    "status": "passed" if passed else "failed",
                }
            )
        return results, inconclusive
