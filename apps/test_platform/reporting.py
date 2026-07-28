"""Deterministic JSON, HTML, and JUnit reports for one platform run.

The reporter consumes normalized plan/run data only.  It does not call a model,
re-evaluate assertions, or read raw API/database payloads.  Runner results remain
the source of truth; this module renders those results for people and CI systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any, Mapping
from uuid import uuid4
from xml.etree import ElementTree as ET

from apps.test_platform.runners.base import redact


_KNOWN_STATUSES = (
    "passed",
    "failed",
    "blocked",
    "error",
    "inconclusive",
    "dry_run",
)
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_EXECUTOR_LABELS = {
    "procedure_playwright": "UI 页面测试",
    "http_api": "接口测试",
    "database": "数据库测试",
    "performance": "性能/压力测试",
    "tcp_port": "TCP 端口测试",
}
_STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "blocked": "阻断",
    "error": "错误",
    "inconclusive": "未定",
    "dry_run": "仅预检",
    "queued": "排队中",
    "running": "运行中",
}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _status(value: Any) -> str:
    return str(getattr(value, "value", value) or "inconclusive")


def _executor_label(value: Any) -> str:
    executor = str(value or "unknown")
    return _EXECUTOR_LABELS.get(executor, executor)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    converter = getattr(value, "as_dict", None)
    if callable(converter):
        return dict(converter())
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dict(dumper(mode="json"))
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"report value is not serializable: {type(value).__name__}")


def _safe_component(value: str) -> str:
    normalized = _SAFE_COMPONENT.sub("_", str(value)).strip("._")
    return normalized[:160] or "run"


def _basename(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return PurePosixPath(str(value).replace("\\", "/")).name


def _duration_ms(started_at: Any, finished_at: Any) -> float | None:
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, (finish - start).total_seconds() * 1000)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _status_counts(values: list[str]) -> dict[str, int]:
    result = {status: 0 for status in _KNOWN_STATUSES}
    for value in values:
        result[value] = result.get(value, 0) + 1
    result["total"] = len(values)
    return result


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _evidence_refs(values: list[Any]) -> list[str]:
    """Keep evidence references as filenames, never absolute or traversing paths."""

    return _dedupe(
        [
            name
            for value in values
            if (name := _basename(value)) and name not in {".", ".."}
        ]
    )


def _safe_relative_path(value: Any) -> str | None:
    text = str(value or "").replace("\\", "/").strip()
    if not text or "://" in text or re.match(r"^[A-Za-z]:", text):
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _secret_values(context: Any) -> set[str]:
    variables = _field(context, "variables", {}) or {}
    names = set(_field(context, "secret_variable_names", set()) or set())
    values: set[str] = set()
    for name in names:
        current: Any = variables
        for part in str(name).split("."):
            if not isinstance(current, Mapping) or part not in current:
                current = None
                break
            current = current[part]
        if isinstance(current, (str, int, float)) and str(current):
            values.add(str(current))
    return values


@dataclass(frozen=True)
class ReportPaths:
    """Paths relative to the execution artifact root."""

    root: str
    json: str
    html: str
    junit: str

    def as_dict(self) -> dict[str, str]:
        return {
            "root": self.root,
            "json": self.json,
            "html": self.html,
            "junit": self.junit,
        }


class TestReportGenerator:
    """Build one immutable report view from normalized execution results."""

    schema_version = "test-run-report.v1"

    def generate(
        self,
        *,
        summary: Any,
        artifact_root: str | Path,
        context: Any,
        plan: Any | None = None,
        manifest: Any | None = None,
        manifest_path: str | None = None,
    ) -> ReportPaths:
        root = Path(artifact_root).resolve()
        safe_run_id = _safe_component(str(_field(summary, "run_id", "run")))
        relative_root = PurePosixPath("reports", safe_run_id)
        destination = (root / "reports" / safe_run_id).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise ValueError("report output path escapes artifact root") from exc
        destination.mkdir(parents=True, exist_ok=True)

        paths = ReportPaths(
            root=relative_root.as_posix(),
            json=(relative_root / "report.json").as_posix(),
            html=(relative_root / "report.html").as_posix(),
            junit=(relative_root / "junit.xml").as_posix(),
        )
        payload = self.build_report(
            summary=summary,
            plan=plan,
            manifest=manifest,
            manifest_path=manifest_path,
            outputs=paths.as_dict(),
            artifact_root=root,
        )
        payload = redact(
            payload,
            secret_names=set(_field(context, "secret_variable_names", set()) or set()),
            secret_values=_secret_values(context),
        )
        payload["report_content_hash"] = "sha256:" + hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
        self._validate_payload(payload)

        evidence_prefix = self._evidence_prefix(root, destination, context)
        self._atomic_write(
            destination / "report.json",
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
                default=str,
            )
            + "\n",
        )
        self._atomic_write(
            destination / "report.html",
            self.render_html(payload, evidence_prefix=evidence_prefix),
        )
        self._atomic_write(destination / "junit.xml", self._render_junit(payload))
        return paths

    def build_report(
        self,
        *,
        summary: Any,
        plan: Any | None,
        manifest: Any | None,
        manifest_path: str | None,
        outputs: Mapping[str, str],
        artifact_root: Path | None = None,
    ) -> dict[str, Any]:
        manifest_payload = _as_dict(manifest) if manifest is not None else {}
        plan_flows = {
            str(_field(flow, "flow_id")): flow
            for flow in list(_field(plan, "flows", []) or [])
        }
        normalized_artifacts = self._normalize_artifacts(
            list(manifest_payload.get("artifacts", []) or [])
        )
        artifact_index = {
            (str(item.get("flow_id")), str(item.get("stage_id"))): item
            for item in normalized_artifacts
        }

        report_flows: list[dict[str, Any]] = []
        all_evidence: list[str] = []
        assertion_values: list[bool | None] = []
        stage_statuses: list[str] = []
        for flow_result in list(_field(summary, "flows", []) or []):
            flow_id = str(_field(flow_result, "flow_id", ""))
            planned_flow = plan_flows.get(flow_id)
            stage_plan = {
                str(_field(stage, "stage_id")): stage
                for stage in list(_field(planned_flow, "stages", []) or [])
            }
            stages: list[dict[str, Any]] = []
            for stage_result in list(_field(flow_result, "stages", []) or []):
                stage_id = str(_field(stage_result, "stage_id", ""))
                planned_stage = stage_plan.get(stage_id)
                artifact = artifact_index.get((flow_id, stage_id), {})
                executor_kind = str(
                    _field(stage_result, "executor_kind")
                    or _status(_field(planned_stage, "executor_kind", "unknown"))
                )
                steps = [
                    self._safe_step(step, executor_kind=executor_kind)
                    for step in list(_field(stage_result, "steps", []) or [])
                ]
                for step in steps:
                    details = step.get("details")
                    if not isinstance(details, Mapping):
                        continue
                    for key in ("assertions", "thresholds"):
                        for assertion in list(details.get(key, []) or []):
                            if not isinstance(assertion, Mapping):
                                continue
                            if not str(assertion.get("expected_result_id") or "").strip():
                                continue
                            passed = assertion.get("passed")
                            assertion_values.append(
                                passed if isinstance(passed, bool) else None
                            )
                evidence = _evidence_refs(
                    list(_field(stage_result, "evidence", []) or [])
                    + [
                        item
                        for step in steps
                        for item in list(step.get("evidence", []) or [])
                    ]
                )
                all_evidence.extend(evidence)
                stage_status = _status(_field(stage_result, "status"))
                stage_statuses.append(stage_status)
                stages.append(
                    {
                        "stage_id": stage_id,
                        "order": _field(planned_stage, "order"),
                        "executor_kind": executor_kind,
                        "status": stage_status,
                        "started_at": _field(stage_result, "started_at"),
                        "finished_at": _field(stage_result, "finished_at"),
                        "duration_ms": _duration_ms(
                            _field(stage_result, "started_at"),
                            _field(stage_result, "finished_at"),
                        ),
                        "operation_ids": list(
                            _field(planned_stage, "operation_ids", []) or []
                        ),
                        "expected_result_ids": list(
                            _field(planned_stage, "expected_result_ids", []) or []
                        ),
                        "setup_required_state_ids": list(
                            _field(planned_stage, "setup_required_state_ids", [])
                            or []
                        ),
                        "data_ids": list(_field(planned_stage, "data_ids", []) or []),
                        "steps": steps,
                        "evidence": evidence,
                        "errors": list(_field(stage_result, "errors", []) or []),
                        "metadata": {
                            key: value
                            for key, value in dict(
                                _field(stage_result, "metadata", {}) or {}
                            ).items()
                            if key
                            in {
                                "coordinator_run_id",
                                "stage_order",
                                "not_executed",
                                "final_url",
                                "message",
                                "planned_rows",
                            }
                        },
                        "external_action_started": bool(
                            _field(stage_result, "external_action_started", False)
                        ),
                        "artifacts": self._artifact_refs(
                            artifact,
                            plan=plan,
                            flow_id=flow_id,
                            stage_id=stage_id,
                            artifact_root=artifact_root,
                        ),
                    }
                )
            cleanup_value = _field(flow_result, "cleanup")
            cleanup = (
                self._safe_step(cleanup_value, executor_kind="cleanup")
                if cleanup_value is not None
                else None
            )
            if cleanup is not None:
                all_evidence.extend(list(cleanup.get("evidence", []) or []))
            flow_evidence = _evidence_refs(
                list(_field(flow_result, "evidence", []) or [])
                + [item for stage in stages for item in stage["evidence"]]
                + (list(cleanup.get("evidence", []) or []) if cleanup else [])
            )
            all_evidence.extend(flow_evidence)
            report_flows.append(
                {
                    "flow_id": flow_id,
                    "scenario_id": str(_field(planned_flow, "scenario_id", "")),
                    "name": str(_field(planned_flow, "name", flow_id)),
                    "requirement_ids": list(
                        _field(planned_flow, "requirement_ids", []) or []
                    ),
                    "techniques": [
                        _status(item)
                        for item in list(_field(planned_flow, "techniques", []) or [])
                    ],
                    "status": _status(_field(flow_result, "status")),
                    "started_at": _field(flow_result, "started_at"),
                    "finished_at": _field(flow_result, "finished_at"),
                    "duration_ms": _duration_ms(
                        _field(flow_result, "started_at"),
                        _field(flow_result, "finished_at"),
                    ),
                    "stages": stages,
                    "cleanup": cleanup,
                    "evidence": flow_evidence,
                    "errors": list(_field(flow_result, "errors", []) or []),
                }
            )

        flow_statuses = [str(flow["status"]) for flow in report_flows]
        planned_assertions = sum(
            len(stage["expected_result_ids"])
            for flow in report_flows
            for stage in flow["stages"]
        )
        assertions = {
            "planned": planned_assertions,
            "evaluated": len(assertion_values),
            "passed": sum(value is True for value in assertion_values),
            "failed": sum(value is False for value in assertion_values),
            "inconclusive": sum(value is None for value in assertion_values),
            "not_evaluated": max(0, planned_assertions - len(assertion_values)),
        }
        started_at = manifest_payload.get("started_at") or self._earliest(
            [flow.get("started_at") for flow in report_flows]
        ) or _field(summary, "started_at")
        finished_at = manifest_payload.get("finished_at") or self._latest(
            [flow.get("finished_at") for flow in report_flows]
        ) or _field(summary, "finished_at")
        limitations: list[str] = []
        blocked_procedure_stages = [
            stage
            for flow in report_flows
            for stage in flow["stages"]
            if stage["executor_kind"] == "procedure_playwright"
            and stage["status"] == "blocked"
        ]
        if blocked_procedure_stages:
            missing_asset_database = any(
                any(
                    "UI_ASSET_DATABASE_MISSING" in str(error)
                    for error in stage.get("errors", [])
                )
                for stage in blocked_procedure_stages
            )
            limitations.append(
                "未选择沉淀资产库，UI 测试未执行。"
                if missing_asset_database
                else "UI 测试在执行前被阻断；请查看阶段错误。"
            )
        if any(stage["status"] == "dry_run" for flow in report_flows for stage in flow["stages"]):
            limitations.append("dry_run 只完成预检，不代表外部测试动作已执行。")
        if not manifest_payload:
            limitations.append("运行在 approved plan handoff 前被阻断，没有生成 run-manifest。")

        run_id = str(_field(summary, "run_id", ""))
        payload = {
            "schema_version": self.schema_version,
            "report_id": f"report-{run_id}",
            "run_id": run_id,
            "status": _status(_field(summary, "status")),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": _duration_ms(started_at, finished_at),
            "target": {
                "system_id": _field(plan, "target_system_id"),
                "environment": _field(plan, "target_environment"),
            },
            "identity": {
                "design_id": manifest_payload.get("design_id")
                or _field(plan, "design_id"),
                "design_version": manifest_payload.get("design_version")
                or _field(plan, "design_version"),
                "design_content_hash": manifest_payload.get("design_content_hash")
                or _field(plan, "design_content_hash"),
                "design_input_content_hash": manifest_payload.get(
                    "design_input_content_hash"
                )
                or _field(plan, "design_input_content_hash"),
                "plan_id": manifest_payload.get("plan_id") or _field(plan, "plan_id"),
                "plan_version": manifest_payload.get("plan_version")
                or _field(plan, "version"),
                "plan_content_hash": manifest_payload.get("plan_content_hash"),
                "validation_content_hash": manifest_payload.get(
                    "validation_content_hash"
                ),
                "review_content_hash": manifest_payload.get("review_content_hash"),
                "artifact_set_hash": manifest_payload.get("artifact_set_hash"),
                "catalog_id": _field(plan, "catalog_id"),
                "catalog_content_hash": _field(plan, "catalog_content_hash"),
            },
            "summary": {
                "flows": _status_counts(flow_statuses),
                "stages": _status_counts(stage_statuses),
                "assertions": assertions,
            },
            "flows": report_flows,
            "artifacts": normalized_artifacts,
            "evidence": _evidence_refs(all_evidence),
            "manifest_path": _basename(manifest_path),
            "outputs": dict(outputs),
            "limitations": limitations,
            "errors": list(_field(summary, "errors", []) or []),
        }
        return payload

    @staticmethod
    def _safe_step(value: Any, *, executor_kind: str) -> dict[str, Any]:
        raw = _as_dict(value)
        details = raw.get("details")
        safe_details: dict[str, Any] = {}
        allowed_detail_keys = {
            "assertions",
            "thresholds",
            "status_code",
            "row_count",
            "truncated",
            "mode",
            "metric_names",
            "threshold_count",
            "probe_ref",
            "host_ref",
            "port",
            "observed_state",
            "connect_latency_ms",
            "cleanup_goal_id",
            "handler_kind",
            "parameter_slots",
            "type",
            "action",
            "pageUrl",
            "taskCompleted",
            "timestamp",
        }
        if isinstance(details, Mapping):
            for key in allowed_detail_keys:
                if key not in details:
                    continue
                if key in {"assertions", "thresholds"}:
                    safe_details[key] = TestReportGenerator._safe_assertions(
                        details[key]
                    )
                elif key == "metric_names" and isinstance(details[key], list):
                    safe_details[key] = [str(item) for item in details[key]]
                elif key == "parameter_slots" and isinstance(details[key], list):
                    safe_details[key] = [str(item) for item in details[key]]
                else:
                    safe_details[key] = details[key]
        return {
            "step_id": str(raw.get("step_id") or ""),
            "executor_kind": executor_kind,
            "status": _status(raw.get("status")),
            "message": str(raw.get("message") or ""),
            "duration_ms": raw.get("duration_ms"),
            "details": safe_details,
            "evidence": _evidence_refs(list(raw.get("evidence", []) or [])),
        }

    @staticmethod
    def _safe_assertions(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        allowed = {
            "assertion_id",
            "threshold_id",
            "expected_result_id",
            "after_operation_id",
            "kind",
            "metric",
            "operator",
            "expected",
            "actual",
            "unit",
            "percentile",
            "passed",
            "status",
            "message",
            "reason",
        }
        return [
            {str(key): item[key] for key in allowed if key in item}
            for item in value
            if isinstance(item, Mapping)
        ]

    @staticmethod
    def _normalize_artifacts(values: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, Mapping):
                continue
            refs = []
            for ref in list(value.get("artifact_refs", []) or []):
                if not isinstance(ref, Mapping):
                    continue
                path_ref = _safe_relative_path(ref.get("path_ref"))
                if path_ref is None:
                    continue
                sha256 = str(ref.get("sha256") or "")
                refs.append(
                    {
                        "kind": str(ref.get("kind") or "artifact"),
                        "path_ref": path_ref,
                        "sha256": sha256
                        if re.fullmatch(r"sha256:[0-9a-f]{64}", sha256)
                        else None,
                    }
                )
            result.append(
                {
                    "flow_id": str(value.get("flow_id") or ""),
                    "stage_id": str(value.get("stage_id") or ""),
                    "executor_kind": str(value.get("executor_kind") or ""),
                    "artifact_refs": refs,
                }
            )
        return result

    @staticmethod
    def _artifact_refs(
        artifact: Mapping[str, Any],
        *,
        plan: Any,
        flow_id: str,
        stage_id: str,
        artifact_root: Path | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for value in list(artifact.get("artifact_refs", []) or []):
            if not isinstance(value, Mapping):
                continue
            path_ref = str(value.get("path_ref") or "")
            item = {
                "kind": str(value.get("kind") or "artifact"),
                "path_ref": path_ref,
                "sha256": value.get("sha256"),
            }
            plan_id = _field(plan, "plan_id")
            version = _field(plan, "version")
            if plan_id and version and path_ref:
                from .planning.artifact_paths import generated_files_path

                categorized_ref = PurePosixPath(
                    generated_files_path(artifact.get("executor_kind")),
                    str(plan_id),
                    f"v{version}",
                    flow_id,
                    stage_id,
                    path_ref,
                )
                legacy_ref = PurePosixPath(
                    str(plan_id),
                    f"v{version}",
                    flow_id,
                    stage_id,
                    path_ref,
                )
                selected_ref = categorized_ref
                if artifact_root is not None:
                    root = Path(artifact_root).resolve()
                    if not (root / Path(*categorized_ref.parts)).is_file() and (
                        root / Path(*legacy_ref.parts)
                    ).is_file():
                        selected_ref = legacy_ref
                item["artifact_path_ref"] = selected_ref.as_posix()
            result.append(item)
        return result

    @staticmethod
    def _earliest(values: list[Any]) -> Any:
        candidates = sorted(str(value) for value in values if value)
        return candidates[0] if candidates else None

    @staticmethod
    def _latest(values: list[Any]) -> Any:
        candidates = sorted(str(value) for value in values if value)
        return candidates[-1] if candidates else None

    @staticmethod
    def _validate_payload(payload: Mapping[str, Any]) -> None:
        required = {
            "schema_version",
            "report_id",
            "run_id",
            "status",
            "generated_at",
            "summary",
            "flows",
            "outputs",
            "report_content_hash",
        }
        missing = required - set(payload)
        if missing or payload.get("schema_version") != "test-run-report.v1":
            raise ValueError(f"invalid report payload; missing={sorted(missing)}")
        content_hash = str(payload.get("report_content_hash") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash):
            raise ValueError("report_content_hash is invalid")
        unhashed = dict(payload)
        unhashed.pop("report_content_hash", None)
        expected = "sha256:" + hashlib.sha256(
            _canonical_json(unhashed).encode("utf-8")
        ).hexdigest()
        if content_hash != expected:
            raise ValueError("report_content_hash does not match report content")

    @staticmethod
    def _evidence_prefix(root: Path, report_dir: Path, context: Any) -> str | None:
        evidence_dir = _field(context, "evidence_dir")
        if evidence_dir is None:
            return None
        evidence = Path(evidence_dir).resolve()
        try:
            evidence_relative = evidence.relative_to(root)
            report_relative = report_dir.resolve().relative_to(root)
        except ValueError:
            return None
        return posixpath.relpath(
            evidence_relative.as_posix(),
            report_relative.as_posix(),
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def render_html(
        self,
        payload: Mapping[str, Any],
        *,
        evidence_prefix: str | None,
    ) -> str:
        summary = payload["summary"]
        flow_counts = summary["flows"]
        stage_counts = summary["stages"]
        assertion_counts = summary["assertions"]
        flow_rows = []
        detail_sections = []
        for flow in payload["flows"]:
            requirements = ", ".join(flow["requirement_ids"]) or "-"
            executors = ", ".join(
                dict.fromkeys(_executor_label(stage["executor_kind"]) for stage in flow["stages"])
            )
            flow_rows.append(
                "<tr>"
                f"<td><a href=\"#flow-{escape(flow['flow_id'], quote=True)}\">{escape(flow['name'])}</a></td>"
                f"<td>{escape(requirements)}</td>"
                f"<td>{escape(executors)}</td>"
                f"<td>{self._status_badge(flow['status'])}</td>"
                f"<td>{self._format_duration(flow.get('duration_ms'))}</td>"
                "</tr>"
            )
            stage_rows = []
            for stage in flow["stages"]:
                evidence = self._render_evidence(stage["evidence"], evidence_prefix)
                artifacts = self._render_artifacts(stage["artifacts"])
                errors = "<br>".join(escape(str(item)) for item in stage["errors"]) or "-"
                agent_summary = self._render_agent_summary(stage)
                display_steps = []
                for value in stage["steps"]:
                    display_value = dict(value)
                    display_value.pop("executor_kind", None)
                    display_steps.append(display_value)
                details = escape(
                    json.dumps(display_steps, ensure_ascii=False, indent=2, default=str)
                )
                stage_rows.append(
                    "<tr>"
                    f"<td>{escape(stage['stage_id'])}</td>"
                    f"<td>{escape(_executor_label(stage['executor_kind']))}</td>"
                    f"<td>{self._status_badge(stage['status'])}</td>"
                    f"<td>{self._format_duration(stage.get('duration_ms'))}</td>"
                    f"<td>{evidence}</td>"
                    f"<td>{artifacts}</td>"
                    f"<td>{errors}{agent_summary}<details><summary>实际步骤</summary><pre>{details}</pre></details></td>"
                    "</tr>"
                )
            cleanup = flow.get("cleanup")
            cleanup_html = (
                "<p><strong>Cleanup:</strong> "
                + self._status_badge(cleanup.get("status"))
                + " "
                + escape(str(cleanup.get("message") or ""))
                + "</p>"
                if cleanup
                else ""
            )
            flow_errors = "<br>".join(escape(str(item)) for item in flow["errors"])
            detail_sections.append(
                f"<section id=\"flow-{escape(flow['flow_id'], quote=True)}\">"
                f"<h2>{escape(flow['name'])}</h2>"
                f"<p><code>{escape(flow['flow_id'])}</code> · {self._status_badge(flow['status'])}</p>"
                f"<p><strong>需求:</strong> {escape(requirements)}</p>"
                f"{cleanup_html}"
                + (f"<p class=\"error-text\">{flow_errors}</p>" if flow_errors else "")
                + "<div class=\"table-wrap\"><table><thead><tr>"
                "<th>Stage</th><th>执行器</th><th>状态</th><th>耗时</th>"
                "<th>证据</th><th>产物</th><th>错误与步骤</th>"
                "</tr></thead><tbody>"
                + "".join(stage_rows)
                + "</tbody></table></div></section>"
            )

        limitations = "".join(
            f"<li>{escape(str(item))}</li>" for item in payload.get("limitations", [])
        )
        errors = "".join(
            f"<li>{escape(str(item))}</li>" for item in payload.get("errors", [])
        )
        notices = ""
        if limitations:
            notices += f"<section><h2>限制与未执行说明</h2><ul>{limitations}</ul></section>"
        if errors:
            notices += f"<section><h2>运行错误</h2><ul>{errors}</ul></section>"
        target = payload["target"]
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>智能测试平台测试报告 - {escape(str(payload['run_id']))}</title>
  <style>
    :root {{ color-scheme: light; --ink:#17202a; --muted:#5f6b76; --line:#d9dee3; --band:#f5f7f8; --pass:#176b3a; --fail:#a12622; --block:#8a5a00; --error:#7a1f37; --neutral:#56616c; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:#fff; font:14px/1.5 system-ui,Segoe UI,sans-serif; letter-spacing:0; }}
    header {{ padding:28px max(24px,calc((100% - 1180px)/2)); border-bottom:1px solid var(--line); background:var(--band); }}
    main {{ max-width:1180px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 8px; font-size:26px; letter-spacing:0; }} h2 {{ margin:0 0 10px; font-size:18px; letter-spacing:0; }}
    .muted {{ color:var(--muted); }}
    .summary {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:0; border:1px solid var(--line); margin:20px 0 28px; }}
    .summary div {{ padding:14px; border-right:1px solid var(--line); }} .summary div:last-child {{ border-right:0; }}
    .summary dt {{ color:var(--muted); }} .summary dd {{ margin:3px 0 0; font-size:20px; font-weight:650; }}
    section {{ padding:22px 0; border-top:1px solid var(--line); }}
    .table-wrap {{ overflow:auto; }} table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:9px 10px; border:1px solid var(--line); text-align:left; vertical-align:top; }} th {{ background:var(--band); white-space:nowrap; }}
    .status {{ display:inline-block; padding:1px 6px; border:1px solid currentColor; border-radius:3px; font-weight:650; white-space:nowrap; }}
    .passed {{ color:var(--pass); }} .failed {{ color:var(--fail); }} .blocked {{ color:var(--block); }} .error {{ color:var(--error); }} .inconclusive,.dry_run {{ color:var(--neutral); }}
    .error-text {{ color:var(--fail); }} code,pre {{ font-family:Consolas,monospace; }} pre {{ max-height:300px; overflow:auto; white-space:pre-wrap; word-break:break-word; background:var(--band); padding:10px; border:1px solid var(--line); }}
    details summary {{ cursor:pointer; color:#245a86; }} a {{ color:#1d5f91; }}
  </style>
</head>
<body>
  <header>
    <h1>智能测试平台测试报告</h1>
    <div>{self._status_badge(payload['status'])} <code>{escape(str(payload['run_id']))}</code></div>
    <div class="muted">系统 {escape(str(target.get('system_id') or '-'))} · 环境 {escape(str(target.get('environment') or '-'))} · 生成于 {escape(str(payload['generated_at']))}</div>
  </header>
  <main>
    <dl class="summary">
      <div><dt>Flow</dt><dd>{flow_counts['total']}</dd></div>
      <div><dt>通过</dt><dd class="passed">{flow_counts.get('passed', 0)}</dd></div>
      <div><dt>失败/错误</dt><dd class="failed">{flow_counts.get('failed', 0) + flow_counts.get('error', 0)}</dd></div>
      <div><dt>阻断</dt><dd class="blocked">{flow_counts.get('blocked', 0)}</dd></div>
      <div><dt>Stage</dt><dd>{stage_counts['total']}</dd></div>
      <div><dt>断言</dt><dd>{assertion_counts['passed']}/{assertion_counts['planned']}</dd></div>
    </dl>
    <section>
      <h2>结果概览</h2>
      <div class="table-wrap"><table><thead><tr><th>测试场景</th><th>关联需求</th><th>测试类型</th><th>结果</th><th>耗时</th></tr></thead><tbody>{''.join(flow_rows) or '<tr><td colspan="5">没有可执行场景</td></tr>'}</tbody></table></div>
    </section>
    {''.join(detail_sections)}
    {notices}
  </main>
</body>
</html>
"""

    @staticmethod
    def _render_agent_summary(stage: Mapping[str, Any]) -> str:
        if stage.get("executor_kind") != "stagehand_agent":
            return ""
        metadata = stage.get("metadata")
        if not isinstance(metadata, Mapping):
            return ""
        items: list[str] = []
        planned = metadata.get("planned_rows")
        if isinstance(planned, list):
            for row in planned:
                if not isinstance(row, Mapping):
                    continue
                checks = row.get("checks")
                check_text = (
                    "；".join(
                        escape(str(value))
                        for value in checks
                        if str(value).strip()
                    )
                    if isinstance(checks, list)
                    else ""
                )
                items.append(
                    "<li><strong>Action:</strong> "
                    f"{escape(str(row.get('action') or '-'))}<br>"
                    f"<strong>Check:</strong> {check_text or '-'}</li>"
                )
        final_url = escape(str(metadata.get("final_url") or "-"))
        message = escape(str(metadata.get("message") or "-"))
        planned_html = "".join(items) or "<li>-</li>"
        return (
            f"<p><strong>最终页面:</strong> {final_url}<br>"
            f"<strong>执行消息:</strong> {message}</p>"
            "<details open><summary>计划 Action / Check</summary>"
            f"<ol>{planned_html}</ol></details>"
        )

    @staticmethod
    def _status_badge(status: Any) -> str:
        value = _status(status)
        label = _STATUS_LABELS.get(value, value)
        return f'<span class="status {escape(value, quote=True)}">{escape(label)}</span>'

    @staticmethod
    def _format_duration(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return "-"
        return f"{float(value):.1f} ms"

    @staticmethod
    def _render_evidence(values: list[str], prefix: str | None) -> str:
        if not values:
            return "-"
        rendered = []
        for value in values:
            name = _basename(value) or "evidence"
            if name in {".", ".."}:
                continue
            if prefix is None:
                rendered.append(f"<code>{escape(name)}</code>")
            else:
                href = PurePosixPath(prefix, name).as_posix()
                rendered.append(
                    f'<a href="{escape(href, quote=True)}">{escape(name)}</a>'
                )
        return "<br>".join(rendered)

    @staticmethod
    def _render_artifacts(values: list[Mapping[str, Any]]) -> str:
        if not values:
            return "-"
        rendered = []
        for item in values:
            label = f"{item.get('kind')}: {item.get('path_ref')}"
            path_ref = item.get("artifact_path_ref")
            if path_ref:
                href = PurePosixPath("..", "artifact", str(path_ref)).as_posix() + "/"
                rendered.append(
                    f'<a href="{escape(href, quote=True)}">{escape(label)}</a>'
                )
            else:
                rendered.append(f"<code>{escape(label)}</code>")
        return "<br>".join(rendered)

    def _render_junit(self, payload: Mapping[str, Any]) -> str:
        flows = list(payload.get("flows", []) or [])
        cases = flows or [
            {
                "flow_id": "__run__",
                "scenario_id": "__run__",
                "name": "智能测试平台运行交接",
                "status": payload.get("status"),
                "duration_ms": payload.get("duration_ms"),
                "stages": [],
                "evidence": [],
                "errors": payload.get("errors", []),
            }
        ]
        statuses = [_status(item.get("status")) for item in cases]
        failures = sum(value == "failed" for value in statuses)
        errors = sum(value == "error" for value in statuses)
        skipped = sum(value in {"blocked", "inconclusive", "dry_run"} for value in statuses)
        duration = payload.get("duration_ms")
        suite = ET.Element(
            "testsuite",
            {
                "name": f"智能测试平台 {payload['run_id']}",
                "tests": str(len(cases)),
                "failures": str(failures),
                "errors": str(errors),
                "skipped": str(skipped),
                "time": f"{float(duration or 0) / 1000:.6f}",
                "timestamp": str(payload.get("started_at") or payload.get("generated_at")),
            },
        )
        properties = ET.SubElement(suite, "properties")
        for name, value in (
            ("run_id", payload.get("run_id")),
            ("design_id", payload.get("identity", {}).get("design_id")),
            ("plan_id", payload.get("identity", {}).get("plan_id")),
            ("system_id", payload.get("target", {}).get("system_id")),
            ("environment", payload.get("target", {}).get("environment")),
        ):
            ET.SubElement(properties, "property", {"name": name, "value": str(value or "")})

        for flow in cases:
            status = _status(flow.get("status"))
            case = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": f"test_conductor.{flow.get('scenario_id') or 'run'}",
                    "name": str(flow.get("name") or flow.get("flow_id")),
                    "time": f"{float(flow.get('duration_ms') or 0) / 1000:.6f}",
                },
            )
            message = "\n".join(str(item) for item in flow.get("errors", []) or [])
            if status == "failed":
                failure = ET.SubElement(
                    case,
                    "failure",
                    {"type": "assertion", "message": message or "flow failed"},
                )
                failure.text = message or "One or more assertions failed."
            elif status == "error":
                error = ET.SubElement(
                    case,
                    "error",
                    {"type": "execution", "message": message or "flow error"},
                )
                error.text = message or "The flow ended with an execution error."
            elif status in {"blocked", "inconclusive", "dry_run"}:
                ET.SubElement(
                    case,
                    "skipped",
                    {"message": message or f"flow status is {status}"},
                )
            output = ET.SubElement(case, "system-out")
            output.text = json.dumps(
                {
                    "flow_id": flow.get("flow_id"),
                    "status": status,
                    "stages": [
                        {
                            "stage_id": stage.get("stage_id"),
                            "executor_kind": stage.get("executor_kind"),
                            "status": stage.get("status"),
                            "evidence": stage.get("evidence", []),
                        }
                        for stage in flow.get("stages", [])
                    ],
                    "evidence": flow.get("evidence", []),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        ET.indent(suite, space="  ")
        return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


__all__ = ["ReportPaths", "TestReportGenerator"]
