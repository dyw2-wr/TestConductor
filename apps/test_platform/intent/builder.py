"""模型候选生成和系统编译。"""

from __future__ import annotations

from itertools import count
import re
from typing import Protocol

from .contracts import (
    ApprovedKnowledge,
    CleanupGoal,
    DataRequirement,
    DesignText,
    ExpectedResult,
    LogicalScenario,
    OpenQuestion,
    Operation,
    RequiredState,
    StateImpact,
    TestDesign,
    TestDesignCandidate,
    TestDesignRequest,
    contains_secret_literal,
)
from .model_gateway import DesignModelGateway
from .prompt_builder import DesignPromptBuilder


def _normalize_channel_expectation(result):
    """Fill only obvious machine predicates for non-UI channel text.

    The reviewer still sees the original statement.  This small bridge keeps
    port plans executable when a model writes "连接成功" or "不超过 1000ms"
    without filling the optional predicate fields.  Ambiguous text is left
    untouched so the second layer can block it instead of guessing.
    """

    operator = {
        "==": "equals",
        "=": "equals",
        "!=": "not_equals",
        "<=": "lte",
        "<": "lt",
        ">=": "gte",
        ">": "gt",
    }.get(result.operator, result.operator)
    if result.channel_hint.value != "port":
        return operator, result.expected, result.unit
    if result.expected is not None:
        if result.expected in {"open", "closed", "filtered"} and operator is None:
            operator = "equals"
        return operator, result.expected, result.unit
    text = result.text.lower()
    state = None
    if "open" in text or "连接建立成功" in text or "可以连接" in text or "连通" in text:
        state = "open"
    elif "closed" in text or "关闭" in text:
        state = "closed"
    elif "filtered" in text or "过滤" in text:
        state = "filtered"
    if state is not None:
        return operator or "equals", state, result.unit
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:毫秒|ms)", text)
    if match and any(token in text for token in ("不超过", "不高于", "小于等于", "<=", "至多")):
        return operator or "lte", float(match.group(1)), result.unit or "ms"
    return operator, result.expected, result.unit


def _is_port_property_state(state, operations) -> bool:
    """Do not model the port property under test as a precondition."""

    if not any(item.channel_hint.value == "port" for item in operations):
        return False
    text = state.text.lower()
    return any(token in text for token in ("监听", "listening", "端口已打开", "port is open"))


def _is_database_property_state(state, operations, expected_results, data_requirements) -> bool:
    """Drop a database fact that the scenario is supposed to observe.

    Models sometimes restate ``records exist`` as a required precondition even
    though the expected result is the read-only existence/count assertion.  If
    no fixture data was requested, treating that statement as setup makes an
    otherwise executable database check impossible to plan.
    """

    if data_requirements or not any(
        item.channel_hint.value == "database" for item in operations
    ):
        return False
    state_text = state.text.lower()
    describes_existing_rows = (
        ("record" in state_text and any(token in state_text for token in ("contain", "exist")))
        or ("row" in state_text and any(token in state_text for token in ("contain", "exist")))
        or (
            any(token in state_text for token in ("记录", "数据"))
            and any(token in state_text for token in ("存在", "包含", "已有"))
        )
    )
    if not describes_existing_rows:
        return False
    return any(
        item.channel_hint.value == "database"
        and (
            item.operator in {"exists", "not_exists", "gt", "gte"}
            or any(
                token in item.text.lower()
                for token in ("record", "row", "count", "记录", "数量", "存在")
            )
        )
        for item in expected_results
    )


class DesignBuilder(Protocol):
    def build_candidate(
        self,
        request: TestDesignRequest,
        approved_knowledge: list[ApprovedKnowledge],
        review_feedback: str | None = None,
    ) -> TestDesignCandidate: ...

    def compile(
        self,
        request: TestDesignRequest,
        candidate: TestDesignCandidate,
        *,
        design_id: str | None = None,
        version: int = 1,
    ) -> TestDesign: ...


