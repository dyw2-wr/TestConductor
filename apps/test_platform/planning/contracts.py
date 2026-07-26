"""第二层 TestPlan v4 契约。

第一层决定测什么，PlanningCatalog 声明目标环境可用的受控能力。第二层模型
生成受资源约束的线性执行计划；确定性 compiler 再把每个 stage 编译成单一
executor 的 typed execution。模型不能编造 locator、HTTP 地址、driver 或凭据。
"""

from __future__ import annotations

from enum import Enum
import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.test_platform.intent.contracts import (
    ApprovedTestDesignBundle,
    DesignStatus,
    contains_secret_literal,
)
from apps.test_platform.database_sql import validate_read_only_sql

from .catalogs import LoadStage, PlanningCatalogSnapshot


_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,191}$")


def _require_safe_artifact_id(value: str, field_name: str) -> None:
    if not _SAFE_ARTIFACT_ID.fullmatch(value):
        raise ValueError(
            f"{field_name} 只能使用 1-192 位 ASCII 字母、数字、点、下划线、@ 或连字符"
        )


class StrictPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PlanStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class PlanReviewDecision(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class ExecutorKind(str, Enum):
    PROCEDURE_PLAYWRIGHT = "procedure_playwright"
    HTTP_API = "http_api"
    DATABASE = "database"
    PERFORMANCE = "performance"
    TCP_PORT = "tcp_port"


class ModelMessage(StrictPlanModel):
    role: Literal["system", "user"]
    content: str


class OperationSelection(StrictPlanModel):
    """把一个第一层逻辑动作绑定到 stage 内的 catalog operation/profile。"""

    operation_id: str
    catalog_ref: str


class ExpectedResultSelection(StrictPlanModel):
    """为业务预期选择观察动作和 observable。

    catalog_ref 可以不同于触发该预期的逻辑动作。例如 UI 动作之后，database
    stage 可以选择一个只读查询作为观察动作。
    """

    expected_result_id: str
    catalog_ref: str
    observable_ref: str


class DatabaseQueryDraftCandidate(StrictPlanModel):
    """AI-authored read-only SQL proposed for execution-plan approval."""

    expected_result_id: str
    operation_id: str | None = None
    sql: str
    parameters_refs: dict[str, str] = Field(default_factory=dict)
    check_kind: Literal["row_count", "column", "exists"]
    check_column: str | None = None
    operator: Literal[
        "equals",
        "not_equals",
        "contains",
        "exists",
        "not_exists",
        "gt",
        "gte",
        "lt",
        "lte",
        "not_null",
        "null",
    ]
    expected: Any | None = None
    knowledge_scope_id: str | None = None

    @model_validator(mode="after")
    def validate_draft(self) -> "DatabaseQueryDraftCandidate":
        validate_read_only_sql(
            self.sql,
            parameters_refs=self.parameters_refs,
        )
        if self.check_kind == "column":
            if not self.check_column:
                raise ValueError("column 检查必须填写 check_column")
        elif self.check_column is not None:
            raise ValueError("只有 column 检查可以填写 check_column")
        if self.operator in {"not_null", "null", "exists", "not_exists"}:
            if self.expected is not None:
                raise ValueError(f"{self.operator} 检查不能填写 expected")
        elif self.expected is None:
            raise ValueError(f"{self.operator} 检查必须填写 expected")
        return self


class DataBindingSelection(StrictPlanModel):
    """把数据需求绑定到 stage 内的消费点。

    consumer_id 必须是本 flow 中的 operation_id、expected_result_id 或由本 stage
    建立的 required_state_id；这样同一 catalog_ref 多次出现时也不会产生歧义。
    """

    data_id: str
    consumer_id: str
    binding_ref: str


class CleanupDataBindingSelection(StrictPlanModel):
    slot: str
    data_id: str
    binding_ref: str


class CleanupSelection(StrictPlanModel):
    cleanup_goal_id: str
    action_ref: str
    data_bindings: list[CleanupDataBindingSelection] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_slots(self) -> "CleanupSelection":
        slots = [item.slot for item in self.data_bindings]
        if len(set(slots)) != len(slots):
            raise ValueError("cleanup data_bindings.slot 不能重复")
        return self


class DataGuaranteeResolutionCandidate(StrictPlanModel):
    resolution_kind: Literal["data_guarantee"] = "data_guarantee"
    required_state_id: str
    data_id: str


class SetupStageResolutionCandidate(StrictPlanModel):
    resolution_kind: Literal["setup_stage"] = "setup_stage"
    required_state_id: str
    stage_index: int = Field(ge=1)
    catalog_ref: str


RequiredStateResolutionCandidate = Annotated[
    DataGuaranteeResolutionCandidate | SetupStageResolutionCandidate,
    Field(discriminator="resolution_kind"),
]


class PlanStageCandidate(StrictPlanModel):
    """模型给出的单 executor 阶段，列表顺序就是执行顺序。"""

    executor_kind: ExecutorKind
    operations: list[OperationSelection] = Field(default_factory=list)
    expected_results: list[ExpectedResultSelection] = Field(default_factory=list)
    database_queries: list[DatabaseQueryDraftCandidate] = Field(default_factory=list)
    performance_stages: list[LoadStage] = Field(default_factory=list)
    data_bindings: list[DataBindingSelection] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_ownership(self) -> "PlanStageCandidate":
        if self.performance_stages and self.executor_kind != ExecutorKind.PERFORMANCE:
            raise ValueError("performance_stages 只能用于 performance stage")
        if self.executor_kind == ExecutorKind.PERFORMANCE and not self.performance_stages:
            raise ValueError("performance stage 必须生成 performance_stages")
        if self.database_queries:
            if self.executor_kind != ExecutorKind.DATABASE:
                raise ValueError("database_queries 只能用于 database stage")
            if self.operations or self.expected_results or self.data_bindings:
                raise ValueError(
                    "AI SQL stage 不能混用 catalog operations/expected_results/data_bindings"
                )
        for label, values in (
            ("operations", [item.operation_id for item in self.operations]),
            (
                "expected_results",
                [
                    *[item.expected_result_id for item in self.expected_results],
                    *[item.expected_result_id for item in self.database_queries],
                ],
            ),
            (
                "database operation",
                [
                    item.operation_id
                    for item in self.database_queries
                    if item.operation_id is not None
                ],
            ),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"stage.{label} 不能重复第一层 ID")
        binding_keys = [
            (item.data_id, item.consumer_id, item.binding_ref)
            for item in self.data_bindings
        ]
        if len(set(binding_keys)) != len(binding_keys):
            raise ValueError("stage.data_bindings 不能重复")
        return self


class PlanFlowCandidate(StrictPlanModel):
    """一个第一层 scenario 对应一个线性 flow。"""

    scenario_id: str
    stages: list[PlanStageCandidate] = Field(min_length=1)
    required_state_resolutions: list[RequiredStateResolutionCandidate] = Field(
        default_factory=list
    )
    cleanup: CleanupSelection | None = None

    @model_validator(mode="after")
    def require_consistent_flow_refs(self) -> "PlanFlowCandidate":
        operation_ids = [
            item.operation_id
            for stage in self.stages
            for item in stage.operations
        ] + [
            item.operation_id
            for stage in self.stages
            for item in stage.database_queries
            if item.operation_id is not None
        ]
        expected_ids = [
            item.expected_result_id
            for stage in self.stages
            for item in stage.expected_results
        ] + [
            item.expected_result_id
            for stage in self.stages
            for item in stage.database_queries
        ]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("同一 operation_id 只能由一个 stage 负责")
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("同一 expected_result_id 只能由一个 stage 负责")
        state_ids = [item.required_state_id for item in self.required_state_resolutions]
        if len(set(state_ids)) != len(state_ids):
            raise ValueError("required_state 只能解析一次")
        invalid_indexes = {
            item.stage_index
            for item in self.required_state_resolutions
            if isinstance(item, SetupStageResolutionCandidate)
            and item.stage_index > len(self.stages)
        }
        if invalid_indexes:
            raise ValueError(
                f"setup_stage.stage_index 超出 stages 范围: {sorted(invalid_indexes)}"
            )
        setup_stage_indexes = {
            item.stage_index
            for item in self.required_state_resolutions
            if isinstance(item, SetupStageResolutionCandidate)
        }
        empty_stages = {
            index
            for index, item in enumerate(self.stages, start=1)
            if not item.operations
            and not item.expected_results
            and not item.database_queries
            and index not in setup_stage_indexes
        }
        if empty_stages:
            raise ValueError(
                f"stage 没有动作、断言或 setup 责任，stage_index: {sorted(empty_stages)}"
            )
        return self


class PlanOpenQuestionCandidate(StrictPlanModel):
    question: str
    field_path: str | None = None


class PlanCandidate(StrictPlanModel):
    flows: list[PlanFlowCandidate] = Field(default_factory=list)
    open_questions: list[PlanOpenQuestionCandidate] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_common_model_nesting(cls, value: Any) -> Any:
        """Accept two harmless nesting mistakes made by planning models.

        The public candidate schema keeps state resolution on a flow and
        blocking questions on the plan.  Some compatible models place those
        already-defined fields on the stage/flow that produced them.  Promote
        only these known fields before strict validation; every other unknown
        field still fails with ``extra_forbidden``.
        """

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        top_questions = list(normalized.get("open_questions") or [])
        # A few providers return a single blocking question instead of the
        # documented array when they cannot form an executable flow.
        if "question" in normalized and not top_questions and not normalized.get("flows"):
            top_questions.append({"question": normalized.pop("question")})
        flows = []
        for raw_flow in normalized.get("flows") or []:
            if not isinstance(raw_flow, dict):
                flows.append(raw_flow)
                continue
            flow = dict(raw_flow)
            top_questions.extend(flow.pop("open_questions", []) or [])
            resolutions = list(flow.get("required_state_resolutions") or [])
            stages = []
            for stage_index, raw_stage in enumerate(flow.get("stages") or [], start=1):
                if not isinstance(raw_stage, dict):
                    stages.append(raw_stage)
                    continue
                stage = dict(raw_stage)
                nested = stage.pop("required_state_resolutions", []) or []
                for item in nested:
                    if isinstance(item, dict):
                        item = dict(item)
                        if (
                            item.get("resolution_kind") == "setup_stage"
                            and "stage_index" not in item
                        ):
                            item["stage_index"] = stage_index
                    resolutions.append(item)
                stages.append(stage)
            flow["stages"] = stages
            if resolutions:
                flow["required_state_resolutions"] = resolutions
            flows.append(flow)
        normalized["flows"] = flows
        normalized["open_questions"] = top_questions
        return normalized

    @model_validator(mode="after")
    def require_flow_or_question(self) -> "PlanCandidate":
        if not self.flows and not self.open_questions:
            raise ValueError("PlanCandidate 必须包含 flow 或 open_question")
        scenario_ids = [item.scenario_id for item in self.flows]
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("每个 scenario 只能生成一个 flow")
        return self


class PlanOpenQuestion(StrictPlanModel):
    question_id: str
    question: str
    field_path: str | None = None
    blocking: Literal[True] = True


class BoundData(StrictPlanModel):
    data_id: str
    consumer_id: str
    binding_ref: str
    input_refs: dict[str, str]


def format_procedure_input_data(bindings: list[BoundData]) -> str | None:
    """Render catalog bindings in the assignment syntax consumed by Procedure.

    Procedure WorkbookV2 parses ``Input Data`` as semicolon/newline separated
    ``name=value`` assignments. TestConductor keeps variable references as
    placeholders so the external runner can resolve their values at runtime.
    Sorting makes the workbook and validator deterministic.
    """

    assignments: dict[str, str] = {}
    normalized_names: dict[str, str] = {}
    for binding in bindings:
        for key, variable_ref in binding.input_refs.items():
            name = key.removeprefix("input.")
            if not name:
                raise ValueError("UI 输入数据项不能为空")
            normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
            if not normalized:
                raise ValueError(f"UI 输入数据项无法归一化: {name}")
            if normalized in normalized_names:
                raise ValueError(f"UI 输入数据项重复绑定: {name}")
            normalized_names[normalized] = name
            assignments[name] = "{" + variable_ref + "}"
    if not assignments:
        return None
    return "; ".join(
        f"{key}={value}" for key, value in sorted(assignments.items())
    )


class BoundAssertion(StrictPlanModel):
    expected_result_id: str
    after_operation_id: str
    observable_ref: str
    kind: str
    statement: str
    operator: str | None = None
    expected: Any | None = None
    unit: str | None = None
    path: str | None = None
    name: str | None = None
    column: str | None = None
    metric: str | None = None
    percentile: str | None = None


class ExecutionSource(StrictPlanModel):
    source_kind: Literal["operation", "expected_result", "required_state"]
    source_id: str


class ProcedurePlanRow(StrictPlanModel):
    row_id: str
    source: ExecutionSource
    operation_ref: str
    action: str
    checkpoint: str | None = None
    input_data: str | None = None
    data_bindings: list[BoundData] = Field(default_factory=list)
    assertions: list[BoundAssertion] = Field(default_factory=list)
    procedure_id: str
    procedure_version: int = Field(ge=1)
    procedure_fingerprint: str

    @model_validator(mode="after")
    def validate_procedure_ref(self) -> "ProcedurePlanRow":
        _require_safe_artifact_id(self.procedure_id, "ProcedurePlanRow.procedure_id")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.procedure_fingerprint):
            raise ValueError("ProcedurePlanRow.procedure_fingerprint 无效")
        return self


