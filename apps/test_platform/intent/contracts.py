"""第一层 TestDesign v4 公共契约。

第一层直接接收原始需求文本和前端选择。它不解析标题、列表、表格或段落，
也不拆分或改写原文；审核通过的产物只保留原文快照、内容哈希和
场景到需求的粗粒度追溯。
"""

from __future__ import annotations

from enum import Enum
import hashlib
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_SAFE_DESIGN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,191}$")


class StrictModel(BaseModel):
    """所有公开契约拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid")


class DesignStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class TestTechnique(str, Enum):
    """Legacy convenience values; active request contracts accept free text."""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    BOUNDARY = "boundary"
    STATE_TRANSITION = "state_transition"
    RECOVERY = "recovery"
    PERMISSION = "permission"
    IDEMPOTENCY = "idempotency"
    RANDOM = "random"


class TestChannel(str, Enum):
    UI = "ui"
    API = "api"
    DATABASE = "database"
    PERFORMANCE = "performance"
    PORT = "port"


class StateImpactKind(str, Enum):
    READ_ONLY = "read_only"
    CREATES_DATA = "creates_data"
    CHANGES_STATE = "changes_state"
    UNKNOWN = "unknown"


class ModelMessage(StrictModel):
    role: Literal["system", "user"]
    content: str


class RequirementInput(StrictModel):
    """调用方提交的原始需求；content 会原样进入模型，不做结构解析。"""

    requirement_id: Optional[str] = None
    content: str = Field(min_length=1, max_length=262_144)

    @model_validator(mode="after")
    def validate_requirement(self) -> "RequirementInput":
        if self.requirement_id is not None:
            value = self.requirement_id.strip()
            if not _SAFE_ID_PATTERN.fullmatch(value):
                raise ValueError(
                    "requirement_id 只能使用 1-128 位 ASCII 字母、数字、点、下划线、@ 或连字符"
                )
            self.requirement_id = value
        if not self.content.strip():
            raise ValueError("需求原文不能为空")
        if len(self.content.encode("utf-8")) > 256 * 1024:
            raise ValueError("单份需求原文不能超过 256 KiB")
        return self


class TargetSelection(StrictModel):
    """由前端或调用方明确提交；模型没有这个输出字段。"""

    system_id: str = Field(default="unknown", min_length=1, max_length=128)
    environment: str = Field(default="unknown", min_length=1, max_length=128)


class DesignSelections(StrictModel):
    """用户可选的测试方式、允许/必需渠道和可选知识范围。

    ``allowed_channels`` 是模型可以建议的渠道上限，不表示每个渠道都适合当前需求。
    只有调用方明确放入 ``required_channels`` 的渠道才要求至少出现一次。该字段默认
    为空，以兼容现有请求，也避免模型为了形式覆盖而编造无关 UI、数据库或端口测试。
    """

    techniques: list[str] = Field(default_factory=list)
    techniques_by_channel: dict[TestChannel, list[str]] = Field(default_factory=dict)
    allowed_channels: list[TestChannel] = Field(min_length=1)
    required_channels: list[TestChannel] = Field(default_factory=list)
    knowledge_scope_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_unique_values(self) -> "DesignSelections":
        self.techniques[:] = [value.strip() for value in self.techniques]
        if any(not value or len(value) > 128 for value in self.techniques):
            raise ValueError("techniques 不能包含空值且单项不能超过 128 字符")
        if len(set(self.techniques)) != len(self.techniques):
            raise ValueError("techniques 不能重复")
        for channel, values in self.techniques_by_channel.items():
            values[:] = [value.strip() for value in values]
            if channel not in self.allowed_channels:
                raise ValueError(
                    f"techniques_by_channel 包含未选择渠道: {channel.value}"
                )
            if not values:
                raise ValueError(
                    f"techniques_by_channel.{channel.value} 至少包含一种覆盖方式"
                )
            if any(len(value) > 128 for value in values):
                raise ValueError(
                    f"techniques_by_channel.{channel.value} 单项不能超过 128 字符"
                )
            if len(set(values)) != len(values):
                raise ValueError(
                    f"techniques_by_channel.{channel.value} 不能重复"
                )
            unknown = set(values) - set(self.techniques)
            if unknown:
                raise ValueError(
                    f"techniques_by_channel.{channel.value} 包含未登记覆盖方式: "
                    f"{sorted(unknown)}"
                )
        if self.techniques_by_channel:
            missing_channels = set(self.allowed_channels) - set(
                self.techniques_by_channel
            )
            if missing_channels:
                raise ValueError(
                    "techniques_by_channel 缺少已选择渠道: "
                    f"{sorted(item.value for item in missing_channels)}"
                )
            assigned = {
                item
                for values in self.techniques_by_channel.values()
                for item in values
            }
            if assigned != set(self.techniques):
                raise ValueError("techniques 必须等于各渠道覆盖方式的并集")
        if len(set(self.allowed_channels)) != len(self.allowed_channels):
            raise ValueError("allowed_channels 不能重复")
        if len(set(self.required_channels)) != len(self.required_channels):
            raise ValueError("required_channels 不能重复")
        unknown_required = set(self.required_channels) - set(self.allowed_channels)
        if unknown_required:
            raise ValueError(
                "required_channels 必须是 allowed_channels 的子集: "
                f"{sorted(item.value for item in unknown_required)}"
            )
        normalized_scopes = [value.strip() for value in self.knowledge_scope_ids]
        if any(not value for value in normalized_scopes):
            raise ValueError("knowledge_scope_ids 不能包含空值")
        if any(len(value) > 128 for value in normalized_scopes):
            raise ValueError("knowledge_scope_ids 单项不能超过 128 字符")
        if len(set(normalized_scopes)) != len(normalized_scopes):
            raise ValueError("knowledge_scope_ids 不能重复")
        self.knowledge_scope_ids[:] = normalized_scopes
        return self


class TestDesignRequest(StrictModel):
    schema_version: Literal["test-design-request.v4"] = "test-design-request.v4"
    request_id: Optional[str] = None
    requirements: list[RequirementInput] = Field(min_length=1, max_length=20)
    target: TargetSelection
    selections: DesignSelections

    @model_validator(mode="after")
    def validate_request(self) -> "TestDesignRequest":
        if self.request_id is not None:
            value = self.request_id.strip()
            if not _SAFE_ID_PATTERN.fullmatch(value):
                raise ValueError(
                    "request_id 只能使用 1-128 位 ASCII 字母、数字、点、下划线、@ 或连字符"
                )
            self.request_id = value
        explicit_ids = [
            item.requirement_id
            for item in self.requirements
            if item.requirement_id is not None
        ]
        if len(set(explicit_ids)) != len(explicit_ids):
            raise ValueError("requirements.requirement_id 不能重复")
        return self


class ApprovedKnowledge(StrictModel):
    """平台已经审核并锁定内容哈希的可选知识。"""

    scope_id: str = Field(min_length=1, max_length=128)
    knowledge_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    approval_id: str = Field(min_length=1, max_length=128)
    approved_at: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=262_144)
    content_hash: str

    @model_validator(mode="after")
    def validate_approved_knowledge(self) -> "ApprovedKnowledge":
        if not all(
            value.strip()
            for value in (
                self.scope_id,
                self.knowledge_id,
                self.approval_id,
                self.approved_at,
                self.content,
            )
        ):
            raise ValueError("approved knowledge 缺少内容或审批身份")
        if not _SAFE_ID_PATTERN.fullmatch(self.knowledge_id):
            raise ValueError("knowledge_id 不是安全标识")
        actual_hash = _text_hash(self.content)
        if self.content_hash != actual_hash:
            raise ValueError("approved knowledge content_hash 与内容不一致")
        if contains_secret_literal(self.content):
            raise ValueError("approved knowledge 不能包含凭据实际值")
        return self


class RequirementSnapshot(StrictModel):
    """审核时锁定的原始需求；它是快照，不是解析结果。"""

    requirement_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=262_144)
    content_hash: str

    @model_validator(mode="after")
    def validate_snapshot(self) -> "RequirementSnapshot":
        if not _SAFE_ID_PATTERN.fullmatch(self.requirement_id):
            raise ValueError("requirement snapshot ID 不是安全标识")
        if not self.content.strip():
            raise ValueError("requirement snapshot 内容不能为空")
        if self.content_hash != _text_hash(self.content):
            raise ValueError("requirement snapshot content_hash 与原文不一致")
        return self


class TestDesignInputSnapshot(StrictModel):
    """模型实际看到的完整输入，用于绑定校验和人工审核。"""

    request_id: str
    requirements: list[RequirementSnapshot] = Field(min_length=1, max_length=20)
    target: TargetSelection
    selections: DesignSelections
    approved_knowledge: list[ApprovedKnowledge] = Field(default_factory=list, max_length=20)
    review_feedback: Optional[str] = None
    content_hash: str

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        requirements: list[RequirementSnapshot],
        target: TargetSelection,
        selections: DesignSelections,
        approved_knowledge: list[ApprovedKnowledge],
        review_feedback: str | None,
    ) -> "TestDesignInputSnapshot":
        payload = {
            "request_id": request_id,
            "requirements": [item.model_dump(mode="json") for item in requirements],
            "target": target.model_dump(mode="json"),
            "selections": selections.model_dump(mode="json"),
            "approved_knowledge": [
                item.model_dump(mode="json") for item in approved_knowledge
            ],
            "review_feedback": review_feedback,
        }
        return cls(**payload, content_hash=_canonical_hash(payload))

    @model_validator(mode="after")
    def validate_input_snapshot(self) -> "TestDesignInputSnapshot":
        if not _SAFE_ID_PATTERN.fullmatch(self.request_id):
            raise ValueError("input snapshot request_id 不是安全标识")
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("input snapshot requirement_id 不能重复")
        scopes = [item.scope_id for item in self.approved_knowledge]
        if len(set(scopes)) != len(scopes):
            raise ValueError("input snapshot knowledge scope 不能重复")
        if scopes != self.selections.knowledge_scope_ids:
            raise ValueError("approved knowledge 必须按选择顺序精确覆盖 knowledge scopes")
        if self.review_feedback is not None:
            if not self.review_feedback.strip() or len(
                self.review_feedback.encode("utf-8")
            ) > 4096:
                raise ValueError("review_feedback 必须为 1-4096 字节")
            if contains_secret_literal(self.review_feedback):
                raise ValueError("review_feedback 不能包含凭据实际值")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != _canonical_hash(payload):
            raise ValueError("input snapshot content_hash 与实际输入不一致")
        return self


class DesignText(StrictModel):
    """没有系统 ID 的业务文本；语义是否成立由审核人确认。"""

    text: str = Field(min_length=1, max_length=8192)
    derivation_note: Optional[str] = Field(default=None, max_length=4096)


class RequiredStateCandidate(DesignText):
    """模型提出的执行前必要状态。"""


class OperationCandidate(DesignText):
    """模型提出的逻辑操作；channel_hint 不是执行器配置。"""

    channel_hint: TestChannel


class ExpectedResultCandidate(DesignText):
    """模型提出的独立业务判定，并以 1-based 索引关联操作。"""

    after_operation_index: int = Field(ge=1)
    channel_hint: TestChannel
    operator: Optional[str] = None
    expected: Optional[Any] = None
    unit: Optional[str] = None

    @model_validator(mode="after")
    def require_safe_json_expected(self) -> "ExpectedResultCandidate":
        _validate_expected_value(self.expected)
        return self


class DataRequirementCandidate(DesignText):
    """模型提出的数据种类和约束，不包含实际凭据。"""

    constraints: list[DesignText] = Field(default_factory=list, max_length=50)


class CleanupGoalCandidate(DesignText):
    """逻辑恢复目标，以 1-based 索引引用本场景的数据要求。"""

    subject_data_indexes: list[PositiveInt] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def require_unique_subjects(self) -> "CleanupGoalCandidate":
        if len(set(self.subject_data_indexes)) != len(self.subject_data_indexes):
            raise ValueError("subject_data_indexes 不能重复")
        return self


class StateImpactCandidate(StrictModel):
    impact: StateImpactKind
    rationale: DesignText
    cleanup_goal: Optional[CleanupGoalCandidate] = None


class LogicalScenarioCandidate(StrictModel):
    """逻辑测试场景，只追溯到原始需求，不包含执行器动作。"""

    title: str = Field(min_length=1, max_length=512)
    techniques: list[str] = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    required_states: list[RequiredStateCandidate] = Field(default_factory=list)
    operations: list[OperationCandidate] = Field(min_length=1)
    expected_results: list[ExpectedResultCandidate] = Field(min_length=1)
    data_requirements: list[DataRequirementCandidate] = Field(default_factory=list)
    state_impact: StateImpactCandidate

    @model_validator(mode="after")
    def require_unique_references(self) -> "LogicalScenarioCandidate":
        if len(set(self.techniques)) != len(self.techniques):
            raise ValueError("scenario techniques 不能重复")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("scenario requirement_ids 不能重复")
        invalid_operation_indexes = [
            item.after_operation_index
            for item in self.expected_results
            if item.after_operation_index > len(self.operations)
        ]
        if invalid_operation_indexes:
            raise ValueError(
                "expected_results.after_operation_index 超出 operations 范围: "
                f"{invalid_operation_indexes}"
            )
        cleanup = self.state_impact.cleanup_goal
        if cleanup is not None:
            invalid_data_indexes = [
                index
                for index in cleanup.subject_data_indexes
                if index > len(self.data_requirements)
            ]
            if invalid_data_indexes:
                raise ValueError(
                    "cleanup_goal.subject_data_indexes 超出 data_requirements 范围: "
                    f"{invalid_data_indexes}"
                )
        return self


class OpenQuestionCandidate(StrictModel):
    question: str = Field(min_length=1, max_length=4096)
    field_path: Optional[str] = Field(default=None, max_length=512)


class TestDesignCandidate(StrictModel):
    """LLM 唯一允许输出的 schema。"""

    title: str = Field(min_length=1, max_length=512)
    objective: DesignText
    in_scope: list[DesignText] = Field(min_length=1)
    out_of_scope: list[DesignText] = Field(default_factory=list)
    scenarios: list[LogicalScenarioCandidate] = Field(min_length=1)
    open_questions: list[OpenQuestionCandidate] = Field(default_factory=list)


class RequiredState(DesignText):
    required_state_id: str


class Operation(DesignText):
    operation_id: str
    channel_hint: TestChannel


class ExpectedResult(DesignText):
    expected_result_id: str
    after_operation_id: str
    channel_hint: TestChannel
    operator: Optional[str] = None
    expected: Optional[Any] = None
    unit: Optional[str] = None

    @model_validator(mode="after")
    def require_safe_json_expected(self) -> "ExpectedResult":
        _validate_expected_value(self.expected)
        return self


class DataRequirement(DesignText):
    data_id: str
    constraints: list[DesignText] = Field(default_factory=list, max_length=50)


class CleanupGoal(DesignText):
    cleanup_goal_id: str
    subject_data_ids: list[str] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def require_unique_subjects(self) -> "CleanupGoal":
        if len(set(self.subject_data_ids)) != len(self.subject_data_ids):
            raise ValueError("cleanup goal subject_data_ids 不能重复")
        return self


class StateImpact(StrictModel):
    impact: StateImpactKind
    rationale: DesignText
    cleanup_goal: Optional[CleanupGoal] = None


class LogicalScenario(StrictModel):
    scenario_id: str
    title: str
    techniques: list[str] = Field(min_length=1)
    requirement_ids: list[str]
    required_states: list[RequiredState]
    operations: list[Operation]
    expected_results: list[ExpectedResult]
    data_requirements: list[DataRequirement]
    state_impact: StateImpact

    @model_validator(mode="after")
    def validate_references(self) -> "LogicalScenario":
        if not self.requirement_ids:
            raise ValueError("scenario 至少要关联一份原始需求")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("scenario requirement_ids 不能重复")
        if len(set(self.techniques)) != len(self.techniques):
            raise ValueError("scenario techniques 不能重复")
        operation_ids = [item.operation_id for item in self.operations]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("scenario operation_id 不能重复")
        unknown_operations = {
            item.after_operation_id for item in self.expected_results
        } - set(operation_ids)
        if unknown_operations:
            raise ValueError(
                f"expected result 引用了不存在的 operation_id: {sorted(unknown_operations)}"
            )
        data_ids = [item.data_id for item in self.data_requirements]
        if len(set(data_ids)) != len(data_ids):
            raise ValueError("scenario data_id 不能重复")
        cleanup = self.state_impact.cleanup_goal
        if cleanup is not None:
            unknown_data = set(cleanup.subject_data_ids) - set(data_ids)
            if unknown_data:
                raise ValueError(
                    f"cleanup goal 引用了不存在的 data_id: {sorted(unknown_data)}"
                )
        return self


class OpenQuestion(StrictModel):
    question_id: str
    question: str
    field_path: Optional[str] = None
    blocking: Literal[True] = True


class TestDesign(StrictModel):
    schema_version: Literal["test-design.v4"] = "test-design.v4"
    design_id: str
    version: int = Field(ge=1)
    status: DesignStatus = DesignStatus.DRAFT
    title: str
    objective: DesignText
    target: TargetSelection
    selections: DesignSelections
    in_scope: list[DesignText]
    out_of_scope: list[DesignText]
    scenarios: list[LogicalScenario]
    open_questions: list[OpenQuestion]

    @model_validator(mode="after")
    def validate_design_identity(self) -> "TestDesign":
        if not _SAFE_DESIGN_ID_PATTERN.fullmatch(self.design_id):
            raise ValueError("design_id 必须是安全的 1-192 位 ASCII 标识")
        return self


class ValidationFinding(StrictModel):
    rule_id: str
    message: str
    field_path: str
    blocking: bool = True


class TestDesignValidationReport(StrictModel):
    design_id: str
    design_version: int
    design_content_hash: str
    input_content_hash: str
    passed: bool
    findings: list[ValidationFinding] = Field(default_factory=list)
    validation_content_hash: str

    @model_validator(mode="after")
    def validate_report_hash(self) -> "TestDesignValidationReport":
        expected_passed = not any(item.blocking for item in self.findings)
        if self.passed != expected_passed:
            raise ValueError("passed 必须与 findings 的 blocking 状态一致")
        if self.validation_content_hash != compute_validation_content_hash(self):
            raise ValueError("validation_content_hash 与实际校验报告不一致")
        return self


class TestDesignReview(StrictModel):
    review_id: str = Field(min_length=1, max_length=192)
    design_id: str
    design_version: int
    decision: ReviewDecision
    comments: str = Field(min_length=1, max_length=4096)
    reviewed_at: str = Field(min_length=1, max_length=64)
    design_content_hash: str
    input_content_hash: str
    validation_content_hash: str
    review_content_hash: str

    @model_validator(mode="after")
    def validate_review_hash(self) -> "TestDesignReview":
        if not all(
            value.strip()
            for value in (
                self.review_id,
                self.comments,
                self.reviewed_at,
            )
        ):
            raise ValueError("review_id、comments 和 reviewed_at 不能为空")
        if len(self.comments.encode("utf-8")) > 4096:
            raise ValueError("审核意见不能超过 4096 字节")
        if contains_secret_literal(self.comments):
            raise ValueError("审核意见不能包含凭据实际值")
        if self.review_content_hash != compute_review_content_hash(self):
            raise ValueError("review_content_hash 与实际审核记录不一致")
        return self


class ApprovedTestDesignBundle(StrictModel):
    """第一层到第二层的唯一正式产物。"""

    schema_version: Literal["approved-test-design-bundle.v4"] = (
        "approved-test-design-bundle.v4"
    )
    design: TestDesign
    input_snapshot: TestDesignInputSnapshot
    validation: TestDesignValidationReport
    review: TestDesignReview
    trace_id: str

    @model_validator(mode="after")
    def enforce_handoff_gate(self) -> "ApprovedTestDesignBundle":
        if self.design.status != DesignStatus.APPROVED:
            raise ValueError("只有 approved TestDesign 能交接")
        if not self.validation.passed or any(
            item.blocking for item in self.validation.findings
        ):
            raise ValueError("校验未通过的 TestDesign 不能交接")
        if self.review.decision != ReviewDecision.APPROVED:
            raise ValueError("审核未批准")
        if self.validation.design_id != self.design.design_id or (
            self.validation.design_version != self.design.version
        ):
            raise ValueError("校验报告与 TestDesign 版本不匹配")
        if self.review.design_id != self.design.design_id or (
            self.review.design_version != self.design.version
        ):
            raise ValueError("审核记录与 TestDesign 版本不匹配")

        design_hash = compute_design_content_hash(self.design)
        if self.validation.design_content_hash != design_hash or (
            self.review.design_content_hash != design_hash
        ):
            raise ValueError("校验/审核 hash 与实际 TestDesign 内容不一致")
        input_hash = compute_input_content_hash(self.input_snapshot)
        if self.validation.input_content_hash != input_hash or (
            self.review.input_content_hash != input_hash
        ):
            raise ValueError("校验/审核 hash 与实际模型输入不一致")
        validation_hash = compute_validation_content_hash(self.validation)
        if self.review.validation_content_hash != validation_hash:
            raise ValueError("人工审核没有绑定当前校验报告")
        if self.input_snapshot.target.model_dump(mode="json") != self.design.target.model_dump(
            mode="json"
        ) or self.input_snapshot.selections.model_dump(
            mode="json"
        ) != self.design.selections.model_dump(mode="json"):
            raise ValueError("input snapshot 与 TestDesign 的前端选择不一致")

        known_requirements = {
            item.requirement_id for item in self.input_snapshot.requirements
        }
        referenced_requirements = collect_requirement_ids(self.design)
        if not referenced_requirements <= known_requirements:
            raise ValueError("测试场景引用了 input snapshot 中不存在的需求")
        if self.review.review_content_hash != compute_review_content_hash(self.review):
            raise ValueError("审核记录 hash 不一致")
        expected_trace_id = f"trace-{self.design.design_id}-v{self.design.version}"
        if self.trace_id != expected_trace_id:
            raise ValueError("trace_id 与 design/version 不一致")
        if not all(
            value.strip()
            for value in (
                self.review.review_id,
                self.review.comments,
                self.review.reviewed_at,
                self.trace_id,
            )
        ):
            raise ValueError("审核时间、意见和 trace_id 不能为空")
        return self


_HIGH_CONFIDENCE_SECRET_PATTERN = re.compile(
    r"(?:\b(?:bearer)\s+[A-Za-z0-9._~+/=-]+"
    r"|\b(?:jdbc|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://"
    r"|\bsk-[A-Za-z0-9]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    r"|\bhttps?://[^\s/:@]+:[^\s/@]+@"
    r"|-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----)",
    re.IGNORECASE,
)

_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>\b(?:[A-Za-z][A-Za-z0-9]*[_-])*"
    r"(?:password|passwd|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?token|api[_-]?key|client[_-]?secret|private[_-]?key|"
    r"aws[_-]?secret[_-]?access[_-]?key|secret|token|authorization)"
    r"|密码|口令|密钥|令牌)\s*[\"']?\s*[:：=]\s*[\"']?(?P<value>[^\r\n,;\"']+)",
    re.IGNORECASE,
)

_CREDENTIAL_POLICY_VALUE_PATTERN = re.compile(
    r"(?:^\s*(?:\{[^}]+\}|[\[{]|<[^>]+>)\s*$"
    r"|^\s*(?:string|integer|number|boolean|object|array|null)\s*$"
    r"|\b(?:policy|field|name|placeholder|format|at\s+least|at\s+most|between|"
    r"minimum|maximum|min|max|length|rule|required|must|characters?|chars?|"
    r"contain|uppercase|lowercase|digits?)\b"
    r"|(?:占位|字段|格式|策略|至少|最多|长度|规则|必须|不能|应当|错误|正确|"
    r"为空|连续|重置|不得|不少于|不超过|需要|需|包含|字母|数字|字符|位|组成|"
    r"由\s*\d+\s*(?:至|到|-|~)\s*\d+\s*位))",
    re.IGNORECASE,
)

_SCHEMA_KEYS = {
    "$ref",
    "allof",
    "anyof",
    "deprecated",
    "description",
    "discriminator",
    "enum",
    "format",
    "items",
    "maxlength",
    "maxitems",
    "maximum",
    "minlength",
    "minitems",
    "minimum",
    "nullable",
    "oneof",
    "pattern",
    "properties",
    "readonly",
    "required",
    "title",
    "type",
    "writeonly",
}

_SCHEMA_LITERAL_KEYS = {"const", "default", "example", "examples"}


def _normalized_mapping_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff$]", "", str(value).lower())


def _is_schema_mapping(value: Any) -> bool:
    return isinstance(value, dict) and any(
        _normalized_mapping_key(key) in _SCHEMA_KEYS for key in value
    )


def _schema_contains_credential_example(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalized_mapping_key(key)
            if normalized_key in _SCHEMA_LITERAL_KEYS:
                values = item if isinstance(item, (list, tuple)) else [item]
                if any(
                    candidate not in (None, "", "<redacted>", "<placeholder>")
                    for candidate in values
                ):
                    return True
            if normalized_key == "properties" and isinstance(item, dict):
                if contains_secret_value(item):
                    return True
            elif isinstance(item, (dict, list, tuple)) and _schema_contains_credential_example(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_schema_contains_credential_example(item) for item in value)
    return False


def contains_secret_literal(value: str) -> bool:
    if _HIGH_CONFIDENCE_SECRET_PATTERN.search(value):
        return True
    if _contains_structured_secret(value):
        return True
    return any(
        re.fullmatch(
            r"(?:true|false|null)\s*[}\],;]*",
            match.group("value").strip(),
            flags=re.IGNORECASE,
        )
        is None
        and not _CREDENTIAL_POLICY_VALUE_PATTERN.search(match.group("value"))
        for match in _CREDENTIAL_ASSIGNMENT_PATTERN.finditer(value)
    )


def _contains_structured_secret(value: str) -> bool:
    """Inspect complete JSON snippets embedded in prose without rewriting prose."""

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", value):
        try:
            parsed, _ = decoder.raw_decode(value[match.start() :])
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, (dict, list, tuple)) and contains_secret_value(parsed):
            return True
    return False


def contains_secret_value(value: Any) -> bool:
    if isinstance(value, str):
        return contains_secret_literal(value)
    if isinstance(value, dict):
        sensitive_keys = {
            "password",
            "passwd",
            "secret",
            "clientsecret",
            "token",
            "accesstoken",
            "refreshtoken",
            "sessiontoken",
            "privatekey",
            "awssecretaccesskey",
            "authorization",
            "apikey",
            "密码",
            "口令",
            "密钥",
            "令牌",
        }
        for key, item in value.items():
            normalized_key = _normalized_mapping_key(key)
            if normalized_key == "secret" and isinstance(item, bool):
                # A schema's boolean secret flag is not a credential value.
                # String/non-boolean values still pass
                # through the normal secret checks below.
                continue
            is_sensitive_key = any(
                normalized_key.endswith(suffix) for suffix in sensitive_keys
            )
            if is_sensitive_key and item not in (None, "", "<redacted>"):
                if _is_schema_mapping(item):
                    if _schema_contains_credential_example(item):
                        return True
                    continue
                if not isinstance(item, str) or contains_secret_literal(f"{key}={item}"):
                    return True
            if contains_secret_value(key) or contains_secret_value(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_secret_value(item) for item in value)
    return False


def _validate_expected_value(value: Any) -> None:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("expected 必须是有限的 JSON 值") from exc
    if len(serialized.encode("utf-8")) > 16_384:
        raise ValueError("expected JSON 不能超过 16 KiB")
    if contains_secret_value(value):
        raise ValueError("expected 不能包含密码、token、私钥或 API key 实际值")


def _text_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _text_hash(canonical)


def _selection_hash_compatibility(payload: dict[str, Any]) -> dict[str, Any]:
    """Do not change hashes of v4 artifacts created before per-channel coverage."""

    selections = payload.get("selections")
    if isinstance(selections, dict) and selections.get("techniques_by_channel") == {}:
        selections.pop("techniques_by_channel", None)
    return payload


def compute_design_content_hash(design: TestDesign) -> str:
    """审核内容指纹不包含生命周期状态。"""

    return _canonical_hash(
        _selection_hash_compatibility(
            design.model_dump(mode="json", exclude={"status"})
        )
    )


def compute_input_content_hash(snapshot: TestDesignInputSnapshot) -> str:
    """锁定模型实际看到的原始需求、前端选择、知识和审核反馈。"""

    return _canonical_hash(
        _selection_hash_compatibility(
            snapshot.model_dump(mode="json", exclude={"content_hash"})
        )
    )


def compute_review_content_hash(review: TestDesignReview | dict[str, Any]) -> str:
    """绑定审核决定、意见、时间以及所审核的设计和输入。"""

    if isinstance(review, TestDesignReview):
        payload = review.model_dump(mode="json", exclude={"review_content_hash"})
    else:
        payload = dict(review)
        payload.pop("review_content_hash", None)
    return _canonical_hash(payload)


def compute_validation_content_hash(
    report: TestDesignValidationReport | dict[str, Any],
) -> str:
    """绑定 passed 状态和全部阻塞、非阻塞校验提示。"""

    if isinstance(report, TestDesignValidationReport):
        payload = report.model_dump(mode="json", exclude={"validation_content_hash"})
    else:
        payload = dict(report)
        payload.pop("validation_content_hash", None)
    return _canonical_hash(payload)


def iter_design_texts(design: TestDesign):
    yield design.objective
    yield from design.in_scope
    yield from design.out_of_scope
    for scenario in design.scenarios:
        yield from scenario.required_states
        yield from scenario.operations
        yield from scenario.expected_results
        yield from scenario.data_requirements
        for data_requirement in scenario.data_requirements:
            yield from data_requirement.constraints
        yield scenario.state_impact.rationale
        if scenario.state_impact.cleanup_goal is not None:
            yield scenario.state_impact.cleanup_goal


def collect_requirement_ids(design: TestDesign) -> set[str]:
    return {
        requirement_id
        for scenario in design.scenarios
        for requirement_id in scenario.requirement_ids
    }
