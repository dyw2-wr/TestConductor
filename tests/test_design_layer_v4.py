from __future__ import annotations

import hashlib
import unittest

from pydantic import ValidationError

from apps.test_platform.intent.builder import DefaultDesignBuilder
from apps.test_platform.intent.contracts import (
    ApprovedKnowledge,
    ApprovedTestDesignBundle,
    DesignSelections,
    RequirementInput,
    ReviewDecision,
    TargetSelection,
    TestDesign,
    TestDesignCandidate as DesignCandidateModel,
    TestDesignRequest as DesignRequest,
    TestDesignValidationReport,
    compute_review_content_hash,
    compute_validation_content_hash,
    contains_secret_literal,
)
from apps.test_platform.intent.knowledge import InMemoryApprovedKnowledgeResolver
from apps.test_platform.intent.prompt_builder import DefaultDesignPromptBuilder
from apps.test_platform.intent.service import TestDesignPipeline as DesignPipeline


class RecordingGateway:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.messages = []

    def generate(self, messages, output_schema):
        self.calls += 1
        self.messages = messages
        return self.payload


def _hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _request(
    *,
    scopes=None,
    techniques=None,
    allowed_channels=None,
    required_channels=None,
    requirements=None,
):
    return DesignRequest(
        request_id="REQ-LOGIN-001",
        requirements=requirements
        or [
            RequirementInput(
                requirement_id="REQ-LOGIN",
                content=(
                    "登录锁定需求\n"
                    "使用可恢复的测试账号。\n"
                    "连续输入错误密码 5 次。\n"
                    "账号被锁定并显示账号已锁定。\n"
                    "测试结束后恢复测试账号。"
                ),
            )
        ],
        target=TargetSelection(system_id="account-web", environment="staging"),
        selections=DesignSelections(
            techniques=["positive"] if techniques is None else techniques,
            allowed_channels=(
                ["ui"] if allowed_channels is None else allowed_channels
            ),
            required_channels=([] if required_channels is None else required_channels),
            knowledge_scope_ids=(
                ["ui:login-approved"] if scopes is None else scopes
            ),
        ),
    )


def _approved_knowledge():
    content = "登录页面入口为 /login"
    return ApprovedKnowledge(
        scope_id="ui:login-approved",
        knowledge_id="UI-APPROVED",
        version=1,
        approval_id="KNOWLEDGE-REVIEW-1",
        approved_at="2026-07-18T00:00:00Z",
        content=content,
        content_hash=_hash(content),
    )


def _valid_candidate():
    return {
        "title": "登录错误锁定",
        "objective": {"text": "连续输入错误密码 5 次"},
        "in_scope": [{"text": "账号被锁定并显示账号已锁定"}],
        "out_of_scope": [],
        "scenarios": [
            {
                "title": "连续错误密码锁定账号",
                "techniques": ["positive"],
                "requirement_ids": ["REQ-LOGIN"],
                "required_states": [{"text": "测试开始前账号未锁定"}],
                "operations": [
                    {"text": "连续输入错误密码 5 次", "channel_hint": "ui"}
                ],
                "expected_results": [
                    {
                        "text": "账号被锁定并显示账号已锁定",
                        "after_operation_index": 1,
                        "channel_hint": "ui",
                    }
                ],
                "data_requirements": [
                    {
                        "text": "使用可恢复的测试账号",
                        "constraints": [{"text": "测试开始前账号未锁定"}],
                    }
                ],
                "state_impact": {
                    "impact": "changes_state",
                    "rationale": {"text": "账号被锁定"},
                    "cleanup_goal": {
                        "text": "测试结束后恢复测试账号",
                        "subject_data_indexes": [1],
                    },
                },
            }
        ],
        "open_questions": [],
    }