class ProcedureExecution(StrictPlanModel):
    kind: Literal["procedure_playwright"] = "procedure_playwright"
    capability_profile_ref: str
    capability_site: str
    library_id: str
    library_hash: str
    procedure_refs: list[str] = Field(default_factory=list)
    rows: list[ProcedurePlanRow] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capabilities(self) -> "ProcedureExecution":
        _require_safe_artifact_id(self.capability_site, "ProcedureExecution.capability_site")
        _require_safe_artifact_id(self.library_id, "ProcedureExecution.library_id")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.library_hash):
            raise ValueError("ProcedureExecution.library_hash 无效")
        if len(set(self.procedure_refs)) != len(self.procedure_refs):
            raise ValueError("ProcedureExecution.procedure_refs 不能重复")
        expected_refs = sorted(
            {f"{row.procedure_id}@v{row.procedure_version}" for row in self.rows}
        )
        if self.procedure_refs != expected_refs:
            raise ValueError("ProcedureExecution.procedure_refs 与行级模块引用不一致")
        return self


class HttpRequestPlan(StrictPlanModel):
    request_id: str
    source: ExecutionSource
    operation_ref: str
    action: str
    method: str
    path: str
    data_bindings: list[BoundData] = Field(default_factory=list)
    assertions: list[BoundAssertion] = Field(default_factory=list)


