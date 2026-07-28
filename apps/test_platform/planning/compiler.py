"""把模型选择的 catalog 引用确定性编译成 TestPlan v4 flow。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

from apps.test_platform.intent.contracts import (
    ApprovedTestDesignBundle,
    LogicalScenario,
)
from apps.test_platform.database_sql import validate_database_sql

from .adapters import (
    DatabaseCompiler,
    HttpApiCompiler,
    PerformanceCompiler,
    TcpPortCompiler,
)
from .catalogs import (
    AgentUiCapabilityProfile,
    AgentUiObservable,
    AgentUiOperation,
    LoadStage,
    HttpObservable,
    HttpOperation,
    PerformanceObservable,
    PerformanceProfile,
    PlanningCatalogSnapshot,
    PortObservable,
    TcpPortProbe,
)
from .contracts import (
    AgentUiExecution,
    AgentUiPlanRow,
    ApprovedTestPlanBundle,
    BoundAssertion,
    BoundData,
    CleanupDataBinding,
    DatabaseExecution,
    DatabaseOperationPlan,
    DatabaseQueryDraftCandidate,
    DataGuaranteeResolutionCandidate,
    ExecutionSource,
    ExecutorArtifactBundle,
    ExecutorKind,
    ExpectedResultSelection,
    HttpExecution,
    HttpRequestPlan,
    PerformanceExecution,
    PerformanceThreshold,
    PlanCandidate,
    PlanCleanup,
    PlanDataGuaranteeResolution,
    PlanFlow,
    PlanFlowCandidate,
    PlanOpenQuestion,
    PlanReview,
    PlanReviewDecision,
    PlanSetupStageResolution,
    PlanStage,
    PlanStageCandidate,
    PlanStatus,
    PlanValidationReport,
    PortExecution,
    PortProbePlan,
    SetupStageResolutionCandidate,
    TestPlanDraft,
    compute_artifact_set_hash,
    compute_plan_review_content_hash,
    compute_plan_validation_content_hash,
    design_hash,
    reject_secret_values,
)
from .validator import TestPlanValidator
from .artifact_paths import artifact_category, generated_files_root


EXECUTOR_CHANNEL = {
    ExecutorKind.STAGEHAND_AGENT: "ui",
    ExecutorKind.HTTP_API: "api",
    ExecutorKind.DATABASE: "database",
    ExecutorKind.PERFORMANCE: "performance",
    ExecutorKind.TCP_PORT: "port",
}


@dataclass
class PlanCompilationResult:
    plan: TestPlanDraft
    validation: PlanValidationReport
    artifacts: list[ExecutorArtifactBundle] = field(default_factory=list)


@dataclass
class _ExecutionUnit:
    source: ExecutionSource
    catalog_ref: str
    action: str
    sort_key: tuple[int, int]
    assertions: list[ExpectedResultSelection] = field(default_factory=list)
    data_selections: list[Any] = field(default_factory=list)


class TestPlanCompiler:
    """第二层唯一服务入口；生成线性计划和产物，但不执行测试。"""

    def __init__(self, validator: TestPlanValidator | None = None):
        self.validator = validator or TestPlanValidator()
        self._reviewed_revisions: set[tuple[str, int, str]] = set()
        self._adapters = {
            ExecutorKind.HTTP_API: HttpApiCompiler(),
            ExecutorKind.DATABASE: DatabaseCompiler(),
            ExecutorKind.PERFORMANCE: PerformanceCompiler(),
            ExecutorKind.TCP_PORT: TcpPortCompiler(),
        }

    def _adapter_for(self, executor_kind: ExecutorKind):
        """Resolve a stage compiler without importing the unfinished UI adapter eagerly."""

        adapter = self._adapters.get(executor_kind)
        if adapter is not None:
            return adapter
        if executor_kind == ExecutorKind.STAGEHAND_AGENT:
            from .adapters.agent_ui import AgentUiCompiler

            adapter = AgentUiCompiler()
            self._adapters[executor_kind] = adapter
            return adapter
        raise ValueError(f"未登记 stage compiler: {executor_kind.value}")

    def build_draft(
        self,
        bundle: ApprovedTestDesignBundle,
        candidate: PlanCandidate,
        catalog: PlanningCatalogSnapshot,
        *,
        plan_id: str | None = None,
        version: int = 1,
    ) -> TestPlanDraft:
        bundle = ApprovedTestDesignBundle.model_validate(bundle.model_dump(mode="json"))
        candidate = PlanCandidate.model_validate(candidate.model_dump(mode="json"))
        catalog = PlanningCatalogSnapshot.model_validate(catalog.model_dump(mode="json"))
        if version < 1:
            raise ValueError("plan version 必须从 1 开始")
        catalog.require_target(
            bundle.design.target.system_id,
            bundle.design.target.environment,
        )
        scenarios = {item.scenario_id: item for item in bundle.design.scenarios}
        if len(scenarios) != len(bundle.design.scenarios):
            raise ValueError("TestDesign.scenario_id 必须唯一")

        candidate_by_scenario = {item.scenario_id: item for item in candidate.flows}
        unknown_scenarios = set(candidate_by_scenario) - set(scenarios)
        if unknown_scenarios:
            raise ValueError(
                f"候选引用了未知 scenario_id: {sorted(unknown_scenarios)}"
            )

        resolved_plan_id = plan_id or f"plan-{bundle.input_snapshot.request_id}"
        approved_knowledge = {
            item.scope_id: item
            for item in bundle.input_snapshot.approved_knowledge
        }
        flows: list[PlanFlow] = []
        flow_number = 0
        for scenario in bundle.design.scenarios:
            flow_candidate = candidate_by_scenario.get(scenario.scenario_id)
            if flow_candidate is None:
                continue
            flow_number += 1
            flow_id = f"FLOW-{flow_number:04d}"
            flows.append(
                self._resolve_flow(
                    scenario,
                    flow_candidate,
                    catalog,
                    approved_knowledge,
                    flow_id=flow_id,
                )
            )

        questions = [
            PlanOpenQuestion(
                question_id=f"PLAN-Q-{index:04d}",
                question=item.question,
                field_path=item.field_path,
            )
            for index, item in enumerate(candidate.open_questions, start=1)
        ]
        plan = TestPlanDraft(
            plan_id=resolved_plan_id,
            version=version,
            design_id=bundle.design.design_id,
            design_version=bundle.design.version,
            design_content_hash=design_hash(bundle),
            design_input_content_hash=bundle.review.input_content_hash,
            catalog_id=catalog.catalog_id,
            catalog_content_hash=catalog.content_hash,
            target_system_id=bundle.design.target.system_id,
            target_environment=bundle.design.target.environment,
            flows=flows,
            open_questions=questions,
        )
        reject_secret_values(plan.model_dump(mode="json"))
        return plan

    def _resolve_flow(
        self,
        scenario: LogicalScenario,
        candidate: PlanFlowCandidate,
        catalog: PlanningCatalogSnapshot,
        approved_knowledge: dict[str, Any],
        *,
        flow_id: str,
    ) -> PlanFlow:
        operations = {item.operation_id: item for item in scenario.operations}
        expected_results = {
            item.expected_result_id: item for item in scenario.expected_results
        }
        required_states = {
            item.required_state_id: item for item in scenario.required_states
        }
        data_requirements = {item.data_id: item for item in scenario.data_requirements}

        selected_operation_ids = {
            item.operation_id for stage in candidate.stages for item in stage.operations
        } | {
            item.operation_id
            for stage in candidate.stages
            for item in stage.database_queries
            if item.operation_id is not None
        }
        selected_expected_ids = {
            item.expected_result_id
            for stage in candidate.stages
            for item in stage.expected_results
        } | {
            item.expected_result_id
            for stage in candidate.stages
            for item in stage.database_queries
        }
        unknown_operations = selected_operation_ids - set(operations)
        unknown_expected = selected_expected_ids - set(expected_results)
        if unknown_operations:
            raise ValueError(f"flow 引用了未知 operation_id: {sorted(unknown_operations)}")
        if unknown_expected:
            raise ValueError(
                f"flow 引用了未知 expected_result_id: {sorted(unknown_expected)}"
            )

        operation_order = {
            item.operation_id: index for index, item in enumerate(scenario.operations)
        }
        expected_order = {
            item.expected_result_id: index
            for index, item in enumerate(scenario.expected_results)
        }
        data_order = {
            item.data_id: index for index, item in enumerate(scenario.data_requirements)
        }
        candidate = candidate.model_copy(
            update={
                "stages": [
                    stage.model_copy(
                        update={
                            "operations": sorted(
                                stage.operations,
                                key=lambda item: operation_order[item.operation_id],
                            ),
                            "expected_results": sorted(
                                stage.expected_results,
                                key=lambda item: expected_order[item.expected_result_id],
                            ),
                            "database_queries": sorted(
                                stage.database_queries,
                                key=lambda item: (
                                    operation_order.get(
                                        item.operation_id,
                                        len(operation_order),
                                    ),
                                    expected_order[item.expected_result_id],
                                ),
                            ),
                            "data_bindings": sorted(
                                stage.data_bindings,
                                key=lambda item: (
                                    data_order.get(item.data_id, len(data_order)),
                                    item.consumer_id,
                                    item.binding_ref,
                                ),
                            ),
                        }
                    )
                    for stage in candidate.stages
                ]
            }
        )

        stage_ids = [
            f"STAGE-{index:04d}"
            for index in range(1, len(candidate.stages) + 1)
        ]
        setup_by_stage: dict[int, list[SetupStageResolutionCandidate]] = {}
        resolutions = []
        for selection in candidate.required_state_resolutions:
            state = required_states.get(selection.required_state_id)
            if state is None:
                raise ValueError(
                    f"required_state resolution 引用了未知 ID: {selection.required_state_id}"
                )
            if isinstance(selection, DataGuaranteeResolutionCandidate):
                if selection.data_id not in data_requirements:
                    raise ValueError(
                        f"data_guarantee 引用了未知 data_id: {selection.data_id}"
                    )
                resolutions.append(
                    PlanDataGuaranteeResolution(
                        required_state_id=state.required_state_id,
                        text=state.text,
                        data_id=selection.data_id,
                    )
                )
            else:
                if selection.stage_index > len(candidate.stages):
                    raise ValueError("setup_stage.stage_index 超出 stages 范围")
                setup_by_stage.setdefault(selection.stage_index, []).append(selection)
                stage_candidate = candidate.stages[selection.stage_index - 1]
                self._require_catalog_resource(
                    stage_candidate.executor_kind,
                    selection.catalog_ref,
                    catalog,
                )
                resolutions.append(
                    PlanSetupStageResolution(
                        required_state_id=state.required_state_id,
                        text=state.text,
                        stage_id=stage_ids[selection.stage_index - 1],
                        catalog_ref=selection.catalog_ref,
                    )
                )

        stages = [
            self._resolve_stage(
                scenario,
                stage_candidate,
                setup_by_stage.get(index, []),
                catalog,
                approved_knowledge,
                stage_id=stage_ids[index - 1],
                order=index,
            )
            for index, stage_candidate in enumerate(candidate.stages, start=1)
        ]
        self._validate_flow_state_effect(scenario, candidate, setup_by_stage, catalog)
        cleanup = self._resolve_cleanup(scenario, candidate, catalog)
        return PlanFlow(
            flow_id=flow_id,
            name=scenario.title,
            scenario_id=scenario.scenario_id,
            techniques=list(scenario.techniques),
            requirement_ids=list(scenario.requirement_ids),
            required_state_resolutions=resolutions,
            stages=stages,
            cleanup=cleanup,
        )

    def _resolve_stage(
        self,
        scenario: LogicalScenario,
        candidate: PlanStageCandidate,
        setup_resolutions: list[SetupStageResolutionCandidate],
        catalog: PlanningCatalogSnapshot,
        approved_knowledge: dict[str, Any],
        *,
        stage_id: str,
        order: int,
    ) -> PlanStage:
        if setup_resolutions and (
            candidate.operations
            or candidate.expected_results
            or candidate.database_queries
        ):
            raise ValueError(
                "setup stage 只能建立 required_state，不能同时承载逻辑 operation/expected"
            )
        if candidate.executor_kind.value not in catalog.available_executors:
            raise ValueError(
                f"catalog 未登记 executor: {candidate.executor_kind.value}"
            )
        if candidate.database_queries:
            if setup_resolutions:
                raise ValueError("AI SQL stage 不能用于建立 required_state")
            return self._resolve_ai_database_stage(
                scenario,
                candidate.database_queries,
                catalog,
                approved_knowledge,
                stage_id=stage_id,
                order=order,
            )
        expected_channel = EXECUTOR_CHANNEL[candidate.executor_kind]
        operation_by_id = {item.operation_id: item for item in scenario.operations}
        expected_by_id = {
            item.expected_result_id: item for item in scenario.expected_results
        }
        operation_order = {
            item.operation_id: index for index, item in enumerate(scenario.operations)
        }
        expected_order = {
            item.expected_result_id: index
            for index, item in enumerate(scenario.expected_results)
        }

        units: list[_ExecutionUnit] = []
        for setup in setup_resolutions:
            resource = self._require_catalog_resource(
                candidate.executor_kind, setup.catalog_ref, catalog
            )
            if getattr(resource, "state_effect", "read_only") == "read_only":
                raise ValueError(
                    "setup_stage 必须使用能建立状态的 catalog resource，不能使用 read_only"
                )
            units.append(
                _ExecutionUnit(
                    source=ExecutionSource(
                        source_kind="required_state",
                        source_id=setup.required_state_id,
                    ),
                    catalog_ref=setup.catalog_ref,
                    action=self._catalog_action(resource),
                    sort_key=(-1, len(units)),
                )
            )

        for selection in candidate.operations:
            logical = operation_by_id[selection.operation_id]
            if logical.channel_hint.value != expected_channel:
                raise ValueError(
                    f"operation {logical.operation_id} 的 channel_hint "
                    f"{logical.channel_hint.value} 与 stage executor {expected_channel} 不一致"
                )
            self._require_catalog_resource(
                candidate.executor_kind, selection.catalog_ref, catalog
            )
            units.append(
                _ExecutionUnit(
                    source=ExecutionSource(
                        source_kind="operation", source_id=logical.operation_id
                    ),
                    catalog_ref=selection.catalog_ref,
                    action=logical.text,
                    sort_key=(operation_order[logical.operation_id], 0),
                )
            )

        for selection in candidate.expected_results:
            expected = expected_by_id[selection.expected_result_id]
            if expected.channel_hint.value != expected_channel:
                raise ValueError(
                    f"expected_result {expected.expected_result_id} 的 channel_hint "
                    f"{expected.channel_hint.value} 与 stage executor {expected_channel} 不一致"
                )
            resource = self._require_catalog_resource(
                candidate.executor_kind, selection.catalog_ref, catalog
            )
            unit = next(
                (
                    item
                    for item in units
                    if item.source.source_kind == "operation"
                    and item.source.source_id == expected.after_operation_id
                    and item.catalog_ref == selection.catalog_ref
                ),
                None,
            )
            if unit is None:
                unit = _ExecutionUnit(
                    source=ExecutionSource(
                        source_kind="expected_result",
                        source_id=expected.expected_result_id,
                    ),
                    catalog_ref=selection.catalog_ref,
                    action=self._catalog_action(resource),
                    sort_key=(
                        operation_order[expected.after_operation_id],
                        expected_order[expected.expected_result_id] + 1,
                    ),
                )
                units.append(unit)
            unit.assertions.append(selection)

        for selection in candidate.data_bindings:
            if selection.data_id not in {
                item.data_id for item in scenario.data_requirements
            }:
                raise ValueError(f"stage 引用了未知 data_id: {selection.data_id}")
            matching_units = [
                unit
                for unit in units
                if unit.source.source_id == selection.consumer_id
                or any(
                    item.expected_result_id == selection.consumer_id
                    for item in unit.assertions
                )
            ]
            if len(matching_units) != 1:
                raise ValueError(
                    f"data binding consumer_id 必须唯一定位 stage 消费点: "
                    f"{selection.consumer_id}"
                )
            matching_units[0].data_selections.append(selection)

        if not units:
            raise ValueError("stage 必须包含逻辑动作、观察动作或 setup 动作")
        units.sort(key=lambda item: item.sort_key)
        for unit in units:
            unit.assertions.sort(
                key=lambda item: expected_order[item.expected_result_id]
            )
        resolver = {
            ExecutorKind.STAGEHAND_AGENT: self._resolve_agent_ui,
            ExecutorKind.HTTP_API: self._resolve_http,
            ExecutorKind.PERFORMANCE: self._resolve_performance,
            ExecutorKind.TCP_PORT: self._resolve_port,
        }[candidate.executor_kind]
        if candidate.executor_kind == ExecutorKind.PERFORMANCE:
            execution = resolver(scenario, units, catalog, candidate.performance_stages)
        elif candidate.executor_kind == ExecutorKind.STAGEHAND_AGENT:
            execution = resolver(
                scenario,
                units,
                catalog,
                candidate.agent_start_url,
            )
        else:
            execution = resolver(scenario, units, catalog)
        return PlanStage(
            stage_id=stage_id,
            order=order,
            executor_kind=candidate.executor_kind,
            operation_ids=[
                item.operation_id
                for item in scenario.operations
                if item.operation_id
                in {selection.operation_id for selection in candidate.operations}
            ],
            expected_result_ids=[
                item.expected_result_id
                for item in scenario.expected_results
                if item.expected_result_id
                in {
                    selection.expected_result_id
                    for selection in candidate.expected_results
                }
            ],
            setup_required_state_ids=[item.required_state_id for item in setup_resolutions],
            data_ids=[
                item.data_id
                for item in scenario.data_requirements
                if item.data_id
                in {selection.data_id for selection in candidate.data_bindings}
            ],
            execution=execution,
        )

    @staticmethod
    def _catalog_action(resource: Any) -> str:
        return getattr(resource, "action", None) or resource.description

    @staticmethod
    def _require_catalog_resource(
        executor_kind: ExecutorKind,
        catalog_ref: str,
        catalog: PlanningCatalogSnapshot,
    ):
        if executor_kind == ExecutorKind.HTTP_API:
            resource = catalog.get_http_operation(catalog_ref)
            expected_type = HttpOperation
        elif executor_kind == ExecutorKind.DATABASE:
            raise ValueError("database stage 必须携带完整 SQL，不能选择 catalog ref")
        elif executor_kind == ExecutorKind.STAGEHAND_AGENT:
            resource = catalog.get_agent_ui_operation(catalog_ref)
            expected_type = AgentUiOperation
        elif executor_kind == ExecutorKind.TCP_PORT:
            resource = catalog.get_tcp_port_probe(catalog_ref)
            expected_type = TcpPortProbe
        else:
            resource = catalog.get_performance_profile(catalog_ref)
            expected_type = PerformanceProfile
        if not isinstance(resource, expected_type):
            raise ValueError(
                f"catalog ref {catalog_ref} 不属于 executor {executor_kind.value}"
            )
        return resource

    @staticmethod
    def _resolve_cleanup(
        scenario: LogicalScenario,
        candidate: PlanFlowCandidate,
        catalog: PlanningCatalogSnapshot,
    ) -> PlanCleanup | None:
        if candidate.cleanup is None:
            return None
        goal = scenario.state_impact.cleanup_goal
        if goal is None or candidate.cleanup.cleanup_goal_id != goal.cleanup_goal_id:
            raise ValueError("cleanup selection 没有绑定当前 scenario.cleanup_goal")
        action = catalog.get_cleanup_action(candidate.cleanup.action_ref)
        if action is None:
            raise ValueError(f"未登记 cleanup action: {candidate.cleanup.action_ref}")
        if not action.always_run:
            raise ValueError("状态恢复 cleanup action 必须支持 always_run")
        selections = {item.slot: item for item in candidate.cleanup.data_bindings}
        required_slots = set(action.required_data_slots)
        if set(selections) != required_slots:
            raise ValueError(
                "cleanup data slot 必须精确覆盖 catalog required_data_slots; "
                f"missing={sorted(required_slots - set(selections))}, "
                f"unknown={sorted(set(selections) - required_slots)}"
            )
        data_by_id = {item.data_id: item for item in scenario.data_requirements}
        bindings: list[CleanupDataBinding] = []
        for slot in action.required_data_slots:
            selection = selections[slot]
            if selection.data_id not in data_by_id:
                raise ValueError(f"cleanup 引用了未知 data_id: {selection.data_id}")
            if selection.data_id not in goal.subject_data_ids:
                raise ValueError(
                    f"cleanup data_id 不属于 cleanup_goal.subject_data_ids: {selection.data_id}"
                )
            binding = catalog.get_data_binding(selection.binding_ref)
            if binding is None or (
                binding.operation_ref != action.action_ref
                or binding.executor_kind != action.handler_kind
                or slot not in binding.input_refs
            ):
                raise ValueError(
                    f"cleanup binding {selection.binding_ref} 不能提供 slot {slot}"
                )
            bindings.append(
                CleanupDataBinding(
                    slot=slot,
                    data_id=selection.data_id,
                    binding_ref=binding.binding_ref,
                    variable_ref=binding.input_refs[slot],
                )
            )
        if {item.data_id for item in bindings} != set(goal.subject_data_ids):
            raise ValueError(
                "cleanup data bindings 必须精确覆盖 cleanup_goal.subject_data_ids"
            )
        return PlanCleanup(
            cleanup_goal_id=goal.cleanup_goal_id,
            action_ref=action.action_ref,
            handler_kind=action.handler_kind,
            policy=action.policy,
            target=action.target,
            always_run=action.always_run,
            evidence_required=action.evidence_required,
            data_bindings=bindings,
        )

    def _validate_flow_state_effect(
        self,
        scenario: LogicalScenario,
        candidate: PlanFlowCandidate,
        setup_by_stage: dict[int, list[SetupStageResolutionCandidate]],
        catalog: PlanningCatalogSnapshot,
    ) -> None:
        effects: list[str] = []
        for index, stage in enumerate(candidate.stages, start=1):
            if stage.database_queries:
                effects.append("read_only")
            refs = [item.catalog_ref for item in stage.operations]
            refs.extend(item.catalog_ref for item in stage.expected_results)
            refs.extend(item.catalog_ref for item in setup_by_stage.get(index, []))
            for ref in refs:
                resource = self._require_catalog_resource(
                    stage.executor_kind, ref, catalog
                )
                effects.append(resource.state_effect)
        aggregate = self._aggregate_state_effect(effects)
        if "unknown" not in effects and aggregate != scenario.state_impact.impact.value:
            raise ValueError(
                "第一层 state_impact 与 flow 全部 stage 的 catalog state_effect 聚合结果冲突"
            )

    @staticmethod
    def _aggregate_state_effect(effects: list[str]) -> str:
        if "changes_state" in effects:
            return "changes_state"
        if "creates_data" in effects:
            return "creates_data"
        return "read_only"

    def _bound_data(
        self,
        unit: _ExecutionUnit,
        executor_kind: ExecutorKind,
        catalog: PlanningCatalogSnapshot,
    ) -> list[BoundData]:
        values: list[BoundData] = []
        for selection in unit.data_selections:
            binding = catalog.get_data_binding(selection.binding_ref)
            if binding is None:
                raise ValueError(f"未登记 data binding: {selection.binding_ref}")
            if (
                binding.executor_kind != executor_kind.value
                or binding.operation_ref != unit.catalog_ref
            ):
                raise ValueError(
                    f"data binding {binding.binding_ref} 不属于 {unit.catalog_ref}"
                )
            values.append(
                BoundData(
                    data_id=selection.data_id,
                    consumer_id=selection.consumer_id,
                    binding_ref=binding.binding_ref,
                    input_refs=dict(binding.input_refs),
                )
            )
        return values

    @staticmethod
    def _assertion(
        scenario: LogicalScenario,
        selection: ExpectedResultSelection,
        observable: Any,
    ) -> BoundAssertion:
        expected = next(
            item
            for item in scenario.expected_results
            if item.expected_result_id == selection.expected_result_id
        )
        return BoundAssertion(
            expected_result_id=expected.expected_result_id,
            after_operation_id=expected.after_operation_id,
            observable_ref=selection.observable_ref,
            kind=observable.kind if hasattr(observable, "kind") else "agent_ui",
            statement=expected.text,
            operator=expected.operator,
            expected=expected.expected,
            unit=expected.unit,
            path=getattr(observable, "path", None),
            name=getattr(observable, "name", None),
            column=getattr(observable, "column", None),
            metric=getattr(observable, "metric", None),
            percentile=getattr(observable, "percentile", None),
        )

    def _resolve_http(
        self, scenario: LogicalScenario, units: list[_ExecutionUnit], catalog
    ) -> HttpExecution:
        requests: list[HttpRequestPlan] = []
        base_urls: set[str] = set()
        for index, unit in enumerate(units, start=1):
            operation = catalog.get_http_operation(unit.catalog_ref)
            if not isinstance(operation, HttpOperation):
                raise ValueError(f"HTTP stage 引用了非 HTTP operation: {unit.catalog_ref}")
            base_urls.add(operation.base_url_ref)
            allowed = {item.observable_ref: item for item in operation.observables}
            assertions = []
            for selection in unit.assertions:
                observable = allowed.get(selection.observable_ref)
                if not isinstance(observable, HttpObservable):
                    raise ValueError(
                        f"observable {selection.observable_ref} 不属于 {operation.operation_ref}"
                    )
                assertions.append(self._assertion(scenario, selection, observable))
            requests.append(
                HttpRequestPlan(
                    request_id=f"HTTP-{index:04d}",
                    source=unit.source,
                    operation_ref=operation.operation_ref,
                    action=unit.action,
                    method=operation.method,
                    path=operation.path,
                    data_bindings=self._bound_data(
                        unit, ExecutorKind.HTTP_API, catalog
                    ),
                    assertions=assertions,
                )
            )
        if len(base_urls) != 1:
            raise ValueError("一个 HTTP stage 必须使用同一 base_url_ref")
        return HttpExecution(base_url_ref=base_urls.pop(), requests=requests)

    def _resolve_agent_ui(
        self,
        scenario: LogicalScenario,
        units: list[_ExecutionUnit],
        catalog: PlanningCatalogSnapshot,
        start_url: str | None,
    ) -> AgentUiExecution:
        rows: list[AgentUiPlanRow] = []
        profiles: dict[str, AgentUiCapabilityProfile] = {}
        for index, unit in enumerate(units, start=1):
            operation = catalog.get_agent_ui_operation(unit.catalog_ref)
            if not isinstance(operation, AgentUiOperation):
                raise ValueError(
                    f"Agent UI stage 引用了非 Agent UI operation: {unit.catalog_ref}"
                )
            profile = next(
                (
                    item
                    for item in catalog.agent_ui_profiles
                    if any(
                        candidate.operation_ref == operation.operation_ref
                        for candidate in item.operations
                    )
                ),
                None,
            )
            if not isinstance(profile, AgentUiCapabilityProfile):
                raise ValueError(f"Agent UI operation 没有所属 profile: {unit.catalog_ref}")
            profiles[profile.profile_ref] = profile
            allowed = {item.observable_ref: item for item in profile.observables}
            assertions: list[BoundAssertion] = []
            for selection in unit.assertions:
                observable = allowed.get(selection.observable_ref)
                if not isinstance(observable, AgentUiObservable):
                    raise ValueError(
                        f"observable {selection.observable_ref} 不属于 {profile.profile_ref}"
                    )
                assertions.append(self._assertion(scenario, selection, observable))
            rows.append(
                AgentUiPlanRow(
                    row_id=f"AGENT-UI-{index:04d}",
                    source=unit.source,
                    operation_ref=operation.operation_ref,
                    action=unit.action,
                    assertions=assertions,
                )
            )
        if len(profiles) != 1:
            raise ValueError("一个 Agent UI stage 必须使用同一个资产 profile")
        profile = next(iter(profiles.values()))
        return AgentUiExecution(
            capability_profile_ref=profile.profile_ref,
            start_url=start_url or profile.start_url,
            max_steps=profile.max_steps,
            rows=rows,
        )

    def _resolve_ai_database_stage(
        self,
        scenario: LogicalScenario,
        drafts: list[DatabaseQueryDraftCandidate],
        catalog: PlanningCatalogSnapshot,
        approved_knowledge: dict[str, Any],
        *,
        stage_id: str,
        order: int,
    ) -> PlanStage:
        schema = catalog.get_database_schema()
        if schema is None:
            raise ValueError("数据库资源未登记可供 AI 生成 SQL 的表和字段")
        operations_by_id = {
            item.operation_id: item for item in scenario.operations
        }
        expected_by_id = {
            item.expected_result_id: item
            for item in scenario.expected_results
        }
        allowed_tables = [item.name for item in schema.tables]
        allowed_columns = {
            column.name
            for table in schema.tables
            for column in table.columns
        }
        operations: list[DatabaseOperationPlan] = []
        for index, draft in enumerate(drafts, start=1):
            expected = expected_by_id.get(draft.expected_result_id)
            if expected is None:
                raise ValueError(
                    f"AI SQL 引用了未知 expected_result_id: {draft.expected_result_id}"
                )
            if expected.channel_hint.value != "database":
                raise ValueError(
                    f"AI SQL 只能负责 database 预期: {expected.expected_result_id}"
                )
            logical_operation = None
            if draft.operation_id is not None:
                logical_operation = operations_by_id.get(draft.operation_id)
                if logical_operation is None:
                    raise ValueError(
                        f"AI SQL 引用了未知 operation_id: {draft.operation_id}"
                    )
                if logical_operation.channel_hint.value != "database":
                    raise ValueError(
                        f"AI SQL 只能负责 database 操作: {draft.operation_id}"
                    )
                if expected.after_operation_id != logical_operation.operation_id:
                    raise ValueError(
                        "AI SQL operation_id 必须是预期结果关联的业务操作"
                    )
            if draft.execution_policy == "write" and logical_operation is None:
                raise ValueError("数据库写 SQL 必须绑定已审批的 database operation")
            if (
                draft.execution_policy == "write"
                and scenario.state_impact.impact.value == "read_only"
            ):
                raise ValueError("read_only 场景不能生成数据库写 SQL")
            if (
                draft.execution_policy == "write"
                and draft.check_kind != "affected_rows"
            ):
                raise ValueError("数据库写 SQL 必须使用 affected_rows 检查受影响行数")
            if (
                draft.execution_policy == "read_only"
                and draft.check_kind == "affected_rows"
            ):
                raise ValueError("只读 SQL 不能使用 affected_rows 检查")
            normalized_sql = validate_database_sql(
                draft.sql,
                execution_policy=draft.execution_policy,
                allowed_tables=allowed_tables,
                allowed_columns={
                    table.name: [column.name for column in table.columns]
                    for table in schema.tables
                },
                allowed_parameter_refs=schema.allowed_parameter_refs,
                parameters_refs=draft.parameters_refs,
            )
            if (
                draft.check_kind == "column"
                and draft.check_column not in allowed_columns
                and not re.search(
                    rf"(?i)\bas\s+{re.escape(str(draft.check_column))}\b",
                    normalized_sql,
                )
            ):
                raise ValueError(
                    f"AI SQL 检查列未在资源字段或 SQL 别名中登记: "
                    f"{draft.check_column}"
                )
            if expected.operator is not None and expected.operator != draft.operator:
                raise ValueError(
                    f"AI SQL 检查操作符不能改写已审批预期: "
                    f"{expected.operator} != {draft.operator}"
                )
            if expected.expected is not None and expected.expected != draft.expected:
                raise ValueError("AI SQL 检查值不能改写已审批预期")

            sql_origin = "ai_generated"
            knowledge_scope_id = draft.knowledge_scope_id
            if knowledge_scope_id is not None:
                knowledge = approved_knowledge.get(knowledge_scope_id)
                if knowledge is None:
                    raise ValueError(
                        f"AI SQL 引用了未审批知识范围: {knowledge_scope_id}"
                    )
                normalized_knowledge = " ".join(
                    str(knowledge.content).lower().split()
                )
                if " ".join(normalized_sql.lower().split()) not in normalized_knowledge:
                    raise ValueError(
                        "标记为知识库复用的 SQL 必须完整存在于对应已审批知识内容"
                    )
                sql_origin = "knowledge_reused"

            operation_ref = f"ai.sql.{stage_id.lower()}.{index:04d}"
            assertion = BoundAssertion(
                expected_result_id=expected.expected_result_id,
                after_operation_id=expected.after_operation_id,
                observable_ref=f"{operation_ref}.check",
                kind=draft.check_kind,
                statement=expected.text,
                operator=expected.operator or draft.operator,
                expected=(
                    expected.expected
                    if expected.expected is not None
                    else draft.expected
                ),
                unit=expected.unit,
                column=draft.check_column,
            )
            operations.append(
                DatabaseOperationPlan(
                    operation_run_id=f"DB-{index:04d}",
                    source=ExecutionSource(
                        source_kind=(
                            "operation"
                            if logical_operation is not None
                            else "expected_result"
                        ),
                        source_id=(
                            logical_operation.operation_id
                            if logical_operation is not None
                            else expected.expected_result_id
                        ),
                    ),
                    operation_ref=operation_ref,
                    action=(
                        logical_operation.text
                        if logical_operation is not None
                        else f"执行 SQL 验证：{expected.text}"
                    ),
                    operation_kind="statement",
                    execution_policy=draft.execution_policy,
                    sql=normalized_sql,
                    parameters_refs=dict(draft.parameters_refs),
                    sql_origin=sql_origin,
                    knowledge_scope_id=knowledge_scope_id,
                    assertions=[assertion],
                )
            )
        return PlanStage(
            stage_id=stage_id,
            order=order,
            executor_kind=ExecutorKind.DATABASE,
            operation_ids=[
                item.operation_id
                for item in scenario.operations
                if item.operation_id
                in {
                    draft.operation_id
                    for draft in drafts
                    if draft.operation_id is not None
                }
            ],
            expected_result_ids=[
                item.expected_result_id
                for item in scenario.expected_results
                if item.expected_result_id
                in {draft.expected_result_id for draft in drafts}
            ],
            setup_required_state_ids=[],
            data_ids=[],
            execution=DatabaseExecution(
                connection_profile_ref=schema.connection_profile_ref,
                operations=operations,
            ),
        )

    def _resolve_performance(
        self,
        scenario: LogicalScenario,
        units: list[_ExecutionUnit],
        catalog,
        stages: list[LoadStage],
    ) -> PerformanceExecution:
        profiles: dict[str, PerformanceProfile] = {}
        for unit in units:
            profile = catalog.get_performance_profile(unit.catalog_ref)
            if not isinstance(profile, PerformanceProfile):
                raise ValueError(
                    f"performance stage 引用了非 performance profile: {unit.catalog_ref}"
                )
            profiles[profile.profile_ref] = profile
        if len(profiles) != 1:
            raise ValueError("一个 performance stage 只能绑定一个 performance profile")
        profile = next(iter(profiles.values()))
        if (
            sum(stage.duration_seconds for stage in stages)
            > profile.max_duration_seconds
            or any(
                stage.virtual_users > profile.max_virtual_users
                for stage in stages
            )
        ):
            raise ValueError("performance_stages 超出所选 performance profile 的执行上限")
        allowed = {item.observable_ref: item for item in profile.observables}
        thresholds: list[PerformanceThreshold] = []
        data: list[BoundData] = []
        threshold_number = 0
        for unit in units:
            data.extend(self._bound_data(unit, ExecutorKind.PERFORMANCE, catalog))
            for selection in unit.assertions:
                observable = allowed.get(selection.observable_ref)
                if not isinstance(observable, PerformanceObservable):
                    raise ValueError(
                        f"observable {selection.observable_ref} 不属于 {profile.profile_ref}"
                    )
                expected = next(
                    item
                    for item in scenario.expected_results
                    if item.expected_result_id == selection.expected_result_id
                )
                if (
                    not isinstance(expected.expected, (int, float))
                    or isinstance(expected.expected, bool)
                    or expected.operator not in {"lte", "lt", "gte", "gt", "equals"}
                ):
                    raise ValueError(
                        "performance expected result 必须提供数字和受支持的 operator"
                    )
                if expected.unit and observable.unit and expected.unit != observable.unit:
                    raise ValueError(
                        "performance expected unit 与 catalog observable unit 冲突"
                    )
                threshold_number += 1
                thresholds.append(
                    PerformanceThreshold(
                        threshold_id=f"THRESHOLD-{threshold_number:04d}",
                        expected_result_id=expected.expected_result_id,
                        after_operation_id=expected.after_operation_id,
                        observable_ref=observable.observable_ref,
                        metric=observable.metric,
                        operator=expected.operator,
                        value=float(expected.expected),
                        unit=expected.unit or observable.unit,
                        percentile=observable.percentile,
                    )
                )
        return PerformanceExecution(
            profile_ref=profile.profile_ref,
            driver_ref=profile.driver_ref,
            sources=[item.source for item in units],
            stages=list(stages),
            data_bindings=data,
            thresholds=thresholds,
        )

    def _resolve_port(
        self, scenario: LogicalScenario, units: list[_ExecutionUnit], catalog
    ) -> PortExecution:
        probes: list[PortProbePlan] = []
        for index, unit in enumerate(units, start=1):
            if unit.data_selections:
                raise ValueError(
                    "TCP port probe 不接受 data binding；host/port 必须来自 catalog"
                )
            probe = catalog.get_tcp_port_probe(unit.catalog_ref)
            if not isinstance(probe, TcpPortProbe):
                raise ValueError(f"TCP port stage 引用了非 TCP port probe: {unit.catalog_ref}")
            allowed = {item.observable_ref: item for item in probe.observables}
            assertions: list[BoundAssertion] = []
            for selection in unit.assertions:
                observable = allowed.get(selection.observable_ref)
                if not isinstance(observable, PortObservable):
                    raise ValueError(
                        f"observable {selection.observable_ref} 不属于 {probe.probe_ref}"
                    )
                expected = next(
                    item
                    for item in scenario.expected_results
                    if item.expected_result_id == selection.expected_result_id
                )
                if observable.kind == "state":
                    if expected.expected not in {"open", "closed", "filtered"}:
                        raise ValueError(
                            "TCP port state 断言 expected 必须是 open、closed 或 filtered"
                        )
                    if expected.operator not in {"equals", "not_equals"}:
                        raise ValueError(
                            "TCP port state 断言 operator 必须是 equals 或 not_equals"
                        )
                    if expected.unit is not None:
                        raise ValueError("TCP port state 断言不能携带 unit")
                else:
                    records_observation = (
                        expected.operator is None and expected.expected is None
                    )
                    compares_value = (
                        isinstance(expected.expected, (int, float))
                        and not isinstance(expected.expected, bool)
                        and expected.expected >= 0
                        and expected.operator in {"equals", "lte", "lt", "gte", "gt"}
                    )
                    if not (records_observation or compares_value):
                        raise ValueError(
                            "TCP port latency 断言必须提供非负数字和受支持的 operator"
                        )
                    if expected.unit not in {None, "ms"}:
                        raise ValueError(
                            "TCP port latency 断言 unit 必须为 ms 或省略"
                        )
                assertion = self._assertion(scenario, selection, observable)
                if (
                    observable.kind == "connect_latency_ms"
                    and expected.operator is None
                    and expected.expected is None
                ):
                    assertion = assertion.model_copy(update={"operator": "exists"})
                assertions.append(assertion)
            probes.append(
                PortProbePlan(
                    probe_run_id=f"PORT-{index:04d}",
                    source=unit.source,
                    probe_ref=probe.probe_ref,
                    action=(
                        unit.action
                        if unit.source.source_kind == "operation"
                        else self._catalog_action(probe)
                    ),
                    host_ref=probe.host_ref,
                    port=probe.port,
                    timeout_seconds=probe.timeout_seconds,
                    assertions=assertions,
                )
            )
        return PortExecution(probes=probes)

    def compile(
        self,
        bundle: ApprovedTestDesignBundle,
        plan: TestPlanDraft,
        catalog: PlanningCatalogSnapshot,
        output_root: str | Path,
    ) -> PlanCompilationResult:
        bundle = ApprovedTestDesignBundle.model_validate(bundle.model_dump(mode="json"))
        plan = TestPlanDraft.model_validate(plan.model_dump(mode="json"))
        catalog = PlanningCatalogSnapshot.model_validate(catalog.model_dump(mode="json"))
        validation = self.validator.validate(bundle, plan, catalog)
        if not validation.passed:
            reasons = [item.message for item in validation.findings if item.blocking]
            return PlanCompilationResult(
                plan=plan.model_copy(
                    update={"status": PlanStatus.BLOCKED, "blocked_reasons": reasons}
                ),
                validation=validation,
            )

        root = Path(output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        categories = sorted(
            {artifact_category(stage.executor_kind) for flow in plan.flows for stage in flow.stages}
        )
        final_roots = [
            (root / "generated-files" / category / plan.plan_id / f"v{plan.version}").resolve()
            for category in categories
        ]
        for final_root in final_roots:
            try:
                final_root.relative_to(root)
            except ValueError as exc:
                raise ValueError("计划产物目录越过 output_root") from exc
            if final_root.exists():
                raise ValueError(f"计划产物目录已存在，拒绝覆盖: {final_root}")

        artifacts: list[ExecutorArtifactBundle] = []
        with tempfile.TemporaryDirectory(dir=root, prefix=".planning-staging-") as staging:
            staging_root = Path(staging)
            for flow in plan.flows:
                for stage in flow.stages:
                    adapter = self._adapter_for(stage.executor_kind)
                    artifacts.append(
                        adapter.compile(
                            bundle,
                            plan,
                            flow,
                            stage,
                            catalog,
                            generated_files_root(staging_root, stage.executor_kind),
                        )
                    )
            for category, final_root in zip(categories, final_roots, strict=True):
                staged_root = (
                    staging_root
                    / "generated-files"
                    / category
                    / plan.plan_id
                    / f"v{plan.version}"
                )
                final_root.parent.mkdir(parents=True, exist_ok=True)
                staged_root.rename(final_root)
        return PlanCompilationResult(
            plan=plan,
            validation=validation,
            artifacts=artifacts,
        )

    def review(
        self,
        result: PlanCompilationResult,
        decision: PlanReviewDecision,
        comments: str,
    ) -> tuple[TestPlanDraft, PlanReview]:
        validation = PlanValidationReport.model_validate(
            result.validation.model_dump(mode="json")
        )
        revision_key = (
            result.plan.plan_id,
            result.plan.version,
            result.plan.content_hash(),
        )
        if (
            validation.plan_id != result.plan.plan_id
            or validation.plan_version != result.plan.version
            or validation.plan_content_hash != result.plan.content_hash()
        ):
            raise ValueError("计划审核不能绑定其他计划或版本的校验报告")
        if decision == PlanReviewDecision.APPROVED:
            if not validation.passed:
                raise ValueError("存在阻塞校验问题，不能批准计划")
            if not result.artifacts:
                raise ValueError("没有编译产物，不能批准计划")
        status = {
            PlanReviewDecision.APPROVED: PlanStatus.APPROVED,
            PlanReviewDecision.CHANGES_REQUESTED: PlanStatus.CHANGES_REQUESTED,
            PlanReviewDecision.REJECTED: PlanStatus.REJECTED,
        }[decision]
        updated = result.plan.model_copy(update={"status": status})
        review_payload = {
            "review_id": f"plan-review-{uuid4().hex}",
            "plan_id": updated.plan_id,
            "plan_version": updated.version,
            "decision": decision,
            "comments": comments,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "plan_content_hash": updated.content_hash(),
            "validation_content_hash": compute_plan_validation_content_hash(
                validation
            ),
            "artifact_set_hash": compute_artifact_set_hash(result.artifacts),
        }
        review_payload["review_content_hash"] = compute_plan_review_content_hash(
            review_payload
        )
        review = PlanReview.model_validate(review_payload)
        if result.plan.status not in {
            PlanStatus.DRAFT,
            PlanStatus.IN_REVIEW,
            PlanStatus.CHANGES_REQUESTED,
        }:
            raise ValueError("已结束的 plan revision 不能重复审核")
        if (
            result.plan.status == PlanStatus.CHANGES_REQUESTED
            and decision == PlanReviewDecision.APPROVED
        ):
            raise ValueError(
                "changes_requested 版本不能原地批准；必须生成更高版本并重新编译审核"
            )
        if revision_key in self._reviewed_revisions:
            raise ValueError(
                "同一 plan/version/content 已审核，必须重新编译更高版本后再审核"
            )
        self._reviewed_revisions.add(revision_key)
        # Consume the compilation result just like the first-layer generation
        # result, so replaying the same object cannot resurrect a draft status.
        result.plan = updated
        return updated, review

    def build_approved_bundle(
        self,
        result: PlanCompilationResult,
        approved_plan: TestPlanDraft,
        review: PlanReview,
        catalog: PlanningCatalogSnapshot,
    ) -> ApprovedTestPlanBundle:
        return ApprovedTestPlanBundle(
            plan=approved_plan,
            validation=result.validation,
            review=review,
            catalog_snapshot=catalog,
            compiled_artifacts=result.artifacts,
        )


__all__ = ["PlanCompilationResult", "TestPlanCompiler"]
