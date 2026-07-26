"""TestDesign v4 的确定性门禁。"""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    DesignStatus,
    StateImpactKind,
    TestDesign,
    TestDesignInputSnapshot,
    TestDesignValidationReport,
    ValidationFinding,
    collect_requirement_ids,
    compute_design_content_hash,
    compute_input_content_hash,
    compute_validation_content_hash,
    contains_secret_literal,
    contains_secret_value,
)


class DesignValidator(Protocol):
    def validate(
        self,
        design: TestDesign,
        input_snapshot: TestDesignInputSnapshot,
    ) -> TestDesignValidationReport: ...


class DefaultDesignValidator:
    """验证可由代码证明的结构事实；业务语义仍由审核人确认。"""

    _unknown_values = {"", "unknown", "pending", "未确认"}

    def validate(
        self,
        design: TestDesign,
        input_snapshot: TestDesignInputSnapshot,
    ) -> TestDesignValidationReport:
        findings: list[ValidationFinding] = []

        def add(
            rule_id: str,
            message: str,
            field_path: str,
            blocking: bool = True,
        ) -> None:
            findings.append(
                ValidationFinding(
                    rule_id=rule_id,
                    message=message,
                    field_path=field_path,
                    blocking=blocking,
                )
            )

        def unknown(value: str | None) -> bool:
            return (value or "").strip().lower() in self._unknown_values

        def validate_text(value, field_path: str) -> None:
            if not value.text.strip():
                add("DESIGN_TEXT_REQUIRED", "业务文本不能为空", field_path + ".text")
            if contains_secret_literal(value.text) or contains_secret_literal(
                value.derivation_note or ""
            ):
                add("SECRET_LITERAL_PRESENT", "业务文本不能包含凭据实际值", field_path)
            if (value.derivation_note or "").strip():
                add(
                    "DERIVATION_HUMAN_REVIEW_REQUIRED",
                    "模型声明该内容包含测试设计推导；审核人必须确认输入确实支持它",
                    field_path + ".derivation_note",
                    blocking=False,
                )

        def validate_domain_id(
            value: str,
            seen: set[str],
            *,
            rule_id: str,
            label: str,
            field_path: str,
        ) -> None:
            if not value.strip() or value in seen:
                add(rule_id, f"{label} 不能为空或重复", field_path)
            seen.add(value)

        if input_snapshot.target.model_dump(mode="json") != design.target.model_dump(
            mode="json"
        ):
            add("INPUT_TARGET_MISMATCH", "模型输入与设计 target 不一致", "target")
        if input_snapshot.selections.model_dump(
            mode="json"
        ) != design.selections.model_dump(mode="json"):
            add(
                "INPUT_SELECTION_MISMATCH",
                "模型输入与设计 selections 不一致",
                "selections",
            )
        if unknown(design.target.system_id):
            add("TARGET_SYSTEM_REQUIRED", "前端必须确认目标系统", "target.system_id")
        if unknown(design.target.environment):
            add(
                "TARGET_ENVIRONMENT_REQUIRED",
                "前端必须确认目标环境",
                "target.environment",
            )
        if not design.title.strip():
            add("TITLE_REQUIRED", "测试设计标题不能为空", "title")

        validate_text(design.objective, "objective")
        for index, value in enumerate(design.in_scope):
            validate_text(value, f"in_scope[{index}]")
        for index, value in enumerate(design.out_of_scope):
            validate_text(value, f"out_of_scope[{index}]")

        known_requirements = {
            item.requirement_id for item in input_snapshot.requirements
        }
        scenario_ids: set[str] = set()
        required_state_ids: set[str] = set()
        operation_ids: set[str] = set()
        expectation_ids: set[str] = set()
        data_ids: set[str] = set()
        cleanup_goal_ids: set[str] = set()
        used_techniques = set()
        used_channels = set()
        used_channel_techniques = set()
        selected_techniques = set(design.selections.techniques)
        techniques_by_channel = {
            channel: set(values)
            for channel, values in design.selections.techniques_by_channel.items()
        }
        allowed_channels = set(design.selections.allowed_channels)
        required_channels = set(design.selections.required_channels)

        if not design.scenarios:
            add("SCENARIO_REQUIRED", "至少需要一个逻辑测试场景", "scenarios")
        for scenario_index, scenario in enumerate(design.scenarios):
            path = f"scenarios[{scenario_index}]"
            validate_domain_id(
                scenario.scenario_id,
                scenario_ids,
                rule_id="SCENARIO_ID_INVALID",
                label="scenario_id",
                field_path=path + ".scenario_id",
            )
            if not scenario.title.strip():
                add("SCENARIO_TITLE_REQUIRED", "场景标题不能为空", path + ".title")
            if not scenario.requirement_ids:
                add(
                    "SCENARIO_REQUIREMENT_REQUIRED",
                    "每个场景至少要关联一份原始需求",
                    path + ".requirement_ids",
                )
            unknown_requirements = set(scenario.requirement_ids) - known_requirements
            if unknown_requirements:
                add(
                    "SCENARIO_REQUIREMENT_UNKNOWN",
                    f"场景引用了不存在的 requirement_id: {sorted(unknown_requirements)}",
                    path + ".requirement_ids",
                )

            scenario_channels = {
                item.channel_hint for item in scenario.operations
            } | {
                item.channel_hint for item in scenario.expected_results
            }
            for technique in scenario.techniques:
                used_techniques.add(technique)
                if selected_techniques and technique not in selected_techniques:
                    add(
                        "SCENARIO_TECHNIQUE_NOT_SELECTED",
                        f"场景使用了前端未选择的 technique: {technique}",
                        path + ".techniques",
                    )
                if techniques_by_channel:
                    compatible_channels = {
                        channel
                        for channel in scenario_channels
                        if technique in techniques_by_channel.get(channel, set())
                    }
                    if not compatible_channels:
                        add(
                            "SCENARIO_TECHNIQUE_NOT_APPLICABLE",
                            "场景覆盖方式不适用于该场景实际使用的测试分类: "
                            f"{technique}",
                            path + ".techniques",
                        )
                    used_channel_techniques.update(
                        (channel, technique) for channel in compatible_channels
                    )

            if not scenario.operations:
                add(
                    "SCENARIO_OPERATION_REQUIRED",
                    "场景至少需要一个逻辑 operation",
                    path + ".operations",
                )
            if not scenario.expected_results:
                add(
                    "SCENARIO_EXPECTED_RESULT_REQUIRED",
                    "场景至少需要一个预期结果",
                    path + ".expected_results",
                )

            for index, required_state in enumerate(scenario.required_states):
                state_path = f"{path}.required_states[{index}]"
                validate_domain_id(
                    required_state.required_state_id,
                    required_state_ids,
                    rule_id="REQUIRED_STATE_ID_INVALID",
                    label="required_state_id",
                    field_path=state_path + ".required_state_id",
                )
                validate_text(required_state, state_path)

            scenario_operation_ids = {item.operation_id for item in scenario.operations}
            for index, operation in enumerate(scenario.operations):
                operation_path = f"{path}.operations[{index}]"
                validate_domain_id(
                    operation.operation_id,
                    operation_ids,
                    rule_id="OPERATION_ID_INVALID",
                    label="operation_id",
                    field_path=operation_path + ".operation_id",
                )
                validate_text(operation, operation_path)
                used_channels.add(operation.channel_hint)
                if operation.channel_hint not in allowed_channels:
                    add(
                        "OPERATION_CHANNEL_NOT_ALLOWED",
                        "operation 使用了前端未允许的 channel_hint: "
                        f"{operation.channel_hint.value}",
                        operation_path + ".channel_hint",
                    )

            for index, result in enumerate(scenario.expected_results):
                result_path = f"{path}.expected_results[{index}]"
                validate_domain_id(
                    result.expected_result_id,
                    expectation_ids,
                    rule_id="EXPECTED_RESULT_ID_INVALID",
                    label="expected_result_id",
                    field_path=result_path + ".expected_result_id",
                )
                validate_text(result, result_path)
                if result.after_operation_id not in scenario_operation_ids:
                    add(
                        "EXPECTED_OPERATION_UNKNOWN",
                        "expected result 引用了本场景不存在的 operation_id",
                        result_path + ".after_operation_id",
                    )
                used_channels.add(result.channel_hint)
                if result.channel_hint not in allowed_channels:
                    add(
                        "EXPECTED_CHANNEL_NOT_ALLOWED",
                        "expected result 使用了前端未允许的 channel_hint: "
                        f"{result.channel_hint.value}",
                        result_path + ".channel_hint",
                    )
                if contains_secret_value(result.model_dump(mode="json")):
                    add(
                        "EXPECTED_SECRET_PRESENT",
                        "结构化 expected 不能包含秘密值",
                        result_path,
                    )

            scenario_data_ids = {item.data_id for item in scenario.data_requirements}
            for index, data_requirement in enumerate(scenario.data_requirements):
                data_path = f"{path}.data_requirements[{index}]"
                validate_domain_id(
                    data_requirement.data_id,
                    data_ids,
                    rule_id="DATA_ID_INVALID",
                    label="data_id",
                    field_path=data_path + ".data_id",
                )
                validate_text(data_requirement, data_path)
                for constraint_index, constraint in enumerate(
                    data_requirement.constraints
                ):
                    validate_text(
                        constraint,
                        f"{data_path}.constraints[{constraint_index}]",
                    )

            impact_path = path + ".state_impact"
            validate_text(scenario.state_impact.rationale, impact_path + ".rationale")
            if scenario.state_impact.impact == StateImpactKind.UNKNOWN:
                add(
                    "STATE_IMPACT_UNKNOWN",
                    "状态影响未确认，不能批准",
                    impact_path + ".impact",
                )
            if scenario.state_impact.impact in {
                StateImpactKind.CREATES_DATA,
                StateImpactKind.CHANGES_STATE,
            } and scenario.state_impact.cleanup_goal is None:
                add(
                    "CLEANUP_GOAL_REQUIRED",
                    "创建数据或修改状态的场景必须声明清理目标",
                    impact_path + ".cleanup_goal",
                )
            if (
                scenario.state_impact.impact == StateImpactKind.READ_ONLY
                and scenario.state_impact.cleanup_goal is not None
            ):
                add(
                    "READ_ONLY_CLEANUP_FORBIDDEN",
                    "read_only 场景不能声明 cleanup_goal",
                    impact_path + ".cleanup_goal",
                )
            if scenario.state_impact.cleanup_goal is not None:
                cleanup = scenario.state_impact.cleanup_goal
                cleanup_path = impact_path + ".cleanup_goal"
                validate_domain_id(
                    cleanup.cleanup_goal_id,
                    cleanup_goal_ids,
                    rule_id="CLEANUP_GOAL_ID_INVALID",
                    label="cleanup_goal_id",
                    field_path=cleanup_path + ".cleanup_goal_id",
                )
                validate_text(cleanup, cleanup_path)
                unknown_subjects = set(cleanup.subject_data_ids) - scenario_data_ids
                if unknown_subjects:
                    add(
                        "CLEANUP_SUBJECT_UNKNOWN",
                        "cleanup goal 引用了本场景不存在的 data_id: "
                        f"{sorted(unknown_subjects)}",
                        cleanup_path + ".subject_data_ids",
                    )

        uncovered_requirements = known_requirements - collect_requirement_ids(design)
        if uncovered_requirements:
            add(
                "REQUIREMENT_LINK_HUMAN_REVIEW_REQUIRED",
                "以下原始需求未关联到测试场景；审核人需确认它们是无关、范围外还是被遗漏: "
                f"{sorted(uncovered_requirements)}",
                "scenarios.requirement_ids",
                blocking=False,
            )
        missing_techniques = selected_techniques - used_techniques
        if missing_techniques:
            add(
                "SELECTED_TECHNIQUE_UNCOVERED",
                "用户选择的测试技术没有生成场景: "
                f"{sorted(missing_techniques)}",
                "selections.techniques",
            )
        if techniques_by_channel:
            required_pairs = {
                (channel, technique)
                for channel, techniques in techniques_by_channel.items()
                for technique in techniques
            }
            missing_pairs = required_pairs - used_channel_techniques
            if missing_pairs:
                add(
                    "SELECTED_CHANNEL_TECHNIQUE_UNCOVERED",
                    "以下测试分类覆盖方式没有生成对应场景: "
                    + ", ".join(
                        f"{channel.value}/{technique.value}"
                        for channel, technique in sorted(
                            missing_pairs,
                            key=lambda item: (item[0].value, item[1].value),
                        )
                    ),
                    "selections.techniques_by_channel",
                )
        missing_channels = required_channels - used_channels
        if missing_channels:
            add(
                "REQUIRED_CHANNEL_UNCOVERED",
                "用户要求的 channel 没有被任何 operation/expected result 使用: "
                f"{sorted(item.value for item in missing_channels)}",
                "selections.required_channels",
            )
        for index, question in enumerate(design.open_questions):
            if not question.question.strip():
                add("OPEN_QUESTION_EMPTY", "未决问题不能为空", f"open_questions[{index}]")
            add(
                "OPEN_QUESTION_BLOCKING",
                "所有模型提出的未决问题都必须由修订后的原始需求解决后重新生成",
                f"open_questions[{index}]",
            )
        if design.status not in {DesignStatus.DRAFT, DesignStatus.IN_REVIEW}:
            add("STATUS_NOT_REVIEWABLE", "只有 draft/in_review 设计可校验", "status")
        if contains_secret_value(design.model_dump(mode="json")):
            add("SECRET_LITERAL_PRESENT", "TestDesign 不能包含秘密值", "design")

        payload = {
            "design_id": design.design_id,
            "design_version": design.version,
            "design_content_hash": compute_design_content_hash(design),
            "input_content_hash": compute_input_content_hash(input_snapshot),
            "passed": not any(item.blocking for item in findings),
            "findings": [item.model_dump(mode="json") for item in findings],
        }
        payload["validation_content_hash"] = compute_validation_content_hash(payload)
        return TestDesignValidationReport.model_validate(payload)
