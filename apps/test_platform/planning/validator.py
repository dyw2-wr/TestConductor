"""TestPlan v4 flow/stage 的确定性门禁。"""

from __future__ import annotations

import json
import re

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle, StateImpactKind
from apps.test_platform.database_sql import validate_database_sql

from .catalogs import PlanningCatalogSnapshot
from .contracts import (
    AgentUiExecution,
    DatabaseExecution,
    ExecutorKind,
    HttpExecution,
    PerformanceExecution,
    PortExecution,
    PlanDataGuaranteeResolution,
    PlanSetupStageResolution,
    PlanStatus,
    PlanValidationFinding,
    PlanValidationReport,
    TestPlanDraft,
    compute_plan_validation_content_hash,
    design_hash,
    reject_secret_values,
)


EXECUTOR_CHANNEL = {
    ExecutorKind.STAGEHAND_AGENT: "ui",
    ExecutorKind.HTTP_API: "api",
    ExecutorKind.DATABASE: "database",
    ExecutorKind.PERFORMANCE: "performance",
    ExecutorKind.TCP_PORT: "port",
}


def _same_json_value(left, right) -> bool:
    return json.dumps(
        left,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) == json.dumps(
        right,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class TestPlanValidator:
    def validate(
        self,
        bundle: ApprovedTestDesignBundle,
        plan: TestPlanDraft,
        catalog: PlanningCatalogSnapshot,
    ) -> PlanValidationReport:
        findings: list[PlanValidationFinding] = []

        def add(rule_id: str, message: str, field_path: str, *, blocking: bool = True):
            findings.append(
                PlanValidationFinding(
                    rule_id=rule_id,
                    message=message,
                    field_path=field_path,
                    blocking=blocking,
                )
            )

        if plan.status not in {
            PlanStatus.DRAFT,
            PlanStatus.IN_REVIEW,
            PlanStatus.CHANGES_REQUESTED,
        }:
            add("PLAN_STATUS_NOT_REVIEWABLE", "当前计划状态不能编译", "status")
        if (
            plan.design_id != bundle.design.design_id
            or plan.design_version != bundle.design.version
            or plan.design_content_hash != design_hash(bundle)
            or plan.design_input_content_hash != bundle.review.input_content_hash
        ):
            add(
                "DESIGN_IDENTITY_MISMATCH",
                "计划没有绑定当前 approved design 及其原始输入快照",
                "design_id",
            )
        if (
            plan.catalog_id != catalog.catalog_id
            or plan.catalog_content_hash != catalog.content_hash
        ):
            add(
                "CATALOG_IDENTITY_MISMATCH",
                "计划没有绑定当前 catalog snapshot",
                "catalog_id",
            )
        if (
            plan.target_system_id != bundle.design.target.system_id
            or plan.target_environment != bundle.design.target.environment
            or not catalog.matches_target(
                plan.target_system_id, plan.target_environment
            )
        ):
            add(
                "TARGET_MISMATCH",
                "design、plan 与 catalog 的目标不一致",
                "target_system_id",
            )
        if plan.open_questions:
            add("OPEN_QUESTIONS", "计划仍有阻塞性未决问题", "open_questions")
        if not plan.flows:
            add("FLOWS_REQUIRED", "计划至少需要一个 flow", "flows")

        scenarios = {item.scenario_id: item for item in bundle.design.scenarios}
        known_requirement_ids = {
            item.requirement_id for item in bundle.input_snapshot.requirements
        }
        expected_scenario_order = [item.scenario_id for item in bundle.design.scenarios]
        actual_scenario_order = [item.scenario_id for item in plan.flows]
        if actual_scenario_order != expected_scenario_order:
            missing = sorted(set(expected_scenario_order) - set(actual_scenario_order))
            extra = sorted(set(actual_scenario_order) - set(expected_scenario_order))
            add(
                "FLOW_COVERAGE_MISMATCH",
                "每个 scenario 必须按设计顺序恰好生成一个 flow; "
                f"missing={missing}, extra={extra}",
                "flows",
            )
        if len(set(actual_scenario_order)) != len(actual_scenario_order):
            add("FLOW_SCENARIO_DUPLICATE", "一个 scenario 只能有一个 flow", "flows")

        seen_flow_ids: set[str] = set()
        for flow_index, flow in enumerate(plan.flows, start=1):
            path = f"flows[{flow_index - 1}]"
            if flow.flow_id in seen_flow_ids:
                add("FLOW_ID_DUPLICATE", "flow_id 不能重复", f"{path}.flow_id")
            seen_flow_ids.add(flow.flow_id)
            expected_flow_id = f"FLOW-{flow_index:04d}"
            if flow.flow_id != expected_flow_id:
                add(
                    "FLOW_ID_NOT_SYSTEM_GENERATED",
                    "flow_id 必须由 approved scenario 顺序确定性生成",
                    f"{path}.flow_id",
                )
            scenario = scenarios.get(flow.scenario_id)
            if scenario is None:
                add("SCENARIO_UNKNOWN", "flow 引用了未知 scenario", f"{path}.scenario_id")
                continue
            if flow.name != scenario.title:
                add("FLOW_NAME_MISMATCH", "flow name 必须来自 scenario title", f"{path}.name")
            if flow.techniques != scenario.techniques:
                add(
                    "TECHNIQUE_PROVENANCE_MISMATCH",
                    "flow techniques 没有精确复制 scenario",
                    f"{path}.techniques",
                )
            if flow.requirement_ids != scenario.requirement_ids:
                add(
                    "REQUIREMENT_PROVENANCE_MISMATCH",
                    "flow requirement_ids 没有精确复制 scenario",
                    f"{path}.requirement_ids",
                )
            if not set(flow.requirement_ids) <= known_requirement_ids:
                add(
                    "REQUIREMENT_INPUT_MISSING",
                    "flow 引用了 input snapshot 中不存在的 requirement",
                    f"{path}.requirement_ids",
                )
            self._validate_flow(flow, scenario, catalog, path, add)

        try:
            reject_secret_values(plan.model_dump(mode="json"))
        except ValueError as exc:
            add("SECRET_VALUE_PRESENT", str(exc), "plan")

        report_payload = {
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "plan_content_hash": plan.content_hash(),
            "passed": not any(item.blocking for item in findings),
            "findings": [item.model_dump(mode="json") for item in findings],
        }
        report_payload["validation_content_hash"] = (
            compute_plan_validation_content_hash(report_payload)
        )
        return PlanValidationReport.model_validate(report_payload)

    def _validate_flow(self, flow, scenario, catalog, path, add):
        operations = {item.operation_id: item for item in scenario.operations}
        expectations = {
            item.expected_result_id: item for item in scenario.expected_results
        }
        required_states = {
            item.required_state_id: item for item in scenario.required_states
        }
        data_requirements = {item.data_id: item for item in scenario.data_requirements}

        resolution_ids = [
            item.required_state_id for item in flow.required_state_resolutions
        ]
        expected_state_ids = set(required_states)
        actual_state_ids = set(resolution_ids)
        if actual_state_ids != expected_state_ids or len(resolution_ids) != len(
            actual_state_ids
        ):
            add(
                "REQUIRED_STATE_UNRESOLVED",
                "每个 required_state 必须且只能解析为 data_guarantee 或 setup_stage; "
                f"missing={sorted(expected_state_ids - actual_state_ids)}, "
                f"unknown={sorted(actual_state_ids - expected_state_ids)}",
                f"{path}.required_state_resolutions",
            )

        stage_by_id = {item.stage_id: item for item in flow.stages}
        for index, resolution in enumerate(flow.required_state_resolutions):
            resolution_path = f"{path}.required_state_resolutions[{index}]"
            state = required_states.get(resolution.required_state_id)
            if state is None or resolution.text != state.text:
                add(
                    "REQUIRED_STATE_PROVENANCE_MISMATCH",
                    "required state resolution 没有精确复制第一层状态",
                    resolution_path,
                )
                continue
            if isinstance(resolution, PlanDataGuaranteeResolution):
                if resolution.data_id not in data_requirements:
                    add(
                        "DATA_GUARANTEE_INVALID",
                        "data_guarantee 必须绑定本场景 data_id",
                        f"{resolution_path}.data_id",
                    )
                else:
                    add(
                        "DATA_GUARANTEE_REQUIRES_REVIEW",
                        "data_guarantee 是需由计划审核人和运行时 fixture provider 确认的外部数据假设，"
                        "第三层只校验显式保证映射，不探测真实环境状态；需要确定性建立状态时应使用 setup_stage",
                        resolution_path,
                        blocking=False,
                    )
            elif isinstance(resolution, PlanSetupStageResolution):
                stage = stage_by_id.get(resolution.stage_id)
                if stage is None:
                    add(
                        "SETUP_STAGE_UNKNOWN",
                        "setup_stage resolution 引用了未知 stage",
                        f"{resolution_path}.stage_id",
                    )
                elif (
                    resolution.required_state_id
                    not in stage.setup_required_state_ids
                    or not self._execution_has_source_ref(
                        stage.execution,
                        "required_state",
                        resolution.required_state_id,
                        resolution.catalog_ref,
                    )
                ):
                    add(
                        "SETUP_STAGE_BINDING_MISMATCH",
                        "setup_stage 没有用声明的 catalog_ref 建立 required_state",
                        resolution_path,
                    )

        seen_stage_ids: set[str] = set()
        all_operation_ids: list[str] = []
        all_expected_ids: list[str] = []
        stage_data_ids: set[str] = set()
        operation_stage_order: dict[str, int] = {}
        expected_stage_order: dict[str, int] = {}
        setup_stage_orders: list[int] = []
        catalog_effects: list[str] = []
        for stage_index, stage in enumerate(flow.stages, start=1):
            stage_path = f"{path}.stages[{stage_index - 1}]"
            expected_stage_id = f"STAGE-{stage_index:04d}"
            if stage.stage_id != expected_stage_id:
                add(
                    "STAGE_ID_NOT_SYSTEM_GENERATED",
                    "stage_id 必须由 flow 内顺序确定性生成",
                    f"{stage_path}.stage_id",
                )
            if stage.stage_id in seen_stage_ids:
                add("STAGE_ID_DUPLICATE", "stage_id 不能重复", f"{stage_path}.stage_id")
            seen_stage_ids.add(stage.stage_id)
            if stage.order != stage_index:
                add(
                    "STAGE_ORDER_INVALID",
                    "stage.order 必须与线性列表顺序一致",
                    f"{stage_path}.order",
                )
            if stage.executor_kind.value not in catalog.available_executors:
                add(
                    "EXECUTOR_UNAVAILABLE",
                    "catalog 未登记该 executor",
                    f"{stage_path}.executor_kind",
                )
            channel = EXECUTOR_CHANNEL[stage.executor_kind]
            for operation_id in stage.operation_ids:
                logical = operations.get(operation_id)
                if logical is None:
                    add(
                        "OPERATION_UNKNOWN",
                        "stage 引用了未知 operation",
                        f"{stage_path}.operation_ids",
                    )
                elif logical.channel_hint.value != channel:
                    add(
                        "OPERATION_CHANNEL_MISMATCH",
                        "operation.channel_hint 与 stage executor 不一致",
                        f"{stage_path}.operation_ids",
                    )
                operation_stage_order[operation_id] = stage.order
            for expected_id in stage.expected_result_ids:
                expected = expectations.get(expected_id)
                if expected is None:
                    add(
                        "EXPECTED_UNKNOWN",
                        "stage 引用了未知 expected_result",
                        f"{stage_path}.expected_result_ids",
                    )
                elif expected.channel_hint.value != channel:
                    add(
                        "EXPECTED_CHANNEL_MISMATCH",
                        "expected_result.channel_hint 与 stage executor 不一致",
                        f"{stage_path}.expected_result_ids",
                    )
                expected_stage_order[expected_id] = stage.order
            if stage.setup_required_state_ids:
                setup_stage_orders.append(stage.order)
                if stage.operation_ids or stage.expected_result_ids:
                    add(
                        "SETUP_STAGE_MIXED_RESPONSIBILITY",
                        "setup stage 不能同时承载逻辑 operation/expected",
                        stage_path,
                    )
            all_operation_ids.extend(stage.operation_ids)
            all_expected_ids.extend(stage.expected_result_ids)
            stage_data_ids.update(stage.data_ids)

            source_operations, source_states = self._execution_source_ids(
                stage.execution
            )
            actual_expected_ids = self._execution_expected_ids(stage.execution)
            actual_data_ids = self._execution_data_ids(stage.execution)
            if source_operations != stage.operation_ids:
                add(
                    "STAGE_OPERATION_PROJECTION_MISMATCH",
                    "execution 没有精确投影 stage.operation_ids",
                    f"{stage_path}.execution",
                )
            if set(source_states) != set(stage.setup_required_state_ids) or len(
                source_states
            ) != len(set(source_states)):
                add(
                    "STAGE_SETUP_PROJECTION_MISMATCH",
                    "execution 没有精确投影 stage.setup_required_state_ids",
                    f"{stage_path}.execution",
                )
            if set(actual_expected_ids) != set(stage.expected_result_ids) or len(
                actual_expected_ids
            ) != len(set(actual_expected_ids)):
                add(
                    "STAGE_EXPECTED_PROJECTION_MISMATCH",
                    "execution 没有精确投影 stage.expected_result_ids",
                    f"{stage_path}.execution",
                )
            if set(actual_data_ids) != set(stage.data_ids):
                add(
                    "STAGE_DATA_PROJECTION_MISMATCH",
                    "execution 没有精确投影 stage.data_ids",
                    f"{stage_path}.execution",
                )
            self._validate_execution_order(
                stage.execution, scenario, stage_path, add
            )
            self._validate_execution(
                stage.execution,
                scenario,
                catalog,
                stage_path,
                add,
                catalog_effects,
            )

        if all_operation_ids != [item.operation_id for item in scenario.operations]:
            add(
                "FLOW_OPERATION_COVERAGE_MISMATCH",
                "flow 的所有 stage 必须按 approved 顺序联合且不重复覆盖场景 operations",
                f"{path}.stages",
            )
        if all_expected_ids != [
            item.expected_result_id for item in scenario.expected_results
        ]:
            add(
                "FLOW_EXPECTED_COVERAGE_MISMATCH",
                "flow 的所有 stage 必须按 approved 顺序联合且不重复覆盖场景 expected_results",
                f"{path}.stages",
            )
        for expected_id, expected in expectations.items():
            operation_order = operation_stage_order.get(expected.after_operation_id)
            assertion_order = expected_stage_order.get(expected_id)
            if (
                operation_order is not None
                and assertion_order is not None
                and assertion_order < operation_order
            ):
                add(
                    "EXPECTED_BEFORE_OPERATION",
                    "expected_result 所在 stage 不能早于 after_operation_id 所在 stage",
                    f"{path}.stages",
                )
        self._validate_flow_event_order(flow, scenario, path, add)
        ordinary_stage_orders = list(operation_stage_order.values()) + list(
            expected_stage_order.values()
        )
        if ordinary_stage_orders and setup_stage_orders:
            first_ordinary_order = min(ordinary_stage_orders)
            if any(order >= first_ordinary_order for order in setup_stage_orders):
                add(
                    "SETUP_STAGE_ORDER_INVALID",
                    "建立 required_state 的 setup stage 必须早于测试动作",
                    f"{path}.stages",
                )

        cleanup_data_ids = {
            item.data_id for item in flow.cleanup.data_bindings
        } if flow.cleanup else set()
        used_data_ids = stage_data_ids | cleanup_data_ids
        if used_data_ids != set(data_requirements):
            add(
                "FLOW_DATA_COVERAGE_MISMATCH",
                "数据必须由 stage binding 或 flow cleanup 实际消费; "
                f"missing={sorted(set(data_requirements) - used_data_ids)}, "
                f"unknown={sorted(used_data_ids - set(data_requirements))}",
                f"{path}.stages",
            )

        aggregate = self._aggregate_state_effect(catalog_effects)
        if "unknown" in catalog_effects:
            add(
                "UI_MODULE_STATE_EFFECT_UNDECLARED",
                "UI 资产未声明业务状态影响，需由计划审核人核对第一层状态影响与清理目标",
                f"{path}.stages",
                blocking=False,
            )
        elif aggregate != scenario.state_impact.impact.value:
            add(
                "FLOW_STATE_EFFECT_MISMATCH",
                "第一层 state_impact 与 flow 全部 stage 的 catalog state_effect 聚合结果冲突",
                f"{path}.stages",
            )
        self._validate_cleanup(flow.cleanup, scenario, catalog, path, add)

    @staticmethod
    def _execution_steps(execution):
        if isinstance(execution, AgentUiExecution):
            return execution.rows
        if isinstance(execution, HttpExecution):
            return execution.requests
        if isinstance(execution, DatabaseExecution):
            return execution.operations
        if isinstance(execution, PortExecution):
            return execution.probes
        return []

    def _execution_source_ids(self, execution):
        sources = (
            execution.sources
            if isinstance(execution, PerformanceExecution)
            else [item.source for item in self._execution_steps(execution)]
        )
        return (
            [item.source_id for item in sources if item.source_kind == "operation"],
            [
                item.source_id
                for item in sources
                if item.source_kind == "required_state"
            ],
        )

    def _execution_expected_ids(self, execution):
        if isinstance(execution, PerformanceExecution):
            return [item.expected_result_id for item in execution.thresholds]
        return [
            item.expected_result_id
            for step in self._execution_steps(execution)
            for item in step.assertions
        ]

    def _execution_data(self, execution):
        if isinstance(execution, PerformanceExecution):
            return list(execution.data_bindings)
        return [
            item
            for step in self._execution_steps(execution)
            for item in getattr(step, "data_bindings", [])
        ]

    def _execution_data_ids(self, execution):
        return [item.data_id for item in self._execution_data(execution)]

    def _execution_has_source_ref(
        self, execution, source_kind, source_id, catalog_ref
    ) -> bool:
        if isinstance(execution, PerformanceExecution):
            return (
                execution.profile_ref == catalog_ref
                and any(
                    item.source_kind == source_kind and item.source_id == source_id
                    for item in execution.sources
                )
            )
        return any(
            item.source.source_kind == source_kind
            and item.source.source_id == source_id
            and item.operation_ref == catalog_ref
            for item in self._execution_steps(execution)
        )

    def _validate_flow_event_order(self, flow, scenario, path, add):
        events: list[tuple[str, str]] = []
        for stage in flow.stages:
            execution = stage.execution
            if isinstance(execution, PerformanceExecution):
                thresholds_by_operation: dict[str, list[str]] = {}
                for item in execution.thresholds:
                    thresholds_by_operation.setdefault(
                        item.after_operation_id, []
                    ).append(item.expected_result_id)
                for source in execution.sources:
                    if source.source_kind == "operation":
                        events.append(("operation", source.source_id))
                        events.extend(
                            ("expected", expected_id)
                            for expected_id in thresholds_by_operation.get(
                                source.source_id, []
                            )
                        )
                    elif source.source_kind == "expected_result":
                        events.append(("expected", source.source_id))
                continue
            for step in self._execution_steps(execution):
                if step.source.source_kind == "operation":
                    events.append(("operation", step.source.source_id))
                if step.source.source_kind == "expected_result" and not step.assertions:
                    events.append(("expected", step.source.source_id))
                events.extend(
                    ("expected", item.expected_result_id)
                    for item in step.assertions
                )

        operation_positions = {
            value: index
            for index, (kind, value) in enumerate(events)
            if kind == "operation"
        }
        expected_positions = {
            value: index
            for index, (kind, value) in enumerate(events)
            if kind == "expected"
        }
        approved_operations = [item.operation_id for item in scenario.operations]
        for expected in scenario.expected_results:
            anchor = operation_positions.get(expected.after_operation_id)
            position = expected_positions.get(expected.expected_result_id)
            if anchor is None or position is None:
                continue
            if position <= anchor:
                add(
                    "EXPECTED_NOT_AFTER_OPERATION",
                    "expected_result 必须发生在 after_operation_id 执行之后",
                    f"{path}.stages",
                )
                continue
            anchor_index = approved_operations.index(expected.after_operation_id)
            if anchor_index + 1 < len(approved_operations):
                next_operation = approved_operations[anchor_index + 1]
                next_position = operation_positions.get(next_operation)
                if next_position is not None and position >= next_position:
                    add(
                        "EXPECTED_AFTER_NEXT_OPERATION",
                        "expected_result 必须在下一个 approved operation 执行前完成",
                        f"{path}.stages",
                    )

    def _validate_execution_order(self, execution, scenario, path, add):
        sources = (
            execution.sources
            if isinstance(execution, PerformanceExecution)
            else [item.source for item in self._execution_steps(execution)]
        )
        operation_order = {
            item.operation_id: index for index, item in enumerate(scenario.operations)
        }
        expected_order = {
            item.expected_result_id: index
            for index, item in enumerate(scenario.expected_results)
        }
        expected_by_id = {
            item.expected_result_id: item for item in scenario.expected_results
        }
        keys = []
        for index, source in enumerate(sources):
            if source.source_kind == "required_state":
                keys.append((-1, index))
            elif source.source_kind == "operation":
                keys.append((operation_order.get(source.source_id, 10**9), 0))
            else:
                expected = expected_by_id.get(source.source_id)
                keys.append(
                    (
                        operation_order.get(
                            expected.after_operation_id if expected else "", 10**9
                        ),
                        expected_order.get(source.source_id, 10**9) + 1,
                    )
                )
        if keys != sorted(keys):
            add(
                "EXECUTION_STEP_ORDER_MISMATCH",
                "execution step 必须保持 operation -> observe(operation) 的 approved 顺序",
                f"{path}.execution",
            )

    def _validate_execution(
        self, execution, scenario, catalog, path, add, catalog_effects
    ):
        operations = {item.operation_id: item for item in scenario.operations}
        expectations = {
            item.expected_result_id: item for item in scenario.expected_results
        }
        required_states = {
            item.required_state_id: item for item in scenario.required_states
        }
        if isinstance(execution, HttpExecution):
            for index, step in enumerate(execution.requests):
                step_path = f"{path}.execution.requests[{index}]"
                operation = catalog.get_http_operation(step.operation_ref)
                if operation is not None:
                    catalog_effects.append(operation.state_effect)
                    if (
                        step.source.source_kind == "required_state"
                        and operation.state_effect == "read_only"
                    ):
                        add(
                            "SETUP_RESOURCE_READ_ONLY",
                            "setup required_state 不能使用 read_only HTTP resource",
                            step_path,
                        )
                if operation is None or (
                    operation.base_url_ref != execution.base_url_ref
                    or operation.method != step.method
                    or operation.path != step.path
                ):
                    add(
                        "HTTP_CATALOG_MISMATCH",
                        "HTTP execution 与 catalog 不一致",
                        step_path,
                    )
                self._validate_source_action(
                    step.source, step.action, operation, operations, required_states, step_path, add
                )
                self._validate_assertions(
                    step.assertions, expectations, operation, step_path, add, step.source
                )
                self._validate_data(
                    step.data_bindings, step.operation_ref, catalog, step_path, add
                )
                path_parameters = set(
                    re.findall(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}", step.path)
                )
                bound_path_parameters = {
                    slot.removeprefix("path.")
                    for binding in step.data_bindings
                    for slot in binding.input_refs
                    if slot.startswith("path.")
                }
                if path_parameters != bound_path_parameters:
                    add(
                        "HTTP_PATH_BINDING_MISMATCH",
                        "HTTP path 参数必须由 catalog DataBinding 精确覆盖",
                        f"{step_path}.data_bindings",
                    )
        elif isinstance(execution, DatabaseExecution):
            for index, step in enumerate(execution.operations):
                step_path = f"{path}.execution.operations[{index}]"
                if step.sql_origin == "catalog":
                    add(
                        "DATABASE_CATALOG_SQL_RETIRED",
                        "数据库 SQL 必须完整写入执行计划，不能引用资源目录查询",
                        step_path,
                    )
                    continue
                schema = catalog.get_database_schema()
                catalog_effects.append(
                    "changes_state"
                    if step.execution_policy == "write"
                    else "read_only"
                )
                if schema is None:
                    add(
                        "DATABASE_AI_SCHEMA_MISSING",
                        "AI SQL 缺少已登记数据库结构约束",
                        step_path,
                    )
                else:
                    if (
                        execution.connection_profile_ref
                        != schema.connection_profile_ref
                    ):
                        add(
                            "DATABASE_AI_CONNECTION_MISMATCH",
                            "AI SQL 使用了未登记数据库连接",
                            step_path,
                        )
                    try:
                        validate_database_sql(
                            step.sql or "",
                            execution_policy=step.execution_policy,
                            allowed_tables=[
                                item.name for item in schema.tables
                            ],
                            allowed_columns={
                                item.name: [
                                    column.name for column in item.columns
                                ]
                                for item in schema.tables
                            },
                            allowed_parameter_refs=schema.allowed_parameter_refs,
                            parameters_refs=step.parameters_refs,
                        )
                    except ValueError as exc:
                        add(
                            "DATABASE_AI_SQL_UNSAFE",
                            str(exc),
                            f"{step_path}.sql",
                        )
                self._validate_generated_database_step(
                    step,
                    expectations,
                    operations,
                    step_path,
                    add,
                )
        elif isinstance(execution, PortExecution):
            for index, step in enumerate(execution.probes):
                step_path = f"{path}.execution.probes[{index}]"
                probe = catalog.get_tcp_port_probe(step.probe_ref)
                if probe is not None:
                    catalog_effects.append(probe.state_effect)
                    if step.source.source_kind == "required_state":
                        add(
                            "SETUP_RESOURCE_READ_ONLY",
                            "TCP port probe 是只读探测，不能用于建立 required_state",
                            step_path,
                        )
                if probe is None or (
                    probe.host_ref != step.host_ref
                    or probe.port != step.port
                    or probe.timeout_seconds != step.timeout_seconds
                ):
                    add(
                        "TCP_PORT_CATALOG_MISMATCH",
                        "TCP port execution 与 catalog 不一致",
                        step_path,
                    )
                self._validate_source_action(
                    step.source,
                    step.action,
                    probe,
                    operations,
                    required_states,
                    step_path,
                    add,
                )
                self._validate_assertions(
                    step.assertions,
                    expectations,
                    probe,
                    step_path,
                    add,
                    step.source,
                )
                allowed = {
                    item.observable_ref: item
                    for item in (probe.observables if probe is not None else [])
                }
                for assertion_index, assertion in enumerate(step.assertions):
                    expected = expectations.get(assertion.expected_result_id)
                    observable = allowed.get(assertion.observable_ref)
                    assertion_path = f"{step_path}.assertions[{assertion_index}]"
                    if expected is None or observable is None:
                        continue
                    if observable.kind == "state":
                        valid = (
                            expected.expected in {"open", "closed", "filtered"}
                            and expected.operator in {"equals", "not_equals"}
                            and expected.unit is None
                        )
                    else:
                        records_observation = (
                            expected.operator is None
                            and expected.expected is None
                            and assertion.operator == "exists"
                            and assertion.expected is None
                            and expected.unit in {None, "ms"}
                        )
                        compares_value = (
                            isinstance(expected.expected, (int, float))
                            and not isinstance(expected.expected, bool)
                            and expected.expected >= 0
                            and expected.operator
                            in {"equals", "lte", "lt", "gte", "gt"}
                            and expected.unit in {None, "ms"}
                        )
                        valid = records_observation or compares_value
                    if not valid:
                        add(
                            "TCP_PORT_ASSERTION_INVALID",
                            "TCP port 断言必须是 state(open/closed/filtered) 或非负 connect_latency_ms 数值断言",
                            assertion_path,
                        )
        elif isinstance(execution, PerformanceExecution):
            profile = catalog.get_performance_profile(execution.profile_ref)
            if profile is not None:
                catalog_effects.append(profile.state_effect)
                if (
                    any(item.source_kind == "required_state" for item in execution.sources)
                    and profile.state_effect == "read_only"
                ):
                    add(
                        "SETUP_RESOURCE_READ_ONLY",
                        "setup required_state 不能使用 read_only performance profile",
                        f"{path}.execution",
                    )
            exceeds_profile = profile is not None and (
                sum(item.duration_seconds for item in execution.stages)
                > profile.max_duration_seconds
                or any(
                    item.virtual_users > profile.max_virtual_users
                    for item in execution.stages
                )
            )
            if profile is None or profile.driver_ref != execution.driver_ref or exceeds_profile:
                add(
                    "PERFORMANCE_CATALOG_MISMATCH",
                    "性能 execution 使用了错误 driver 或超出 catalog 约束",
                    f"{path}.execution",
                )
            allowed = {item.observable_ref: item for item in profile.observables} if profile else {}
            for index, threshold in enumerate(execution.thresholds):
                expected = expectations.get(threshold.expected_result_id)
                observable = allowed.get(threshold.observable_ref)
                numeric_expected = (
                    expected is not None
                    and isinstance(expected.expected, (int, float))
                    and not isinstance(expected.expected, bool)
                )
                if (
                    expected is None
                    or observable is None
                    or not numeric_expected
                    or expected.after_operation_id != threshold.after_operation_id
                    or expected.operator != threshold.operator
                    or float(expected.expected) != threshold.value
                    or observable.metric != threshold.metric
                    or threshold.unit != (expected.unit or observable.unit)
                    or threshold.percentile != observable.percentile
                ):
                    add(
                        "PERFORMANCE_THRESHOLD_MISMATCH",
                        "性能阈值不是 design + catalog 的精确投影",
                        f"{path}.execution.thresholds[{index}]",
                    )
            self._validate_data(
                execution.data_bindings,
                execution.profile_ref,
                catalog,
                f"{path}.execution",
                add,
            )
        elif isinstance(execution, AgentUiExecution):
            profile = catalog.get_agent_ui_profile(execution.capability_profile_ref)
            if profile is None or profile.max_steps != execution.max_steps:
                add(
                    "AGENT_UI_PROFILE_MISMATCH",
                    "网页 Agent 能力配置不存在或最大步数被修改",
                    f"{path}.execution",
                )
            allowed_operations = {
                item.operation_ref: item
                for item in (profile.operations if profile is not None else [])
            }
            allowed_observables = {
                item.observable_ref: item
                for item in (profile.observables if profile is not None else [])
            }
            for index, row in enumerate(execution.rows):
                row_path = f"{path}.execution.rows[{index}]"
                operation = allowed_operations.get(row.operation_ref)
                if operation is None:
                    add(
                        "AGENT_UI_OPERATION_MISMATCH",
                        "网页 Agent 操作不属于所选资产能力",
                        row_path,
                    )
                else:
                    catalog_effects.append(operation.state_effect)
                self._validate_source_action(
                    row.source,
                    row.action,
                    operation,
                    operations,
                    required_states,
                    row_path,
                    add,
                )
                for assertion in row.assertions:
                    if assertion.observable_ref not in allowed_observables:
                        add(
                            "AGENT_UI_OBSERVABLE_MISMATCH",
                            "网页 Agent 检查不属于所选资产能力",
                            f"{row_path}.assertions",
                        )
                self._validate_assertions(
                    row.assertions,
                    expectations,
                    profile,
                    row_path,
                    add,
                    row.source,
                )
    @staticmethod
    def _validate_generated_database_step(
        step,
        expectations,
        operations,
        path,
        add,
    ):
        if step.source.source_kind == "operation":
            logical = operations.get(step.source.source_id)
            if logical is None or step.action != logical.text:
                add(
                    "DATABASE_AI_SOURCE_MISMATCH",
                    "AI SQL 没有绑定已审批业务操作",
                    path,
                )
        elif step.source.source_kind != "expected_result":
            add(
                "DATABASE_AI_SOURCE_INVALID",
                "AI SQL 只能绑定业务操作或预期结果",
                path,
            )
        allowed_operators = {
            "column": {
                "equals",
                "not_equals",
                "not_null",
                "null",
                "gte",
                "lte",
                "gt",
                "lt",
                "contains",
                "exists",
                "not_exists",
            },
            "row_count": {
                "equals",
                "not_equals",
                "gte",
                "lte",
                "gt",
                "lt",
            },
            "affected_rows": {
                "equals",
                "not_equals",
                "gte",
                "lte",
                "gt",
                "lt",
            },
            "exists": {
                "equals",
                "not_equals",
                "exists",
                "not_exists",
            },
        }
        for index, assertion in enumerate(step.assertions):
            assertion_path = f"{path}.assertions[{index}]"
            expected = expectations.get(assertion.expected_result_id)
            if expected is None:
                add(
                    "ASSERTION_BINDING_INVALID",
                    "AI SQL 检查引用了未知预期结果",
                    assertion_path,
                )
                continue
            if (
                expected.after_operation_id != assertion.after_operation_id
                or expected.text != assertion.statement
                or expected.unit != assertion.unit
                or (
                    expected.operator is not None
                    and expected.operator != assertion.operator
                )
                or (
                    expected.expected is not None
                    and not _same_json_value(
                        expected.expected,
                        assertion.expected,
                    )
                )
            ):
                add(
                    "ASSERTION_EXPECTED_MISMATCH",
                    "AI SQL 检查改写了已审批预期",
                    assertion_path,
                )
            if assertion.operator not in allowed_operators.get(
                assertion.kind,
                set(),
            ):
                add(
                    "ASSERTION_OPERATOR_UNSUPPORTED",
                    f"operator 不适用于 {assertion.kind}",
                    f"{assertion_path}.operator",
                )
            if assertion.kind == "column" and not assertion.column:
                add(
                    "ASSERTION_COLUMN_REQUIRED",
                    "column 检查必须指定结果列",
                    f"{assertion_path}.column",
                )
            if step.execution_policy == "write" and assertion.kind != "affected_rows":
                add(
                    "DATABASE_WRITE_ASSERTION_INVALID",
                    "数据库写 SQL 必须检查 affected_rows",
                    assertion_path,
                )
            if (
                step.execution_policy == "read_only"
                and assertion.kind == "affected_rows"
            ):
                add(
                    "DATABASE_READ_ASSERTION_INVALID",
                    "只读 SQL 不能检查 affected_rows",
                    assertion_path,
                )

    @staticmethod
    def _validate_source_action(
        source, action, operation, operations, required_states, path, add
    ):
        business_action = str(action or "")
        if operation is None:
            add("CATALOG_REF_UNKNOWN", "execution 引用了未知 catalog ref", path)
            return
        if source.source_kind == "operation":
            logical = operations.get(source.source_id)
            expected_action = logical.text if logical else None
            if logical is None or business_action != expected_action:
                add(
                    "OPERATION_SOURCE_MISMATCH",
                    "execution action 没有精确复制第一层 operation",
                    path,
                )
        elif source.source_kind == "required_state":
            state = required_states.get(source.source_id)
            expected_action = getattr(operation, "action", None) or operation.description
            if state is None or business_action != expected_action:
                add(
                    "SETUP_SOURCE_MISMATCH",
                    "setup execution 没有绑定第一层 required_state 和 catalog action",
                    path,
                )
        else:
            expected_action = getattr(operation, "action", None) or operation.description
            if business_action != expected_action:
                add(
                    "OBSERVER_SOURCE_MISMATCH",
                    "观察 execution 没有精确复制 catalog action",
                    path,
                )

    @staticmethod
    def _validate_assertions(
        assertions, expectations, operation, path, add, source=None
    ):
        allowed = {
            item.observable_ref: item
            for item in (getattr(operation, "observables", []) if operation else [])
        }
        no_expected_operators = {"exists", "not_exists", "not_null", "null"}
        for index, assertion in enumerate(assertions):
            expected = expectations.get(assertion.expected_result_id)
            observable = allowed.get(assertion.observable_ref)
            assertion_path = f"{path}.assertions[{index}]"
            if expected is None or observable is None:
                add("ASSERTION_BINDING_INVALID", "断言引用无效", assertion_path)
                continue
            if source is not None and (
                (
                    source.source_kind == "operation"
                    and assertion.after_operation_id != source.source_id
                )
                or (
                    source.source_kind == "expected_result"
                    and assertion.expected_result_id != source.source_id
                )
                or source.source_kind == "required_state"
            ):
                add(
                    "ASSERTION_SOURCE_ORDER_MISMATCH",
                    "断言没有绑定其 after_operation 或独立观察 step",
                    assertion_path,
                )
            recording_normalized = (
                assertion.kind == "connect_latency_ms"
                and expected.operator is None
                and expected.expected is None
                and assertion.operator == "exists"
                and assertion.expected is None
            )
            if (
                expected.after_operation_id != assertion.after_operation_id
                or (
                    expected.operator != assertion.operator
                    and not recording_normalized
                )
                or not _same_json_value(expected.expected, assertion.expected)
                or expected.unit != assertion.unit
                or expected.text != assertion.statement
            ):
                add(
                    "ASSERTION_EXPECTED_MISMATCH",
                    "断言改写了第一层 expected",
                    assertion_path,
                )
            if assertion.kind != "agent_ui" and not assertion.operator:
                add(
                    "ASSERTION_OPERATOR_REQUIRED",
                    "自动断言需要结构化 operator",
                    f"{assertion_path}.operator",
                )
            elif (
                assertion.kind != "agent_ui"
                and assertion.expected is None
                and assertion.operator not in no_expected_operators
            ):
                add(
                    "ASSERTION_EXPECTED_REQUIRED",
                    "该 operator 需要 expected 值",
                    f"{assertion_path}.expected",
                )
            for field in ("kind", "path", "name", "column", "metric", "percentile"):
                catalog_value = (
                    "agent_ui"
                    if field == "kind" and not hasattr(observable, "kind")
                    else getattr(observable, field, None)
                )
                if getattr(assertion, field) != catalog_value:
                    add(
                        "ASSERTION_OBSERVABLE_MISMATCH",
                        "断言观察位置与 catalog 不一致",
                        f"{assertion_path}.{field}",
                    )
            allowed_operators = {
                "status": {"equals", "gte", "lte", "gt", "lt"},
                "json": {
                    "equals", "exists", "not_exists", "contains",
                    "gte", "lte", "gt", "lt",
                },
                "header": {"equals", "exists", "not_exists", "contains"},
                "body_contains": {"equals", "contains"},
                "text_contains": {"equals", "contains"},
                "column": {
                    "equals", "not_equals", "not_null", "null", "gte", "lte",
                    "gt", "lt", "contains", "exists", "not_exists",
                },
                "row_count": {"equals", "not_equals", "gte", "lte", "gt", "lt"},
                "exists": {"equals", "not_equals", "exists", "not_exists"},
                "state": {"equals", "not_equals"},
                "connect_latency_ms": {"equals", "lte", "lt", "gte", "gt", "exists"},
                "agent_ui": set(),
            }.get(assertion.kind)
            if (
                allowed_operators is not None
                and assertion.kind != "agent_ui"
                and assertion.operator not in allowed_operators
            ):
                add(
                    "ASSERTION_OPERATOR_UNSUPPORTED",
                    f"operator 不适用于 {assertion.kind}",
                    f"{assertion_path}.operator",
                )

    @staticmethod
    def _validate_data(data, operation_ref, catalog, path, add):
        for index, item in enumerate(data):
            binding = catalog.get_data_binding(item.binding_ref)
            if binding is None or (
                binding.operation_ref != operation_ref
                or binding.input_refs != item.input_refs
            ):
                add(
                    "DATA_BINDING_MISMATCH",
                    "数据绑定与 catalog 不一致",
                    f"{path}.data_bindings[{index}]",
                )

    @staticmethod
    def _validate_cleanup(cleanup, scenario, catalog, path, add):
        mutates = scenario.state_impact.impact in {
            StateImpactKind.CREATES_DATA,
            StateImpactKind.CHANGES_STATE,
        }
        goal = scenario.state_impact.cleanup_goal
        if not mutates and (goal is not None or cleanup is not None):
            add(
                "READ_ONLY_CLEANUP_FORBIDDEN",
                "read_only 场景不能声明或执行 cleanup",
                f"{path}.cleanup",
            )
            return
        if mutates and goal is None:
            add(
                "CLEANUP_GOAL_REQUIRED",
                "变更状态的场景必须在第一层声明 cleanup_goal",
                f"{path}.cleanup",
            )
            return
        if goal is not None and cleanup is None:
            add(
                "CLEANUP_REQUIRED",
                "已有 cleanup_goal 必须在 flow 级绑定一次 cleanup action",
                f"{path}.cleanup",
            )
            return
        if goal is None and cleanup is not None:
            add(
                "CLEANUP_NOT_DECLARED",
                "第一层没有 cleanup_goal，flow 不能增加 cleanup",
                f"{path}.cleanup",
            )
            return
        if cleanup is None:
            return
        action = catalog.get_cleanup_action(cleanup.action_ref)
        if goal is None or cleanup.cleanup_goal_id != goal.cleanup_goal_id or action is None:
            add(
                "CLEANUP_BINDING_INVALID",
                "cleanup 没有绑定当前目标或登记动作",
                f"{path}.cleanup",
            )
            return
        if (
            cleanup.handler_kind != action.handler_kind
            or cleanup.policy != action.policy
            or cleanup.target != action.target
            or cleanup.always_run != action.always_run
            or cleanup.evidence_required != action.evidence_required
        ):
            add(
                "CLEANUP_CATALOG_MISMATCH",
                "cleanup 执行属性与 catalog 不一致",
                f"{path}.cleanup",
            )
        slots = [item.slot for item in cleanup.data_bindings]
        if set(slots) != set(action.required_data_slots) or len(slots) != len(set(slots)):
            add(
                "CLEANUP_DATA_SLOT_MISMATCH",
                "cleanup data bindings 必须精确覆盖 required_data_slots",
                f"{path}.cleanup.data_bindings",
            )
        for index, item in enumerate(cleanup.data_bindings):
            binding = catalog.get_data_binding(item.binding_ref)
            if (
                item.data_id not in goal.subject_data_ids
                or binding is None
                or binding.operation_ref != action.action_ref
                or binding.executor_kind != action.handler_kind
                or binding.input_refs.get(item.slot) != item.variable_ref
            ):
                add(
                    "CLEANUP_DATA_BINDING_INVALID",
                    "cleanup data binding 不是 design + catalog 的精确投影",
                    f"{path}.cleanup.data_bindings[{index}]",
                )
        if {item.data_id for item in cleanup.data_bindings} != set(
            goal.subject_data_ids
        ):
            add(
                "CLEANUP_SUBJECT_COVERAGE_MISMATCH",
                "cleanup data bindings 必须精确覆盖 cleanup_goal.subject_data_ids",
                f"{path}.cleanup.data_bindings",
            )

    @staticmethod
    def _aggregate_state_effect(effects: list[str]) -> str:
        if "changes_state" in effects:
            return "changes_state"
        if "creates_data" in effects:
            return "creates_data"
        return "read_only"


__all__ = ["TestPlanValidator"]
