"""TCP port typed plan to a strict JSON executor artifact."""

from __future__ import annotations

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import PlanFlow, PlanStage, PortExecution, TestPlanDraft
from .json_common import write_json_bundle


class TcpPortCompiler:
    """Project reviewed endpoint refs without adding hosts, ranges, or scan options."""

    artifact_schema_version = "tcp-port-execution-plan.v4"

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
        if not isinstance(execution, PortExecution):
            raise ValueError("TcpPortCompiler 只接受 PortExecution")

        probes = []
        for step in execution.probes:
            assertions = [
                {
                    "assertion_id": f"ASSERT-{index:04d}",
                    "expected_result_id": assertion.expected_result_id,
                    "after_operation_id": assertion.after_operation_id,
                    "observable_ref": assertion.observable_ref,
                    "kind": assertion.kind,
                    "operator": assertion.operator,
                    "expected": assertion.expected,
                    "unit": assertion.unit,
                }
                for index, assertion in enumerate(step.assertions, start=1)
            ]
            probes.append(
                {
                    "probe_id": step.probe_run_id,
                    "source": step.source.model_dump(mode="json"),
                    "probe_ref": step.probe_ref,
                    "host_ref": step.host_ref,
                    "port": step.port,
                    "timeout_seconds": step.timeout_seconds,
                    "assertions": assertions,
                }
            )

        payload = {
            "schema_version": self.artifact_schema_version,
            "executor_kind": stage.executor_kind.value,
            "flow_id": flow.flow_id,
            "stage_id": stage.stage_id,
            "design_id": plan.design_id,
            "design_version": plan.design_version,
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "probes": probes,
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


__all__ = ["TcpPortCompiler"]
