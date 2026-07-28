"""Agent UI typed plan to the Stagehand runner artifact."""

from __future__ import annotations

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import AgentUiExecution, PlanFlow, PlanStage, TestPlanDraft
from .json_common import write_json_bundle


class AgentUiCompiler:
    artifact_schema_version = "agent-ui-execution-plan.v1"

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
        if not isinstance(execution, AgentUiExecution):
            raise ValueError("AgentUiCompiler 只接受 AgentUiExecution")
        payload = {
            "schema_version": self.artifact_schema_version,
            "executor_kind": stage.executor_kind.value,
            "flow_id": flow.flow_id,
            "stage_id": stage.stage_id,
            "design_id": plan.design_id,
            "design_version": plan.design_version,
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "start_url": execution.start_url,
            "max_steps": execution.max_steps,
            "rows": [
                {
                    "row_id": row.row_id,
                    "source": row.source.model_dump(mode="json"),
                    "operation_ref": row.operation_ref,
                    "action": row.action,
                    "checks": [
                        {
                            "expected_result_id": assertion.expected_result_id,
                            "after_operation_id": assertion.after_operation_id,
                            "statement": assertion.statement,
                            "operator": assertion.operator,
                            "expected": assertion.expected,
                        }
                        for assertion in row.assertions
                    ],
                }
                for row in execution.rows
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
            payload_filename="agent-ui.json",
        )


__all__ = ["AgentUiCompiler"]
