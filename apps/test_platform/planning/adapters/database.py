"""Database typed plan 到只读 JSON artifact 的投影。"""

from __future__ import annotations

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import DatabaseExecution, PlanFlow, PlanStage, TestPlanDraft
from .json_common import write_json_bundle


class DatabaseCompiler:
    artifact_schema_version = "database-execution-plan.v5"
    _supported_observables = {"column", "row_count", "exists"}

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
        if not isinstance(execution, DatabaseExecution):
            raise ValueError("DatabaseCompiler 只接受 DatabaseExecution")
        operations = []
        for step in execution.operations:
            if step.execution_policy != "read_only":
                raise ValueError(
                    f"当前 database runner 未实现策略: {step.execution_policy}"
                )
            parameters: dict[str, str] = {}
            if step.sql_origin == "catalog":
                for binding in step.data_bindings:
                    for slot, variable_ref in binding.input_refs.items():
                        if not slot.startswith("param."):
                            raise ValueError(
                                f"database input slot 必须是 param.<name>: {slot}"
                            )
                        name = slot.removeprefix("param.")
                        if not name or name in parameters:
                            raise ValueError(f"database 参数重复或为空: {slot}")
                        parameters[name] = variable_ref
            else:
                parameters = dict(step.parameters_refs)
            assertions = []
            for index, assertion in enumerate(step.assertions, start=1):
                if assertion.kind not in self._supported_observables:
                    raise ValueError(
                        f"当前 database runner 不支持 observable kind: {assertion.kind}"
                    )
                assertions.append(
                    {
                        "assertion_id": f"ASSERT-{index:04d}",
                        "expected_result_id": assertion.expected_result_id,
                        "after_operation_id": assertion.after_operation_id,
                        "kind": assertion.kind,
                        "column": assertion.column,
                        "operator": assertion.operator,
                        "expected": assertion.expected,
                        "unit": assertion.unit,
                    }
                )
            operation = {
                "query_id": step.operation_run_id,
                "source": step.source.model_dump(mode="json"),
                "operation_ref": step.operation_ref,
                "parameters_refs": parameters,
                "assertions": assertions,
            }
            if step.sql_origin == "catalog":
                operation["query_ref"] = step.operation_ref
            else:
                operation.update(
                    {
                        "sql": step.sql,
                        "sql_origin": step.sql_origin,
                        "knowledge_scope_id": step.knowledge_scope_id,
                    }
                )
            operations.append(operation)
        payload = {
            "schema_version": self.artifact_schema_version,
            "executor_kind": stage.executor_kind.value,
            "flow_id": flow.flow_id,
            "stage_id": stage.stage_id,
            "design_id": plan.design_id,
            "design_version": plan.design_version,
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "connection_profile_ref": execution.connection_profile_ref,
            "read_only": True,
            "queries": operations,
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


__all__ = ["DatabaseCompiler"]
