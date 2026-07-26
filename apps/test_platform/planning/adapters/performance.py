"""Performance typed plan 到 JSON artifact 的投影。"""

from __future__ import annotations

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import PerformanceExecution, PlanFlow, PlanStage, TestPlanDraft
from .json_common import write_json_bundle


class PerformanceCompiler:
    artifact_schema_version = "performance-execution-plan.v4"

    def compile(
        self,
        bundle: ApprovedTestDesignBundle,
        plan: TestPlanDraft,
        flow: PlanFlow,
        stage: PlanStage,
        catalog: PlanningCatalogSnapshot,
        output_root,
    ):
        execution = stage.execution
        if not isinstance(execution, PerformanceExecution):
            raise ValueError("PerformanceCompiler 只接受 PerformanceExecution")
        input_refs: dict[str, str] = {}
        for binding in execution.data_bindings:
            for slot, variable_ref in binding.input_refs.items():
                if not slot.startswith("input."):
                    raise ValueError(f"performance input slot 必须是 input.<name>: {slot}")
                slot = slot.removeprefix("input.")
                if slot in input_refs:
                    raise ValueError(f"performance input slot 重复绑定: {slot}")
                input_refs[slot] = variable_ref
        payload = {
            "schema_version": self.artifact_schema_version,
            "executor_kind": stage.executor_kind.value,
            "flow_id": flow.flow_id,
            "stage_id": stage.stage_id,
            "design_id": plan.design_id,
            "design_version": plan.design_version,
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "driver_ref": execution.driver_ref,
            "load_profile_ref": execution.profile_ref,
            "sources": [item.model_dump(mode="json") for item in execution.sources],
            "stages": [item.model_dump(mode="json") for item in execution.stages],
            "input_refs": input_refs,
            "thresholds": [
                {
                    "threshold_id": item.threshold_id,
                    "expected_result_id": item.expected_result_id,
                    "after_operation_id": item.after_operation_id,
                    "metric": item.metric,
                    "operator": item.operator,
                    "value": item.value,
                    "unit": item.unit,
                    "percentile": item.percentile,
                }
                for item in execution.thresholds
            ],
        }
        return write_json_bundle(
            bundle,
            plan,
            flow,
            stage,
            catalog,
            output_root,
            artifact_schema_version=self.artifact_schema_version,
            payload=payload,
        )


__all__ = ["PerformanceCompiler"]