def _pipeline(payload, knowledge=None):
    gateway = RecordingGateway(payload)
    resolver = InMemoryApprovedKnowledgeResolver(
        [_approved_knowledge()] if knowledge is None else knowledge
    )
    pipeline = DesignPipeline(
        DefaultDesignBuilder(DefaultDesignPromptBuilder(), gateway),
        knowledge_resolver=resolver,
    )
    return pipeline, gateway


class TestDesignLayerV4Tests(unittest.TestCase):
    def test_port_expectations_gain_only_explicit_machine_predicates(self):
        payload = _valid_candidate()
        scenario = payload["scenarios"][0]
        scenario["required_states"] = [
            {"text": "服务已监听待测端口"},
        ]
        scenario["operations"] = [
            {"text": "探测目标端口", "channel_hint": "port"}
        ]
        scenario["expected_results"] = [
            {
                "text": "端口连接建立成功并显示为 open",
                "after_operation_index": 1,
                "channel_hint": "port",
                "expected": "open",
            },
            {
                "text": "连接延迟不超过 1000 毫秒",
                "after_operation_index": 1,
                "channel_hint": "port",
                "operator": "<=",
                "expected": 1000,
                "unit": "ms",
            },
        ]
        scenario["data_requirements"] = []
        scenario["state_impact"] = {
            "impact": "read_only",
            "rationale": {"text": "仅建立 TCP 连接"},
            "cleanup_goal": None,
        }
        pipeline, _ = _pipeline(payload, knowledge=[])
        result = pipeline.generate(
            _request(scopes=[], allowed_channels=["port"], required_channels=["port"])
        )

        expectations = result.design.scenarios[0].expected_results
        self.assertEqual(result.design.scenarios[0].required_states, [])
        self.assertEqual(
            (expectations[0].operator, expectations[0].expected, expectations[0].unit),
            ("equals", "open", None),
        )
        self.assertEqual(
            (expectations[1].operator, expectations[1].expected, expectations[1].unit),
            ("lte", 1000.0, "ms"),
        )

    def test_database_fact_under_test_is_not_compiled_as_setup_precondition(self):
        payload = _valid_candidate()
        scenario = payload["scenarios"][0]
        scenario["required_states"] = [
            {"text": "The database contains Django migration records before inspection."},
        ]
        scenario["operations"] = [
            {"text": "Inspect Django migration records", "channel_hint": "database"}
        ]
        scenario["expected_results"] = [
            {
                "text": "The number of migration records is greater than 0.",
                "after_operation_index": 1,
                "channel_hint": "database",
                "operator": "gt",
                "expected": 0,
            }
        ]
        scenario["data_requirements"] = []
        scenario["state_impact"] = {
            "impact": "read_only",
            "rationale": {"text": "只读检查迁移记录"},
            "cleanup_goal": None,
        }
        pipeline, _ = _pipeline(payload, knowledge=[])
        result = pipeline.generate(
            _request(
                scopes=[],
                allowed_channels=["database"],
                required_channels=["database"],
            )
        )

        self.assertEqual(result.design.scenarios[0].required_states, [])

    def test_raw_requirement_reaches_model_unchanged_without_parsing(self):
        raw = "没有标题也没有编号。第一句和第二句连在一起；（一）这种格式也保持原样。"
        request = _request(
            scopes=[],
            requirements=[RequirementInput(content=raw)],
        )
        payload = _valid_candidate()
        payload["scenarios"][0]["requirement_ids"] = ["REQ-0001"]
        pipeline, gateway = _pipeline(payload, knowledge=[])
        result = pipeline.generate(request)

        prompt = "\n".join(message.content for message in gateway.messages)
        self.assertIn(raw, prompt)
        self.assertEqual(result.request.requirements[0].content, raw)
        self.assertEqual(result.request.requirements[0].requirement_id, "REQ-0001")
        self.assertEqual(result.input_snapshot.requirements[0].content, raw)
        self.assertEqual(result.input_snapshot.requirements[0].content_hash, _hash(raw))
        self.assertTrue(result.validation.passed, result.validation.findings)

    def test_approved_knowledge_and_input_are_bound_to_review_bundle(self):
        pipeline, gateway = _pipeline(_valid_candidate())
        result = pipeline.generate(_request())
        prompt = "\n".join(message.content for message in gateway.messages)
        self.assertIn("登录页面入口为 /login", prompt)
        self.assertEqual(result.design.target.system_id, "account-web")
        self.assertTrue(result.validation.passed, result.validation.findings)

        approved, review = pipeline.review(
            result,
            decision=ReviewDecision.APPROVED,
            comments="原始需求和逻辑场景已核对",
        )
        bundle = pipeline.build_approved_bundle(result, approved, review)
        self.assertEqual(bundle.schema_version, "approved-test-design-bundle.v4")
        self.assertEqual(bundle.design.schema_version, "test-design.v4")
        self.assertEqual(
            bundle.input_snapshot.approved_knowledge[0].approval_id,
            "KNOWLEDGE-REVIEW-1",
        )

        mutated = bundle.model_dump(mode="json")
        mutated["design"]["title"] = "未审核的篡改"
        with self.assertRaisesRegex(ValidationError, "实际 TestDesign"):
            ApprovedTestDesignBundle.model_validate(mutated)

        mutated = bundle.model_dump(mode="json")
        mutated["input_snapshot"]["requirements"][0]["content"] += "篡改"
        with self.assertRaisesRegex(ValidationError, "content_hash"):
            ApprovedTestDesignBundle.model_validate(mutated)

        mutated = bundle.model_dump(mode="json")
        mutated["review"]["comments"] = "被改写的审核意见"
        with self.assertRaisesRegex(ValidationError, "review_content_hash"):
            ApprovedTestDesignBundle.model_validate(mutated)

        mutated = bundle.model_dump(mode="json")
        mutated["review"]["comments"] = "API_KEY=actual-review-secret"
        mutated["review"]["review_content_hash"] = compute_review_content_hash(
            mutated["review"]
        )
        with self.assertRaisesRegex(ValidationError, "凭据实际值"):
            ApprovedTestDesignBundle.model_validate(mutated)

        mutated = bundle.model_dump(mode="json")
        mutated["trace_id"] = "trace-forged"
        with self.assertRaisesRegex(ValidationError, "trace_id"):
            ApprovedTestDesignBundle.model_validate(mutated)

        mutated = bundle.model_dump(mode="json")
        mutated["review"]["comments"] = "x" * 4097
        mutated["review"]["review_content_hash"] = compute_review_content_hash(
            mutated["review"]
        )
        with self.assertRaisesRegex(ValidationError, "4096"):
            ApprovedTestDesignBundle.model_validate(mutated)

    def test_candidate_is_strict_and_frontend_fields_cannot_come_from_model(self):
        payload = _valid_candidate()
        payload["target"] = {"system_id": "model-invented", "environment": "prod"}
        pipeline, _ = _pipeline(payload)
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            pipeline.generate(_request())

        payload = _valid_candidate()
        payload["objective"]["legacy_provenance"] = ["obsolete-reference"]
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            DesignCandidateModel.model_validate(payload)

        payload = _valid_candidate()
        payload["scenarios"][0]["expected_results"][0]["observable"] = (
            "旧自然语言观察备注"
        )
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            DesignCandidateModel.model_validate(payload)

        payload = _valid_candidate()
        payload["scenarios"][0]["operations"][0]["operation_id"] = "MODEL-OP-1"
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            DesignCandidateModel.model_validate(payload)

        payload = _valid_candidate()
        payload["scenarios"][0]["state_impact"]["cleanup_goal"][
            "cleanup_goal_id"
        ] = "MODEL-CLEANUP-1"
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            DesignCandidateModel.model_validate(payload)

    def test_candidate_indexes_compile_to_domain_ids(self):
        pipeline, _ = _pipeline(_valid_candidate())
        result = pipeline.generate(_request())
        scenario = result.design.scenarios[0]

        self.assertFalse(hasattr(result.design.objective, "statement_id"))
        self.assertTrue(scenario.required_states[0].required_state_id.endswith("REQSTATE-0001"))
        self.assertTrue(scenario.operations[0].operation_id.endswith("OP-0001"))
        self.assertTrue(scenario.expected_results[0].expected_result_id.endswith("EXP-0001"))
        self.assertEqual(
            scenario.expected_results[0].after_operation_id,
            scenario.operations[0].operation_id,
        )
        self.assertTrue(scenario.data_requirements[0].data_id.endswith("DATA-0001"))
        self.assertEqual(
            scenario.state_impact.cleanup_goal.subject_data_ids,
            [scenario.data_requirements[0].data_id],
        )
        self.assertEqual(
            scenario.data_requirements[0].constraints[0].text,
            "测试开始前账号未锁定",
        )
        self.assertFalse(hasattr(scenario.state_impact.rationale, "statement_id"))

    def test_final_design_rejects_unknown_domain_references(self):
        pipeline, _ = _pipeline(_valid_candidate())
        result = pipeline.generate(_request())

        payload = result.design.model_dump(mode="json")
        payload["scenarios"][0]["expected_results"][0][
            "after_operation_id"
        ] = "UNKNOWN-OP"
        with self.assertRaisesRegex(ValidationError, "不存在的 operation_id"):
            TestDesign.model_validate(payload)

        payload = result.design.model_dump(mode="json")
        payload["scenarios"][0]["state_impact"]["cleanup_goal"][
            "subject_data_ids"
        ] = ["UNKNOWN-DATA"]
        with self.assertRaisesRegex(ValidationError, "不存在的 data_id"):
            TestDesign.model_validate(payload)

    def test_candidate_rejects_out_of_range_operation_and_data_indexes(self):
        payload = _valid_candidate()
        payload["scenarios"][0]["expected_results"][0][
            "after_operation_index"
        ] = 2
        with self.assertRaisesRegex(ValidationError, "超出 operations 范围"):
            DesignCandidateModel.model_validate(payload)

        payload = _valid_candidate()
        payload["scenarios"][0]["state_impact"]["cleanup_goal"][
            "subject_data_indexes"
        ] = [2]
        with self.assertRaisesRegex(ValidationError, "超出 data_requirements 范围"):
            DesignCandidateModel.model_validate(payload)

        payload = _valid_candidate()
        payload["scenarios"][0]["state_impact"]["cleanup_goal"][
            "subject_data_indexes"
        ] = [0]
        with self.assertRaisesRegex(ValidationError, "greater than 0"):
            DesignCandidateModel.model_validate(payload)

    def test_one_scenario_can_cover_multiple_techniques_and_channel_roles(self):
        payload = _valid_candidate()
        scenario = payload["scenarios"][0]
        scenario["techniques"] = ["boundary", "negative"]
        scenario["expected_results"].append(
            {
                "text": "账号锁定状态为 true",
                "after_operation_index": 1,
                "channel_hint": "database",
                "operator": "equals",
                "expected": True,
            }
        )
        pipeline, _ = _pipeline(payload)
        result = pipeline.generate(
            _request(
                techniques=["boundary", "negative"],
                allowed_channels=["ui", "database"],
            )
        )

        self.assertTrue(result.validation.passed, result.validation.findings)
        self.assertEqual(result.design.scenarios[0].techniques, ["boundary", "negative"])
        self.assertEqual(
            [item.channel_hint.value for item in result.design.scenarios[0].expected_results],
            ["ui", "database"],
        )

    def test_free_form_requirement_does_not_require_a_fixed_technique_selection(self):
        payload = _valid_candidate()
        payload["scenarios"][0]["techniques"] = ["长时间稳定性验证"]
        pipeline, gateway = _pipeline(payload, knowledge=[])

        result = pipeline.generate(_request(techniques=[], scopes=[]))

        self.assertTrue(result.validation.passed, result.validation.findings)
        self.assertEqual(result.design.selections.techniques, [])
        self.assertEqual(
            result.design.scenarios[0].techniques,
            ["长时间稳定性验证"],
        )
        prompt = "\n".join(message.content for message in gateway.messages)
        self.assertIn("测试类型和测试要求以 raw_content 为准", prompt)

        payload = _valid_candidate()
        payload["open_questions"] = [
            {"question": "环境是否正确？", "blocking": False}
        ]
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            DesignCandidateModel.model_validate(payload)

    def test_unapproved_scope_blocks_before_model_call(self):
        pipeline, gateway = _pipeline(_valid_candidate(), knowledge=[])
        with self.assertRaisesRegex(ValueError, "未批准或不存在"):
            pipeline.generate(_request(scopes=["ui:not-approved"]))
        self.assertEqual(gateway.calls, 0)

    def test_complete_model_messages_reject_secret_in_target(self):
        pipeline, gateway = _pipeline(_valid_candidate())
        request = _request().model_copy(
            update={
                "target": TargetSelection(
                    system_id="API_KEY=actual-target-secret",
                    environment="staging",
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "模型 messages"):
            pipeline.generate(request)
        self.assertEqual(gateway.calls, 0)

    def test_read_only_scenario_cannot_declare_cleanup(self):
        candidate = _valid_candidate()
        candidate["scenarios"][0]["state_impact"]["impact"] = "read_only"
        pipeline, _ = _pipeline(candidate)
        result = pipeline.generate(_request())
        self.assertFalse(result.validation.passed)
        self.assertIn(
            "READ_ONLY_CLEANUP_FORBIDDEN",
            {item.rule_id for item in result.validation.findings},
        )

    def test_validation_passed_must_match_blocking_findings(self):
        pipeline, _ = _pipeline(_valid_candidate())
        result = pipeline.generate(_request())
        payload = result.validation.model_dump(mode="json")
        payload["findings"].append(
            {
                "rule_id": "FORGED_BLOCKING",
                "message": "blocking finding",
                "field_path": "scenarios",
                "blocking": True,
            }
        )
        payload["validation_content_hash"] = compute_validation_content_hash(payload)
        with self.assertRaisesRegex(ValidationError, "passed"):
            TestDesignValidationReport.model_validate(payload)

    def test_changes_requested_regenerates_same_design_with_feedback(self):
        pipeline, _ = _pipeline(_valid_candidate())
        result = pipeline.generate(_request())
        changed, review = pipeline.review(
            result,
            decision=ReviewDecision.CHANGES_REQUESTED,
            comments="补充测试账号恢复条件后重新生成",
        )
        self.assertEqual(changed.status.value, "changes_requested")
        with self.assertRaisesRegex(ValueError, "已审核|原地批准"):
            pipeline.reviewer.review(
                changed,
                result.validation,
                result.input_snapshot,
                ReviewDecision.APPROVED,
                "未重新生成就尝试批准旧版本",
            )
        with self.assertRaisesRegex(ValueError, "已审核"):
            pipeline.review(
                result,
                decision=ReviewDecision.APPROVED,
                comments="重放首次生成对象尝试绕过重新生成",
            )
        regenerated = pipeline.regenerate(result, review, result.request)
        self.assertEqual(regenerated.design.design_id, result.design.design_id)
        self.assertEqual(regenerated.design.version, result.design.version + 1)
        self.assertEqual(regenerated.design.status.value, "draft")
        self.assertEqual(
            regenerated.input_snapshot.review_feedback,
            "补充测试账号恢复条件后重新生成",
        )
        prompt = "\n".join(
            message.content for message in pipeline.design_builder.model_gateway.messages
        )
        self.assertIn("补充测试账号恢复条件后重新生成", prompt)

        changed_target = result.request.model_copy(
            update={"target": TargetSelection(system_id="other", environment="staging")}
        )
        with self.assertRaisesRegex(ValueError, "不能更换 target"):
            pipeline.regenerate(result, review, changed_target)

        implicit_ids = result.request.model_copy(
            update={"requirements": [RequirementInput(content="修订后的登录需求")]}
        )
        with self.assertRaisesRegex(ValueError, "必须携带.*requirement_id"):
            pipeline.regenerate(result, review, implicit_ids)

        mutated_review = review.model_copy()
        mutated_review.comments = "审核后被改写的反馈"
        with self.assertRaisesRegex(ValueError, "不匹配"):
            pipeline.regenerate(result, mutated_review, result.request)

        wrong_validation_review = review.model_copy(
            update={"validation_content_hash": "sha256:" + "f" * 64}
        )
        wrong_validation_review.review_content_hash = compute_review_content_hash(
            wrong_validation_review
        )
        with self.assertRaisesRegex(ValueError, "不匹配"):
            pipeline.regenerate(result, wrong_validation_review, result.request)

        with self.assertRaisesRegex(ValueError, "4096"):
            pipeline.review(
                result,
                decision=ReviewDecision.CHANGES_REQUESTED,
                comments="x" * 4097,
            )

    def test_requirement_and_selection_coverage_are_deterministic_gates(self):
        requirements = [
            RequirementInput(requirement_id="REQ-LOGIN", content="验证登录锁定"),
            RequirementInput(requirement_id="REQ-RECOVERY", content="测试结束后恢复账号"),
        ]
        payload = _valid_candidate()
        pipeline, _ = _pipeline(payload)
        result = pipeline.generate(_request(requirements=requirements))
        self.assertTrue(result.validation.passed)
        self.assertIn(
            "REQUIREMENT_LINK_HUMAN_REVIEW_REQUIRED",
            {item.rule_id for item in result.validation.findings},
        )
        self.assertFalse(
            next(
                item
                for item in result.validation.findings
                if item.rule_id == "REQUIREMENT_LINK_HUMAN_REVIEW_REQUIRED"
            ).blocking
        )
        approved, review = pipeline.review(
            result,
            decision=ReviewDecision.APPROVED,
            comments="已确认未关联需求属于范围外",
        )
        bundle = pipeline.build_approved_bundle(result, approved, review)
        mutated = bundle.model_dump(mode="json")
        mutated["validation"]["findings"] = []
        with self.assertRaisesRegex(ValidationError, "validation_content_hash"):
            ApprovedTestDesignBundle.model_validate(mutated)

        payload = _valid_candidate()
        payload["scenarios"][0]["requirement_ids"] = ["REQ-NOT-EXISTS"]
        pipeline, _ = _pipeline(payload)
        result = pipeline.generate(_request())
        self.assertFalse(result.validation.passed)
        self.assertIn(
            "SCENARIO_REQUIREMENT_UNKNOWN",
            {item.rule_id for item in result.validation.findings},
        )

        payload = _valid_candidate()
        pipeline, _ = _pipeline(payload)
        result = pipeline.generate(_request(techniques=["positive", "boundary"]))
        self.assertFalse(result.validation.passed)
        self.assertIn(
            "SELECTED_TECHNIQUE_UNCOVERED",
            {item.rule_id for item in result.validation.findings},
        )

        payload = _valid_candidate()
        payload["scenarios"][0]["operations"][0]["channel_hint"] = "database"
        pipeline, _ = _pipeline(payload)
        result = pipeline.generate(_request())
        self.assertFalse(result.validation.passed)
        self.assertIn(
            "OPERATION_CHANNEL_NOT_ALLOWED",
            {item.rule_id for item in result.validation.findings},
        )

        pipeline, _ = _pipeline(_valid_candidate())
        result = pipeline.generate(_request(allowed_channels=["ui", "database"]))
        self.assertTrue(result.validation.passed, result.validation.findings)

        pipeline, _ = _pipeline(_valid_candidate())
        result = pipeline.generate(
            _request(
                allowed_channels=["ui", "database"],
                required_channels=["database"],
            )
        )
        self.assertFalse(result.validation.passed)
        self.assertIn(
            "REQUIRED_CHANNEL_UNCOVERED",
            {item.rule_id for item in result.validation.findings},
        )

        with self.assertRaisesRegex(ValidationError, "子集"):
            DesignSelections(
                techniques=["positive"],
                allowed_channels=["ui"],
                required_channels=["database"],
            )

    def test_cleanup_and_open_question_block_approval(self):
        payload = _valid_candidate()
        payload["scenarios"][0]["state_impact"].pop("cleanup_goal")
        pipeline, _ = _pipeline(payload)
        result = pipeline.generate(_request())
        self.assertFalse(result.validation.passed)
        self.assertIn(
            "CLEANUP_GOAL_REQUIRED",
            {item.rule_id for item in result.validation.findings},
        )

        payload = _valid_candidate()
        payload["open_questions"] = [{"question": "锁定阈值是否确定？"}]
        pipeline, _ = _pipeline(payload)
        result = pipeline.generate(_request())
        self.assertFalse(result.validation.passed)
        self.assertTrue(result.design.open_questions[0].blocking)

    def test_secret_size_and_safe_id_boundaries(self):
        payload = _valid_candidate()
        payload["scenarios"][0]["expected_results"][0]["expected"] = {
            "password": "leaked-value"
        }
        with self.assertRaisesRegex(ValidationError, "expected 不能包含"):
            DesignCandidateModel.model_validate(payload)

        for secret in (
            "密码：abc123",
            "DB_PASSWORD=abc123",
            "AUTH_TOKEN=abc123",
            "client_secret=actual-value",
            "-----BEGIN PRIVATE KEY-----",
        ):
            self.assertTrue(contains_secret_literal(secret), secret)
        for policy in (
            "Password: between 8 and 20 characters",
            "密码：8-20位",
            "密码：不得少于8位并需包含大小写字母",
        ):
            self.assertFalse(contains_secret_literal(policy), policy)

        pipeline, gateway = _pipeline(_valid_candidate(), knowledge=[])
        with self.assertRaisesRegex(ValueError, "凭据实际值"):
            pipeline.generate(
                _request(
                    scopes=[],
                    requirements=[
                        RequirementInput(
                            requirement_id="REQ-LOGIN",
                            content="验证登录，DB_PASSWORD=actual-value",
                        )
                    ],
                )
            )
        self.assertEqual(gateway.calls, 0)

        with self.assertRaisesRegex(ValidationError, "request_id"):
            DesignRequest(
                request_id="../unsafe",
                requirements=[RequirementInput(content="验证登录")],
                target=TargetSelection(system_id="account-web", environment="staging"),
                selections=DesignSelections(
                    techniques=["positive"], allowed_channels=["ui"]
                ),
            )

        oversized = [
            RequirementInput(requirement_id="REQ-A", content="a" * 150_000),
            RequirementInput(requirement_id="REQ-B", content="b" * 150_000),
        ]
        pipeline, gateway = _pipeline(_valid_candidate(), knowledge=[])
        with self.assertRaisesRegex(ValueError, "总量不能超过"):
            pipeline.generate(_request(scopes=[], requirements=oversized))
        self.assertEqual(gateway.calls, 0)

    def test_approved_knowledge_hash_must_match_exact_content(self):
        with self.assertRaisesRegex(ValidationError, "content_hash"):
            ApprovedKnowledge(
                scope_id="ui:login-approved",
                knowledge_id="UI-APPROVED",
                version=1,
                approval_id="KNOWLEDGE-REVIEW-1",
                approved_at="2026-07-18T00:00:00Z",
                content="登录入口为 /login",
                content_hash="sha256:" + "0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
