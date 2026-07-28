"""Run an approved Agent UI artifact through the local Stagehand sidecar."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping
from uuid import uuid4

from .base import (
    ExecutorRunner,
    artifact_stage_identity,
    load_json_payload,
    prepare_artifact,
)
from .contracts import (
    RunResult,
    RunStatus,
    RunnerError,
    RuntimeContext,
    StepResult,
    finish_result,
)


_RESULT_PREFIX = "__TEST_CONDUCTOR_RESULT__"


class AgentUiRunner(ExecutorRunner):
    executor_kind = "stagehand_agent"
    payload_schema = "agent-ui-execution-plan.v1"

    def __init__(
        self, invoke: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None
    ):
        self._invoke = invoke or self._invoke_sidecar

    def _prepare_plan(self, artifact_dir: Path, artifact_bundle: Any):
        workspace = prepare_artifact(
            artifact_dir, artifact_bundle, expected_executor=self.executor_kind
        )
        payload_path, payload = load_json_payload(workspace, "payload")
        allowed = {
            "schema_version",
            "executor_kind",
            "flow_id",
            "stage_id",
            "design_id",
            "design_version",
            "plan_id",
            "plan_version",
            "start_url",
            "max_steps",
            "rows",
        }
        if set(payload) != allowed:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID", "Agent UI payload 字段不完整或包含未知字段"
            )
        if payload.get("schema_version") != self.payload_schema:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID", f"{payload_path.name} schema 无效"
            )
        start_url = payload.get("start_url")
        if not isinstance(start_url, str) or not start_url.startswith(
            ("http://", "https://")
        ):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "Agent UI start_url 无效")
        max_steps = payload.get("max_steps")
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or not 1 <= max_steps <= 200
        ):
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID", "Agent UI max_steps 超出 1-200"
            )
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "Agent UI rows 必须是非空数组")
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or set(row) != {
                "row_id",
                "source",
                "operation_ref",
                "action",
                "checks",
            }:
                raise RunnerError(
                    "ARTIFACT_SCHEMA_INVALID", f"Agent UI rows[{index}] 无效"
                )
            if not all(
                isinstance(row.get(key), str) and row[key].strip()
                for key in ("row_id", "operation_ref", "action")
            ):
                raise RunnerError(
                    "ARTIFACT_SCHEMA_INVALID", f"Agent UI rows[{index}] 缺少动作信息"
                )
            if not isinstance(row.get("checks"), list):
                raise RunnerError(
                    "ARTIFACT_SCHEMA_INVALID",
                    f"Agent UI rows[{index}].checks 必须是数组",
                )
        return payload

    def preflight(
        self, artifact_dir: Path, artifact_bundle: Any, context: RuntimeContext
    ) -> None:
        self._prepare_plan(artifact_dir, artifact_bundle)

    def run(
        self, artifact_dir: Path, artifact_bundle: Any, context: RuntimeContext
    ) -> RunResult:
        flow_id, stage_id = artifact_stage_identity(artifact_bundle)
        result = RunResult.new(
            run_id=f"run-{uuid4().hex}",
            executor_kind=self.executor_kind,
            flow_id=flow_id,
            stage_id=stage_id,
        )
        try:
            payload = dict(self._prepare_plan(artifact_dir, artifact_bundle))
            evidence_dir = Path(context.evidence_dir or artifact_dir).resolve()
            evidence_dir.mkdir(parents=True, exist_ok=True)
            request = {
                "start_url": payload["start_url"],
                "max_steps": payload["max_steps"],
                "rows": payload["rows"],
                "variables": {
                    name: value
                    for name, value in context.variables.items()
                    if isinstance(name, str)
                    and isinstance(value, (str, int, float, bool))
                },
                "evidence_dir": str(evidence_dir),
                "headless": context.ui_browser_headless,
            }
            outcome = dict(self._invoke(request))
            actual_steps = (
                outcome.get("actions")
                if isinstance(outcome.get("actions"), list)
                else []
            )
            for index, step in enumerate(actual_steps, start=1):
                details = (
                    dict(step) if isinstance(step, Mapping) else {"action": str(step)}
                )
                result.steps.append(
                    StepResult(
                        step_id=f"AGENT-ACTUAL-{index:04d}",
                        status=RunStatus.PASSED,
                        message=str(
                            details.get("action") or details.get("type") or "Agent step"
                        ),
                        details=details,
                    )
                )
            evidence = [
                str(item)
                for item in outcome.get("evidence", [])
                if isinstance(item, str)
            ]
            result.evidence.extend(evidence)
            success = (
                outcome.get("success") is True and outcome.get("completed") is True
            )
            result.metadata = {
                "final_url": str(outcome.get("final_url") or ""),
                "message": str(outcome.get("message") or ""),
                "planned_rows": [
                    {
                        "row_id": str(row["row_id"]),
                        "action": str(row["action"]),
                        "checks": [
                            str(check.get("statement") or "")
                            for check in row["checks"]
                            if isinstance(check, Mapping)
                            and str(check.get("statement") or "").strip()
                        ],
                    }
                    for row in payload["rows"]
                ],
            }
            result.external_action_started = True
            if not success:
                result.errors.append(
                    str(outcome.get("message") or "Agent 未完成已审批任务")
                )
            return finish_result(
                result, RunStatus.PASSED if success else RunStatus.FAILED
            )
        except RunnerError:
            raise
        except Exception as exc:
            result.errors.append(f"Agent UI 执行失败: {exc}")
            return finish_result(result, RunStatus.ERROR)

    @staticmethod
    def _invoke_sidecar(request: dict[str, Any]) -> Mapping[str, Any]:
        script = (
            Path(__file__).resolve().parents[3] / "scripts" / "stagehand_executor.mjs"
        )
        if not script.is_file():
            raise RunnerError("RUNTIME_RESOURCE_MISSING", "Stagehand sidecar 不存在")
        completed = subprocess.run(
            ["node", str(script)],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=1800,
            check=False,
        )
        line = next(
            (
                item[len(_RESULT_PREFIX) :]
                for item in reversed(completed.stdout.splitlines())
                if item.startswith(_RESULT_PREFIX)
            ),
            None,
        )
        if line is None:
            detail = (
                completed.stderr.strip().splitlines()[-1]
                if completed.stderr.strip()
                else "没有返回结构化结果"
            )
            raise RuntimeError(detail)
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise RuntimeError("Stagehand 返回结果不是对象")
        return value


__all__ = ["AgentUiRunner"]