class HttpExecution(StrictPlanModel):
    kind: Literal["http_api"] = "http_api"
    base_url_ref: str
    requests: list[HttpRequestPlan] = Field(min_length=1)


class DatabaseOperationPlan(StrictPlanModel):
    operation_run_id: str
    source: ExecutionSource
    operation_ref: str
    action: str
    operation_kind: Literal["query"] = "query"
    execution_policy: Literal["read_only"] = "read_only"
    sql: str | None = None
    parameters_refs: dict[str, str] = Field(default_factory=dict)
    sql_origin: Literal["catalog", "knowledge_reused", "ai_generated"] = "catalog"
    knowledge_scope_id: str | None = None
    data_bindings: list[BoundData] = Field(default_factory=list)
    assertions: list[BoundAssertion] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sql_mode(self) -> "DatabaseOperationPlan":
        if self.sql_origin == "catalog":
            if self.sql is not None or self.parameters_refs or self.knowledge_scope_id:
                raise ValueError("catalog 数据库操作不能携带 AI SQL 字段")
        else:
            if self.sql is None:
                raise ValueError("AI 数据库操作必须携带 SQL")
            validate_read_only_sql(
                self.sql,
                parameters_refs=self.parameters_refs,
            )
        return self


class DatabaseExecution(StrictPlanModel):
    kind: Literal["database"] = "database"
    connection_profile_ref: str
    operations: list[DatabaseOperationPlan] = Field(min_length=1)