class DefaultDesignBuilder:
    def __init__(
        self,
        prompt_builder: DesignPromptBuilder,
        model_gateway: DesignModelGateway,
    ):
        self.prompt_builder = prompt_builder
        self.model_gateway = model_gateway

    def build_candidate(
        self,
        request: TestDesignRequest,
        approved_knowledge: list[ApprovedKnowledge],
        review_feedback: str | None = None,
    ) -> TestDesignCandidate:
        messages = self.prompt_builder.build(
            request,
            approved_knowledge,
            review_feedback=review_feedback,
        )
        if any(contains_secret_literal(message.content) for message in messages):
            raise ValueError("完整模型 messages 疑似包含凭据实际值")
        if sum(len(message.content.encode("utf-8")) for message in messages) > 512 * 1024:
            raise ValueError("完整模型 messages 不能超过 512 KiB")
        generated = self.model_gateway.generate(messages, TestDesignCandidate)
        if isinstance(generated, TestDesignCandidate):
            return generated
        return TestDesignCandidate.model_validate(generated)

    def compile(
        self,
        request: TestDesignRequest,
        candidate: TestDesignCandidate,
        *,
        design_id: str | None = None,
        version: int = 1,
    ) -> TestDesign:
        if version < 1:
            raise ValueError("version 必须从 1 开始")
        if request.request_id is None:
            raise ValueError("编译前必须由 pipeline 分配 request_id")
        resolved_design_id = design_id or f"design-{request.request_id}"
        required_state_sequence = count(1)
        operation_sequence = count(1)
        expectation_sequence = count(1)
        data_sequence = count(1)
        cleanup_sequence = count(1)

        def design_text(value) -> DesignText:
            return DesignText(
                text=value.text,
                derivation_note=value.derivation_note,
            )

        scenarios: list[LogicalScenario] = []
        for scenario_index, value in enumerate(candidate.scenarios, start=1):
            operations = [
                Operation(
                    operation_id=(
                        f"{resolved_design_id}-OP-{next(operation_sequence):04d}"
                    ),
                    text=item.text,
                    derivation_note=item.derivation_note,
                    channel_hint=item.channel_hint,
                )
                for item in value.operations
            ]
            required_states = [
                RequiredState(
                    required_state_id=(
                        f"{resolved_design_id}-REQSTATE-"
                        f"{next(required_state_sequence):04d}"
                    ),
                    text=item.text,
                    derivation_note=item.derivation_note,
                )
                for item in value.required_states
                if not _is_port_property_state(item, value.operations)
                and not _is_database_property_state(
                    item,
                    value.operations,
                    value.expected_results,
                    value.data_requirements,
                )
            ]
            expected_results = []
            for result in value.expected_results:
                operator, expected, unit = _normalize_channel_expectation(result)
                expected_results.append(
                    ExpectedResult(
                        expected_result_id=(
                            f"{resolved_design_id}-EXP-{next(expectation_sequence):04d}"
                        ),
                        after_operation_id=(
                            operations[result.after_operation_index - 1].operation_id
                        ),
                        text=result.text,
                        derivation_note=result.derivation_note,
                        channel_hint=result.channel_hint,
                        operator=operator,
                        expected=expected,
                        unit=unit,
                    )
                )
            data_requirements = [
                DataRequirement(
                    data_id=f"{resolved_design_id}-DATA-{next(data_sequence):04d}",
                    text=item.text,
                    derivation_note=item.derivation_note,
                    constraints=[design_text(constraint) for constraint in item.constraints],
                )
                for item in value.data_requirements
            ]
            cleanup_candidate = value.state_impact.cleanup_goal
            cleanup_goal = (
                CleanupGoal(
                    cleanup_goal_id=(
                        f"{resolved_design_id}-CLEANUP-{next(cleanup_sequence):04d}"
                    ),
                    text=cleanup_candidate.text,
                    derivation_note=cleanup_candidate.derivation_note,
                    subject_data_ids=[
                        data_requirements[index - 1].data_id
                        for index in cleanup_candidate.subject_data_indexes
                    ],
                )
                if cleanup_candidate is not None
                else None
            )
            scenarios.append(
                LogicalScenario(
                    scenario_id=f"{resolved_design_id}-SCN-{scenario_index:04d}",
                    title=value.title,
                    techniques=list(value.techniques),
                    requirement_ids=list(dict.fromkeys(value.requirement_ids)),
                    required_states=required_states,
                    operations=operations,
                    expected_results=expected_results,
                    data_requirements=data_requirements,
                    state_impact=StateImpact(
                        impact=value.state_impact.impact,
                        rationale=design_text(value.state_impact.rationale),
                        cleanup_goal=cleanup_goal,
                    ),
                )
            )
        return TestDesign(
            design_id=resolved_design_id,
            version=version,
            title=candidate.title,
            objective=design_text(candidate.objective),
            target=request.target.model_copy(deep=True),
            selections=request.selections.model_copy(deep=True),
            in_scope=[design_text(item) for item in candidate.in_scope],
            out_of_scope=[design_text(item) for item in candidate.out_of_scope],
            scenarios=scenarios,
            open_questions=[
                OpenQuestion(
                    question_id=f"{resolved_design_id}-Q-{index:04d}",
                    question=item.question,
                    field_path=item.field_path,
                )
                for index, item in enumerate(candidate.open_questions, start=1)
            ],
        )
