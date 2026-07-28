"""Database typed plan to an explicit, reviewable SQL artifact."""

from __future__ import annotations

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import DatabaseExecution, PlanFlow, PlanStage, TestPlanDraft
from .json_common import write_json_bundle


class DatabaseCompiler:
    artifact_schema_version = "database-execution-plan.v6"
    _supported_observables = {"column", "row_count", "exists", "affected_rows"}

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
            if step.sql_origin == "catalog" or step.sql is None:
                raise ValueError("数据库执行计划必须包含本次审批的完整 SQL，不能引用目录查询")
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
                "statement_id": step.operation_run_id,
                "source": step.source.model_dump(mode="json"),
                "operation_ref": step.operation_ref,
                "execution_policy": step.execution_policy,
                "risk_level": (
                    "high" if step.execution_policy == "write" else "normal"
                ),
                "parameters_refs": parameters,
                "assertions": assertions,
                "sql": step.sql,
                "sql_origin": step.sql_origin,
                "knowledge_scope_id": step.knowledge_scope_id,
            }
            operations.append(operation)
        contains_writes = any(
            item.execution_policy == "write" for item in execution.operations
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
            "connection_profile_ref": execution.connection_profile_ref,
            "contains_writes": contains_writes,
            "warnings": (
                [
                    "高风险：本执行计划包含数据库写操作，审批并运行后会修改测试环境数据。"
                ]
                if contains_writes
                else []
            ),
            "statements": operations,
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
