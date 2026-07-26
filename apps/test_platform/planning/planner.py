"""执行计划智能体入口。"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, Type

from apps.test_platform.intent.contracts import (
    ApprovedTestDesignBundle,
    contains_secret_literal,
)
from apps.test_platform.input_contracts import validate_runtime_input

from .catalogs import PlanningCatalogSnapshot
from .contracts import ModelMessage, PlanCandidate, TestPlanDraft


def _catalog_selection_guide(catalog: PlanningCatalogSnapshot) -> dict[str, Any]:
    """Expose the two ref namespaces the model most often confuses."""

    return {
        "http_api": [
            {
                "catalog_ref": item.operation_ref,
                "observable_refs": [value.observable_ref for value in item.observables],
            }
            for item in catalog.http_operations
        ],
        "database": [
            {
                "catalog_ref": item.operation_ref,
                "observable_refs": [value.observable_ref for value in item.observables],
            }
            for item in catalog.database_operations
        ],
        "database_ai_constraints": (
            catalog.database_schema.model_dump(mode="json")
            if catalog.database_schema is not None
            else None
        ),
        "tcp_port": [
            {
                "catalog_ref": item.probe_ref,
                "observable_refs": [value.observable_ref for value in item.observables],
            }
            for item in catalog.tcp_port_probes
        ],
        "performance": [
            {
                "catalog_ref": item.profile_ref,
                "observable_refs": [value.observable_ref for value in item.observables],
            }
            for item in catalog.performance_profiles
        ],
        "procedure_playwright": [
            {
                "catalog_ref": operation.operation_ref,
                "observable_refs": [
                    value.observable_ref for value in profile.observables
                ],
            }
            for profile in catalog.procedure_profiles
            for operation in profile.operations
        ],
    }


class PlanModelGateway(Protocol):
    def generate(self, messages: list[ModelMessage], output_schema: Type[Any]) -> Any: ...


class PlanPromptBuilder(Protocol):
    def build(
        self,
        bundle: ApprovedTestDesignBundle,
        catalog: PlanningCatalogSnapshot,
        review_feedback: str | None = None,
        execution_input: dict[str, Any] | None = None,
    ) -> list[ModelMessage]: ...


class DefaultPlanPromptBuilder:
    """Generate an executable plan within the reviewed resource constraints."""

    def build(
        self,
        bundle: ApprovedTestDesignBundle,
        catalog: PlanningCatalogSnapshot,
        review_feedback: str | None = None,
        execution_input: dict[str, Any] | None = None,
    ) -> list[ModelMessage]:
        # Mutable in-memory objects must pass the public gates again before they
        # are serialized into a model prompt.
        bundle = ApprovedTestDesignBundle.model_validate(bundle.model_dump(mode="json"))
        catalog = PlanningCatalogSnapshot.model_validate(catalog.model_dump(mode="json"))
        frozen_input = validate_runtime_input(execution_input).model_dump(
            mode="json",
            exclude_none=True,
        )
        if contains_secret_literal(json.dumps(frozen_input, ensure_ascii=False)):
            raise ValueError("execution_input 不能包含凭据实际值")
        schema = PlanCandidate.model_json_schema()
        system = (
            "你是执行计划智能体。只输出符合给定 schema 的 JSON，不要输出解释。\n"
            "第一层 TestDesign 是测什么的唯一真值，PlanningCatalog 是当前目标环境可执行"
            "边界的唯一真值。除受控的 database_queries SQL 草稿外，你只能选择 catalog 中"
            "已经存在的 ref，不能创建或改写 ref。\n"
            "每个 scenario 只生成一个 flow，flow.stages 的列表顺序就是执行顺序；每个 stage "
            "只能选择一个 executor。不要生成 flow_id/stage_id，系统会按顺序确定性生成。"
            "每个 flow 只能引用其 scenario_id 对应 scenario 内的 operation_id、"
            "expected_result_id、data_id、required_state_id 和 cleanup_goal_id，不能跨场景引用。"
            "setup_stage 使用 1-based stage_index 引用候选 stages。\n"
            "所有 stage 联合且不重复覆盖 scenario.operations 和 expected_results；不要要求每个 "
            "stage 重复覆盖完整场景。operation_id/expected_result_id/data_id/required_state_id/"
            "cleanup_goal_id 只能引用第一层已有 ID。operation 和 expected 必须放入与其 "
            "channel_hint 对应的 executor stage，procedure_playwright 对应 ui，tcp_port 对应 port。\n"
            "expected_result 的 catalog_ref 是执行观察所需的 operation/profile，因此 UI 动作后"
            "允许在 database stage 选择只读查询进行验证。data binding.consumer_id 必须指向"
            "该 stage 的 operation_id、expected_result_id 或 setup required_state_id。\n"
            "每个 required_state 必须选择 data_guarantee(data_id) 或独立的 setup_stage。"
            "data_guarantee 只表示计划审核人和运行时 fixture provider 需要确认的外部预置数据"
            "假设，不表示系统已探测真实状态；没有可信预置数据时必须使用 setup_stage 或提出 open question。setup "
            "stage 必须在所有普通动作和验证之前，且不能混入普通 operation/expected。变更状态"
            "场景的 cleanup 只在 flow 级选择一次，并按 CleanupAction.required_data_slots 明确"
            "绑定 data_id 和 catalog binding_ref。\n"
            "API、端口等 stage 要把已审批的逻辑动作和预期映射到可执行 catalog 能力，最终代码"
            "由系统确定性编译。不要编造 catalog 未提供的 URL、host、driver、locator 或凭据。"
            "performance stage 必须根据已审批需求生成 performance_stages；每段包含 duration_seconds"
            "和 virtual_users；全部段的总时长不得超过所选 PerformanceProfile 的"
            "max_duration_seconds，任一段并发不得超过 max_virtual_users。不要把 profile 当作"
            "预制测试方案。"
            "测试资源只描述数据库访问边界，不保存历史 SQL。数据库优先级必须是：先复用"
            "approved_sql_knowledge 中适用的过往只读 SQL并填写 knowledge_scope_id；仍无法满足时"
            "才新生成 SQL。选择或生成的 SQL 都只能是一条 SELECT/WITH，只能引用 database_ai_constraints"
            "登记的表、字段和运行参数，禁止注释、字面凭据、写操作和管理语句。"
            "database_queries 的 expected_result_id/operator/expected 必须忠实翻译已审批预期，"
            "不得改变业务结果；check_column 必须是查询结果实际返回的列或别名。"
            "找不到可执行引用、数据库结构不足、目标不匹配或信息不足时，不要猜测，"
            "ExpectedResultSelection.catalog_ref 必须取下面选择索引中的 catalog_ref，"
            "observable_ref 必须取同项 observable_refs；绝不能把 observable_ref 填进 catalog_ref。"
            "UI operation 都是 Procedure 已发布模块；按业务操作逐项选择，并把同一场景的模块按"
            "批准顺序放入同一个 procedure_playwright stage。不得编造控件级 Action/Check。"
            "本次冻结输入中的 variables 是审核人在生成执行计划前提供的非秘密值。所有 executor "
            "都可以据此选择 Catalog data binding；数据库 SQL 必须参数化并在 parameters_refs 中"
            "引用变量名，严禁把输入值直接拼入 SQL、URL、Procedure 或脚本。变量与 Catalog "
            "不匹配时提出 open_question，不得猜测。performance_mode 只决定执行模式，不得用它"
            "改写测试计划中的负载模型和阈值。"
            "请在 open_questions 中说明并允许 flows 为空。\n"
            "候选 schema：\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        approved_design_input = {
            "design": bundle.design.model_dump(mode="json"),
            "design_content_hash": bundle.review.design_content_hash,
            "input_content_hash": bundle.review.input_content_hash,
            "trace_id": bundle.trace_id,
        }
        approved_sql_knowledge = [
            {
                "scope_id": item.scope_id,
                "knowledge_id": item.knowledge_id,
                "version": item.version,
                "content_hash": item.content_hash,
                "raw_content": item.content,
            }
            for item in bundle.input_snapshot.approved_knowledge
            if re.search(
                r"\b(?:select|with)\b[\s\S]{0,20000}\bfrom\b",
                item.content,
                re.IGNORECASE,
            )
        ]
        user = (
            "已审核 TestDesign（第一层原始需求、知识和审核意见不会在此重新解释）：\n"
            + json.dumps(approved_design_input, ensure_ascii=False, indent=2)
            + "\n\n目标环境 PlanningCatalogSnapshot：\n"
            + json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n\n严格选择索引（按 executor 分组，catalog_ref 与 observable_ref 不可互换）：\n"
            + json.dumps(_catalog_selection_guide(catalog), ensure_ascii=False, indent=2)
            + "\n\n已审核知识库中的过往 SQL 与作用说明（不包含时为空）：\n"
            + json.dumps(approved_sql_knowledge, ensure_ascii=False, indent=2)
            + "\n\n生成执行计划前已冻结的本次输入（仅用于变量绑定和执行模式）：\n"
            + json.dumps(frozen_input, ensure_ascii=False, indent=2)
        )
        messages = [
            ModelMessage(role="system", content=system),
            ModelMessage(role="user", content=user),
        ]
        if review_feedback is not None:
            feedback = str(review_feedback).strip()
            if not feedback or len(feedback.encode("utf-8")) > 4096:
                raise ValueError("review_feedback 必须为 1-4096 字节")
            if contains_secret_literal(feedback):
                raise ValueError("review_feedback 不能包含凭据实际值")
            messages.append(
                ModelMessage(
                    role="user",
                    content=(
                        "执行计划审核人的修订意见如下。它允许修正 executor/stage/ref 映射、"
                        "AI 只读 SQL、数据绑定和执行顺序，不是新的业务需求，也不能改写已审批"
                        "TestDesign、数据库结构约束或 PlanningCatalog：\n" + feedback
                    ),
                )
            )
        if sum(len(message.content.encode("utf-8")) for message in messages) > 1024 * 1024:
            raise ValueError("第二层完整模型 messages 不能超过 1 MiB")
        return messages


class PlanDraftGenerator:
    """模型产生引用选择，compiler 再确定性解析为计划。"""

    def __init__(self, prompt_builder: PlanPromptBuilder, model_gateway: PlanModelGateway, compiler=None):
        self.prompt_builder = prompt_builder
        self.model_gateway = model_gateway
        if compiler is None:
            from .compiler import TestPlanCompiler

            compiler = TestPlanCompiler()
        self.compiler = compiler

    def generate(
        self,
        bundle: ApprovedTestDesignBundle,
        catalog: PlanningCatalogSnapshot,
        *,
        plan_id: str | None = None,
        version: int = 1,
        review_feedback: str | None = None,
        execution_input: dict[str, Any] | None = None,
    ) -> TestPlanDraft:
        prompt_options = {}
        if review_feedback is not None:
            prompt_options["review_feedback"] = review_feedback
        if execution_input is not None:
            prompt_options["execution_input"] = execution_input
        prompt_messages = self.prompt_builder.build(bundle, catalog, **prompt_options)
        messages = [
            ModelMessage.model_validate(
                message.model_dump(mode="json")
                if callable(getattr(message, "model_dump", None))
                else message
            )
            for message in prompt_messages
        ]
        if not messages:
            raise ValueError("第二层完整模型 messages 不能为空")
        if sum(len(message.content.encode("utf-8")) for message in messages) > 1024 * 1024:
            raise ValueError("第二层完整模型 messages 不能超过 1 MiB")
        if any(contains_secret_literal(message.content) for message in messages):
            raise ValueError("第二层完整模型 messages 疑似包含凭据实际值")
        try:
            candidate = self._candidate(messages)
            return self.compiler.build_draft(
                bundle,
                candidate,
                catalog,
                plan_id=plan_id,
                version=version,
            )
        except ValueError as exc:
            # One bounded repair gives the model the deterministic compiler
            # finding without asking the user to click regenerate repeatedly.
            # Transport/provider failures are RuntimeError and are not retried.
            allowed_ids = []
            for scenario in bundle.design.scenarios:
                allowed_ids.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "operation_ids": [item.operation_id for item in scenario.operations],
                        "expected_result_ids": [
                            item.expected_result_id for item in scenario.expected_results
                        ],
                        "data_ids": [item.data_id for item in scenario.data_requirements],
                        "required_state_ids": [
                            item.required_state_id for item in scenario.required_states
                        ],
                        "cleanup_goal_ids": [
                            scenario.state_impact.cleanup_goal.cleanup_goal_id
                        ]
                        if scenario.state_impact.cleanup_goal is not None
                        else [],
                    }
                )
            repair = ModelMessage(
                role="user",
                content=(
                    "上一个完整候选未通过确定性校验："
                    + str(exc)[:4_000]
                    + "\n请重新输出完整 PlanCandidate JSON。只能使用各自 scenario 中的 ID "
                    "和 PlanningCatalog 已存在的 ref；不要解释，也不要只输出补丁。\n"
                    "ExpectedResultSelection.catalog_ref 只能取选择索引的 catalog_ref；"
                    "observable_ref 只能取对应 observable_refs，二者绝不能互换。\n"
                    "严格选择索引：\n"
                    + json.dumps(_catalog_selection_guide(catalog), ensure_ascii=False)
                    + "\n"
                    "以下是不可跨场景使用的合法 ID 映射：\n"
                    + json.dumps(allowed_ids, ensure_ascii=False)
                ),
            )
            repaired_messages = [*messages, repair]
            if sum(
                len(message.content.encode("utf-8"))
                for message in repaired_messages
            ) > 1024 * 1024:
                raise ValueError("第二层纠错模型 messages 不能超过 1 MiB") from exc
            candidate = self._candidate(repaired_messages)
            return self.compiler.build_draft(
                bundle,
                candidate,
                catalog,
                plan_id=plan_id,
                version=version,
            )

    def _candidate(self, messages: list[ModelMessage]) -> PlanCandidate:
        candidate = self.model_gateway.generate(messages, PlanCandidate)
        if isinstance(candidate, PlanCandidate):
            return candidate
        return PlanCandidate.model_validate(candidate)


__all__ = [
    "DefaultPlanPromptBuilder",
    "PlanDraftGenerator",
    "PlanModelGateway",
    "PlanPromptBuilder",
]
