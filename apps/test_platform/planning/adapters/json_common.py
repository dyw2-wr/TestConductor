"""JSON executor artifact 的 v4 公共写入逻辑。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import (
    AgentUiExecution,
    ProcedureExecution,
    DatabaseExecution,
    ExecutorArtifactBundle,
    ExecutorArtifactRef,
    HttpExecution,
    PerformanceExecution,
    PortExecution,
    PlanFlow,
    PlanStage,
    TestPlanDraft,
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _execution_steps(execution):
    if isinstance(execution, AgentUiExecution):
        return execution.rows
    if isinstance(execution, ProcedureExecution):
        return execution.rows
    if isinstance(execution, HttpExecution):
        return execution.requests
    if isinstance(execution, DatabaseExecution):
        return execution.operations
    if isinstance(execution, PortExecution):
        return execution.probes
    return []


def _assertion_trace(assertion: Any) -> dict[str, Any]:
    """Keep one audit shape without changing executor-specific payloads."""

    return {
        "expected_result_id": assertion.expected_result_id,
        "after_operation_id": assertion.after_operation_id,
        "observable_ref": assertion.observable_ref,
        "kind": assertion.kind,
        "statement": assertion.statement,
        "operator": assertion.operator,
        "expected": assertion.expected,
        "unit": assertion.unit,
        "path": assertion.path,
        "name": assertion.name,
        "column": assertion.column,
        "metric": assertion.metric,
        "percentile": assertion.percentile,
    }


def _threshold_trace(threshold: Any) -> dict[str, Any]:
    return {
        "threshold_id": threshold.threshold_id,
        "expected_result_id": threshold.expected_result_id,
        "after_operation_id": threshold.after_operation_id,
        "observable_ref": threshold.observable_ref,
        "kind": "threshold",
        "statement": None,
        "operator": threshold.operator,
        "expected": threshold.value,
        "unit": threshold.unit,
        "path": None,
        "name": None,
        "column": None,
        "metric": threshold.metric,
        "percentile": threshold.percentile,
    }


def _traceability(flow: PlanFlow, stage: PlanStage) -> dict[str, Any]:
    execution = stage.execution
    if isinstance(execution, PerformanceExecution):
        sources = [item.model_dump(mode="json") for item in execution.sources]
        steps = [
            {
                "step_id": "performance",
                # A performance profile may cover several approved sources, so
                # the singular field is populated only when it is unambiguous.
                "source": sources[0] if len(sources) == 1 else None,
                "sources": sources,
                "operation_ref": None,
                "profile_ref": execution.profile_ref,
                "action": None,
                "check": None,
                "data_bindings": [
                    {
                        "data_id": item.data_id,
                        "consumer_id": item.consumer_id,
                        "binding_ref": item.binding_ref,
                        "input_refs": item.input_refs,
                    }
                    for item in execution.data_bindings
                ],
                "expected_results": [
                    item.expected_result_id for item in execution.thresholds
                ],
                "assertions": [
                    _threshold_trace(item) for item in execution.thresholds
                ],
            }
        ]
    else:
        steps = [
            {
                "step_id": getattr(item, "row_id", None)
                or getattr(item, "request_id", None)
                or getattr(item, "operation_run_id", None)
                or getattr(item, "probe_run_id", None),
                "source": item.source.model_dump(mode="json"),
                "operation_ref": getattr(item, "operation_ref", None),
                "action": getattr(item, "action", None),
                "check": (
                    "；".join(
                        assertion.statement
                        for assertion in getattr(item, "assertions", [])
                        if assertion.statement
                    )
                    or None
                ),
                "data_bindings": [
                    {
                        "data_id": binding.data_id,
                        "consumer_id": binding.consumer_id,
                        "binding_ref": binding.binding_ref,
                        "input_refs": binding.input_refs,
                    }
                    for binding in getattr(item, "data_bindings", [])
                ],
                "expected_results": [
                    assertion.expected_result_id
                    for assertion in item.assertions
                ],
                "assertions": [
                    _assertion_trace(assertion)
                    for assertion in item.assertions
                ],
            }
            for item in _execution_steps(execution)
        ]
    return {
        "scenario_id": flow.scenario_id,
        "flow_id": flow.flow_id,
        "stage_id": stage.stage_id,
        "operations": stage.operation_ids,
        "expected_results": stage.expected_result_ids,
        "setup_required_states": stage.setup_required_state_ids,
        "data_requirements": stage.data_ids,
        "requirement_ids": flow.requirement_ids,
        "steps": steps,
    }


def _variable_refs(execution: Any) -> list[str]:
    if isinstance(execution, PerformanceExecution):
        bindings = execution.data_bindings
    else:
        bindings = [
            binding
            for step in _execution_steps(execution)
            for binding in getattr(step, "data_bindings", [])
        ]
    return sorted(
        {
            variable_ref
            for binding in bindings
            for variable_ref in binding.input_refs.values()
        }
    )


def _manifest(
    bundle: ApprovedTestDesignBundle,
    plan: TestPlanDraft,
    flow: PlanFlow,
    stage: PlanStage,
    catalog: PlanningCatalogSnapshot,
    *,
    artifact_id: str,
    artifact_schema_version: str,
    artifact_refs: list[dict[str, str]],
    compiled_artifact_hashes: dict[str, str],
) -> dict[str, Any]:
    traceability = _traceability(flow, stage)
    return {
        "schema_version": "executor-artifact-manifest.v4",
        "artifact_schema_version": artifact_schema_version,
        "payload_format": "json",
        "artifact_id": artifact_id,
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "plan_content_hash": plan.content_hash(),
        "design_id": bundle.design.design_id,
        "design_version": bundle.design.version,
        "design_content_hash": bundle.review.design_content_hash,
        "design_input_content_hash": bundle.review.input_content_hash,
        "catalog_id": catalog.catalog_id,
        "catalog_content_hash": catalog.content_hash,
        "executor_kind": stage.executor_kind.value,
        "flow_id": flow.flow_id,
        "stage_id": stage.stage_id,
        "stage_order": stage.order,
        "system_id": bundle.design.target.system_id,
        "environment": bundle.design.target.environment,
        "artifact_refs": artifact_refs,
        "compiled_artifact_hashes": compiled_artifact_hashes,
        # Names only; runtime values stay in the injected execution context.
        "variable_refs": _variable_refs(stage.execution),
        # Stage artifacts never own lifecycle actions. The coordinator executes
        # setup stages in order and flow cleanup once from the approved plan.
        "flow_cleanup": (
            flow.cleanup.model_dump(mode="json") if flow.cleanup is not None else None
        ),
        "traceability": traceability,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_json_bundle(
    bundle: ApprovedTestDesignBundle,
    plan: TestPlanDraft,
    flow: PlanFlow,
    stage: PlanStage,
    catalog: PlanningCatalogSnapshot,
    output_root: str | Path,
    *,
    artifact_schema_version: str,
    payload: dict[str, Any],
    payload_filename: str = "execution.json",
    supporting_files: dict[str, tuple[str, str]] | None = None,
) -> ExecutorArtifactBundle:
    destination = (
        Path(output_root)
        / plan.plan_id
        / f"v{plan.version}"
        / flow.flow_id
        / stage.stage_id
    )
    destination.mkdir(parents=True, exist_ok=True)
    payload_path = destination / payload_filename
    manifest_path = destination / "manifest.json"
    payload_path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    payload_hash = sha256_bytes(payload_path.read_bytes())
    supporting_refs: list[dict[str, str]] = []
    supporting_hashes: dict[str, str] = {}
    for kind, (filename, content) in (supporting_files or {}).items():
        path = destination / filename
        path.write_text(content, encoding="utf-8", newline="\n")
        file_hash = sha256_bytes(path.read_bytes())
        supporting_refs.append({"kind": kind, "path_ref": filename})
        supporting_hashes[filename] = file_hash
    artifact_id = f"ARTIFACT-{flow.flow_id}-{stage.stage_id}"
    manifest = _manifest(
        bundle,
        plan,
        flow,
        stage,
        catalog,
        artifact_id=artifact_id,
        artifact_schema_version=artifact_schema_version,
        artifact_refs=[
            {"kind": "payload", "path_ref": payload_filename},
            *supporting_refs,
        ],
        compiled_artifact_hashes={
            payload_filename: payload_hash,
            **supporting_hashes,
        },
    )
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    manifest_hash = sha256_bytes(manifest_path.read_bytes())
    return ExecutorArtifactBundle(
        artifact_id=artifact_id,
        artifact_schema_version=artifact_schema_version,
        executor_kind=stage.executor_kind,
        flow_id=flow.flow_id,
        stage_id=stage.stage_id,
        design_id=bundle.design.design_id,
        design_version=bundle.design.version,
        design_content_hash=bundle.review.design_content_hash,
        design_input_content_hash=bundle.review.input_content_hash,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_content_hash=plan.content_hash(),
        catalog_id=catalog.catalog_id,
        catalog_content_hash=catalog.content_hash,
        manifest_path_ref="manifest.json",
        artifact_refs=[
            ExecutorArtifactRef(
                kind="payload", path_ref=payload_filename, sha256=payload_hash
            ),
            *[
                ExecutorArtifactRef(
                    kind=kind,
                    path_ref=filename,
                    sha256=supporting_hashes[filename],
                )
                for kind, (filename, _) in (supporting_files or {}).items()
            ],
            ExecutorArtifactRef(
                kind="manifest", path_ref="manifest.json", sha256=manifest_hash
            ),
        ],
    )


__all__ = [
    "_manifest",
    "canonical_json",
    "sha256_bytes",
    "write_json_bundle",
]