class PortProbePlan(StrictPlanModel):
    """One catalog-bound TCP connection attempt."""

    probe_run_id: str
    source: ExecutionSource
    probe_ref: str
    action: str
    host_ref: str
    port: int = Field(gt=0, le=65_535)
    timeout_seconds: float = Field(gt=0, le=30)
    assertions: list[BoundAssertion] = Field(default_factory=list)

    @field_validator("port", mode="before")
    @classmethod
    def reject_boolean_port(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("PortProbePlan.port 不能是 bool")
        return value

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def validate_timeout(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("PortProbePlan.timeout_seconds 不能是 bool")
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise ValueError("PortProbePlan.timeout_seconds 必须是有限数字")
        return value

    @property
    def operation_ref(self) -> str:
        """Expose a common traceability name without serializing a second ref."""

        return self.probe_ref


class PortExecution(StrictPlanModel):
    kind: Literal["tcp_port"] = "tcp_port"
    probes: list[PortProbePlan] = Field(min_length=1)


class PerformanceThreshold(StrictPlanModel):
    threshold_id: str
    expected_result_id: str
    after_operation_id: str
    observable_ref: str
    metric: str
    operator: str
    value: float
    unit: str | None = None
    percentile: str | None = None


class PerformanceExecution(StrictPlanModel):
    kind: Literal["performance"] = "performance"
    profile_ref: str
    driver_ref: str
    sources: list[ExecutionSource] = Field(min_length=1)
    stages: list[LoadStage] = Field(min_length=1)
    data_bindings: list[BoundData] = Field(default_factory=list)
    thresholds: list[PerformanceThreshold] = Field(default_factory=list)


PlanExecution = Annotated[
    ProcedureExecution
    | HttpExecution
    | DatabaseExecution
    | PortExecution
    | PerformanceExecution,
    Field(discriminator="kind"),
]


class CleanupDataBinding(StrictPlanModel):
    slot: str
    data_id: str
    binding_ref: str
    variable_ref: str


class PlanCleanup(StrictPlanModel):
    cleanup_goal_id: str
    action_ref: str
    handler_kind: str
    policy: str
    target: str | None = None
    always_run: bool
    evidence_required: bool
    data_bindings: list[CleanupDataBinding] = Field(default_factory=list)


class PlanDataGuaranteeResolution(StrictPlanModel):
    resolution_kind: Literal["data_guarantee"] = "data_guarantee"
    required_state_id: str
    text: str
    data_id: str


class PlanSetupStageResolution(StrictPlanModel):
    resolution_kind: Literal["setup_stage"] = "setup_stage"
    required_state_id: str
    text: str
    stage_id: str
    catalog_ref: str


PlanRequiredStateResolution = Annotated[
    PlanDataGuaranteeResolution | PlanSetupStageResolution,
    Field(discriminator="resolution_kind"),
]


class PlanStage(StrictPlanModel):
    stage_id: str
    order: int = Field(ge=1)
    executor_kind: ExecutorKind
    operation_ids: list[str] = Field(default_factory=list)
    expected_result_ids: list[str] = Field(default_factory=list)
    setup_required_state_ids: list[str] = Field(default_factory=list)
    data_ids: list[str] = Field(default_factory=list)
    execution: PlanExecution

    @model_validator(mode="after")
    def validate_stage(self) -> "PlanStage":
        _require_safe_artifact_id(self.stage_id, "PlanStage.stage_id")
        if self.execution.kind != self.executor_kind.value:
            raise ValueError("PlanStage.executor_kind 与 execution.kind 不一致")
        for label, values in (
            ("operation_ids", self.operation_ids),
            ("expected_result_ids", self.expected_result_ids),
            ("setup_required_state_ids", self.setup_required_state_ids),
            ("data_ids", self.data_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"PlanStage.{label} 不能重复")
        return self


class PlanFlow(StrictPlanModel):
    flow_id: str
    name: str
    scenario_id: str
    techniques: list[str] = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    required_state_resolutions: list[PlanRequiredStateResolution] = Field(
        default_factory=list
    )
    stages: list[PlanStage] = Field(min_length=1)
    cleanup: PlanCleanup | None = None

    @model_validator(mode="after")
    def validate_flow(self) -> "PlanFlow":
        _require_safe_artifact_id(self.flow_id, "PlanFlow.flow_id")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("PlanFlow.requirement_ids 不能重复")
        stage_ids = [item.stage_id for item in self.stages]
        if len(set(stage_ids)) != len(stage_ids):
            raise ValueError("PlanFlow.stage_id 不能重复")
        if [item.order for item in self.stages] != list(range(1, len(self.stages) + 1)):
            raise ValueError("PlanFlow.stages.order 必须从 1 连续递增")
        return self


class TestPlanDraft(StrictPlanModel):
    schema_version: Literal["test-plan.v4"] = "test-plan.v4"
    plan_id: str
    version: int = Field(ge=1)
    status: PlanStatus = PlanStatus.DRAFT
    design_id: str
    design_version: int
    design_content_hash: str
    design_input_content_hash: str
    catalog_id: str
    catalog_content_hash: str
    target_system_id: str
    target_environment: str
    flows: list[PlanFlow] = Field(default_factory=list)
    open_questions: list[PlanOpenQuestion] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> "TestPlanDraft":
        _require_safe_artifact_id(self.plan_id, "TestPlanDraft.plan_id")
        _require_safe_artifact_id(self.design_id, "TestPlanDraft.design_id")
        flow_ids = [item.flow_id for item in self.flows]
        if len(set(flow_ids)) != len(flow_ids):
            raise ValueError("TestPlanDraft.flow_id 不能重复")
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"status", "blocked_reasons"})
        # Omit neutral database defaults so canonical hashes remain stable
        # across serialize/parse cycles for existing approved v4 plans.
        for flow in payload.get("flows", []):
            for stage in flow.get("stages", []):
                execution = stage.get("execution", {})
                if execution.get("kind") == "database":
                    for operation in execution.get("operations", []):
                        if operation.get("sql") is None:
                            operation.pop("sql", None)
                        if operation.get("parameters_refs") == {}:
                            operation.pop("parameters_refs", None)
                        if operation.get("sql_origin") == "catalog":
                            operation.pop("sql_origin", None)
                        if operation.get("knowledge_scope_id") is None:
                            operation.pop("knowledge_scope_id", None)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PlanValidationFinding(StrictPlanModel):
    rule_id: str
    message: str
    field_path: str
    blocking: bool = True


class PlanValidationReport(StrictPlanModel):
    plan_id: str
    plan_version: int
    plan_content_hash: str
    passed: bool
    findings: list[PlanValidationFinding] = Field(default_factory=list)
    validation_content_hash: str

    @model_validator(mode="after")
    def validate_report_hash(self) -> "PlanValidationReport":
        expected_passed = not any(item.blocking for item in self.findings)
        if self.passed != expected_passed:
            raise ValueError("passed 必须与 findings 的 blocking 状态一致")
        if self.validation_content_hash != compute_plan_validation_content_hash(self):
            raise ValueError("validation_content_hash 与实际计划校验报告不一致")
        return self


class PlanReview(StrictPlanModel):
    review_id: str = Field(min_length=1, max_length=512)
    plan_id: str
    plan_version: int
    decision: PlanReviewDecision
    comments: str = Field(min_length=1, max_length=4096)
    reviewed_at: str = Field(min_length=1, max_length=128)
    plan_content_hash: str
    validation_content_hash: str
    artifact_set_hash: str
    review_content_hash: str

    @model_validator(mode="after")
    def validate_review_hash(self) -> "PlanReview":
        if len(self.comments.encode("utf-8")) > 4096:
            raise ValueError("计划审核意见不能超过 4096 字节")
        if contains_secret_literal(self.comments):
            raise ValueError("计划审核意见不能包含凭据实际值")
        if self.review_content_hash != compute_plan_review_content_hash(self):
            raise ValueError("review_content_hash 与实际计划审核记录不一致")
        return self


class ExecutorArtifactRef(StrictPlanModel):
    kind: str
    path_ref: str
    sha256: str


class ExecutorArtifactBundle(StrictPlanModel):
    schema_version: Literal["executor-artifact-bundle.v4"] = (
        "executor-artifact-bundle.v4"
    )
    artifact_id: str
    artifact_schema_version: str
    executor_kind: ExecutorKind
    flow_id: str
    stage_id: str
    design_id: str
    design_version: int
    design_content_hash: str
    design_input_content_hash: str
    plan_id: str
    plan_version: int
    plan_content_hash: str
    catalog_id: str
    catalog_content_hash: str
    manifest_path_ref: str
    artifact_refs: list[ExecutorArtifactRef] = Field(min_length=1)

    @model_validator(mode="after")
    def require_manifest_ref(self) -> "ExecutorArtifactBundle":
        if not any(item.path_ref == self.manifest_path_ref for item in self.artifact_refs):
            raise ValueError("artifact_refs 必须包含 manifest_path_ref")
        return self


def compute_artifact_set_hash(artifacts: list[ExecutorArtifactBundle]) -> str:
    payload = [
        item.model_dump(mode="json")
        for item in sorted(
            artifacts,
            key=lambda item: (
                item.flow_id,
                item.stage_id,
                item.executor_kind.value,
                item.artifact_id,
            ),
        )
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovedTestPlanBundle(StrictPlanModel):
    schema_version: Literal["approved-test-plan-bundle.v4"] = (
        "approved-test-plan-bundle.v4"
    )
    plan: TestPlanDraft
    validation: PlanValidationReport
    review: PlanReview
    catalog_snapshot: PlanningCatalogSnapshot
    compiled_artifacts: list[ExecutorArtifactBundle] = Field(min_length=1)

    @model_validator(mode="after")
    def enforce_handoff_gate(self) -> "ApprovedTestPlanBundle":
        if self.plan.status != PlanStatus.APPROVED:
            raise ValueError("只有 approved TestPlan 能交接")
        if self.plan.open_questions or self.plan.blocked_reasons:
            raise ValueError("approved TestPlan 不能保留 open_questions 或 blocked_reasons")
        actual_hash = self.plan.content_hash()
        if (
            not self.validation.passed
            or any(item.blocking for item in self.validation.findings)
            or self.validation.plan_id != self.plan.plan_id
            or self.validation.plan_version != self.plan.version
            or self.validation.plan_content_hash != actual_hash
        ):
            raise ValueError("计划校验没有绑定当前计划内容")
        validation_hash = compute_plan_validation_content_hash(self.validation)
        if (
            self.review.decision != PlanReviewDecision.APPROVED
            or self.review.plan_id != self.plan.plan_id
            or self.review.plan_version != self.plan.version
            or self.review.plan_content_hash != actual_hash
            or self.review.validation_content_hash != validation_hash
        ):
            raise ValueError("计划审核没有绑定当前计划及校验报告")
        if self.review.artifact_set_hash != compute_artifact_set_hash(
            self.compiled_artifacts
        ):
            raise ValueError("计划审核没有绑定当前编译产物")
        if (
            self.catalog_snapshot.catalog_id != self.plan.catalog_id
            or self.catalog_snapshot.content_hash != self.plan.catalog_content_hash
        ):
            raise ValueError("计划与 catalog snapshot 不一致")
        expected = {
            (stage.executor_kind, flow.flow_id, stage.stage_id)
            for flow in self.plan.flows
            for stage in flow.stages
        }
        actual = {
            (item.executor_kind, item.flow_id, item.stage_id)
            for item in self.compiled_artifacts
        }
        if expected != actual or len(actual) != len(self.compiled_artifacts):
            raise ValueError("编译产物必须与计划 stage 一一对应")
        for artifact in self.compiled_artifacts:
            if (
                artifact.design_id != self.plan.design_id
                or artifact.design_version != self.plan.design_version
                or artifact.design_content_hash != self.plan.design_content_hash
                or artifact.design_input_content_hash
                != self.plan.design_input_content_hash
                or artifact.plan_id != self.plan.plan_id
                or artifact.plan_version != self.plan.version
                or artifact.plan_content_hash != actual_hash
                or artifact.catalog_id != self.plan.catalog_id
                or artifact.catalog_content_hash != self.plan.catalog_content_hash
            ):
                raise ValueError("编译产物身份与计划不一致")
        if self.review.review_content_hash != compute_plan_review_content_hash(
            self.review
        ):
            raise ValueError("计划审核记录 hash 不一致")
        return self


def _canonical_plan_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_plan_validation_content_hash(
    report: PlanValidationReport | dict[str, Any],
) -> str:
    """绑定 passed 状态和全部阻塞、非阻塞计划校验提示。"""

    if isinstance(report, PlanValidationReport):
        payload = report.model_dump(mode="json", exclude={"validation_content_hash"})
    else:
        payload = dict(report)
        payload.pop("validation_content_hash", None)
    return _canonical_plan_hash(payload)


def compute_plan_review_content_hash(
    review: PlanReview | dict[str, Any],
) -> str:
    """绑定审核身份、决定、意见、时间及计划/校验/产物指纹。"""

    if isinstance(review, PlanReview):
        payload = review.model_dump(mode="json", exclude={"review_content_hash"})
    else:
        payload = dict(review)
        payload.pop("review_content_hash", None)
    return _canonical_plan_hash(payload)


def design_hash(bundle: ApprovedTestDesignBundle) -> str:
    if bundle.design.status != DesignStatus.APPROVED:
        raise ValueError("第二层只接受 approved TestDesign bundle")
    return bundle.review.design_content_hash


def reject_secret_values(value: Any) -> None:
    if isinstance(value, str) and contains_secret_literal(value):
        raise ValueError("计划或产物不能包含秘密值")
    if isinstance(value, dict):
        for key, item in value.items():
            reject_secret_values(key)
            reject_secret_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secret_values(item)


__all__ = [
    "ApprovedTestPlanBundle",
    "BoundAssertion",
    "BoundData",
    "ProcedureExecution",
    "ProcedurePlanRow",
    "CleanupDataBinding",
    "CleanupDataBindingSelection",
    "CleanupSelection",
    "DataBindingSelection",
    "DataGuaranteeResolutionCandidate",
    "DatabaseQueryDraftCandidate",
    "DatabaseExecution",
    "DatabaseOperationPlan",
    "ExecutionSource",
    "ExecutorArtifactBundle",
    "ExecutorArtifactRef",
    "ExecutorKind",
    "ExpectedResultSelection",
    "HttpExecution",
    "HttpRequestPlan",
    "ModelMessage",
    "OperationSelection",
    "PerformanceExecution",
    "PerformanceThreshold",
    "PlanExecution",
    "PlanCandidate",
    "PlanCleanup",
    "PlanDataGuaranteeResolution",
    "PlanFlow",
    "PlanFlowCandidate",
    "PlanOpenQuestion",
    "PlanRequiredStateResolution",
    "PlanReview",
    "PlanReviewDecision",
    "PlanSetupStageResolution",
    "PlanStage",
    "PlanStageCandidate",
    "PlanStatus",
    "PlanValidationFinding",
    "PlanValidationReport",
    "PortExecution",
    "PortProbePlan",
    "SetupStageResolutionCandidate",
    "TestPlanDraft",
    "compute_artifact_set_hash",
    "compute_plan_review_content_hash",
    "compute_plan_validation_content_hash",
    "design_hash",
    "format_procedure_input_data",
]
