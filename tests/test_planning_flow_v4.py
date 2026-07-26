from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from apps.test_platform.intent.contracts import ReviewDecision
from apps.test_platform.planning import (
    ApprovedTestPlanBundle,
    ProcedureCapabilityProfile,
    ProcedureObservable,
    ProcedureOperation,
    CleanupAction,
    CleanupDataBindingSelection,
    CleanupSelection,
    DataBinding,
    DataBindingSelection,
    DataGuaranteeResolutionCandidate,
    DatabaseObservable,
    DatabaseOperation,
    DatabaseQueryDraftCandidate,
    DefaultPlanPromptBuilder,
    ExecutorKind,
    ExpectedResultSelection,
    HttpObservable,
    HttpOperation,
    OperationSelection,
    PlanCandidate,
    PlanDraftGenerator,
    PlanFlowCandidate,
    PlanReviewDecision,
    PlanStageCandidate,
    PlanningCatalogSnapshot,
    SetupStageResolutionCandidate,
    TestPlanCompiler,
    TestPlanDraft,
)
from apps.test_platform.planning.contracts import (
    BoundData,
    PlanStatus,
    PlanValidationReport,
    compute_plan_review_content_hash,
    compute_plan_validation_content_hash,
    format_procedure_input_data,
)
from tests.test_design_layer_v4 import (
    _approved_knowledge,
    _hash,
    _pipeline,
    _request,
    _valid_candidate,
)


def _approved_bundle(
    *,
    second_data_requirement: bool = False,
    knowledge_content: str | None = None,
):
    payload = _valid_candidate()
    payload["scenarios"][0]["expected_results"] = [
        {
            "text": "页面显示账号已锁定",
            "after_operation_index": 1,
            "channel_hint": "ui",
        },
        {
            "text": "账号锁定状态为 true",
            "after_operation_index": 1,
            "channel_hint": "database",
            "operator": "equals",
            "expected": True,
        },
    ]
    if second_data_requirement:
        payload["scenarios"][0]["data_requirements"].append(
            {"text": "本次运行的审计标签", "constraints": []}
        )
    knowledge = None
    if knowledge_content is not None:
        knowledge = [
            _approved_knowledge().model_copy(
                update={
                    "content": knowledge_content,
                    "content_hash": _hash(knowledge_content),
                }
            )
        ]
    pipeline, _ = _pipeline(payload, knowledge=knowledge)
    result = pipeline.generate(
        _request(allowed_channels=["ui", "database"])
    )
    approved, review = pipeline.review(
        result,
        decision=ReviewDecision.APPROVED,
        comments="逻辑设计已核对",
    )
    return pipeline.build_approved_bundle(result, approved, review)


def _approved_two_operation_ui_bundle():
    payload = _valid_candidate()
    payload["scenarios"][0]["operations"] = [
        {"text": "第一次提交错误密码", "channel_hint": "ui"},
        {"text": "第二次提交错误密码", "channel_hint": "ui"},
    ]
    payload["scenarios"][0]["expected_results"] = [
        {
            "text": "第一次提交后仍可继续登录",
            "after_operation_index": 1,
            "channel_hint": "ui",
        },
        {
            "text": "第二次提交后显示错误提示",
            "after_operation_index": 2,
            "channel_hint": "ui",
        },
    ]
    pipeline, _ = _pipeline(payload)
    result = pipeline.generate(_request(allowed_channels=["ui"]))
    approved, review = pipeline.review(
        result,
        decision=ReviewDecision.APPROVED,
        comments="双动作时序已核对",
    )
    return pipeline.build_approved_bundle(result, approved, review)


def _approved_two_operation_cross_channel_bundle():
    payload = _valid_candidate()
    payload["scenarios"][0]["operations"] = [
        {"text": "第 4 次提交错误密码", "channel_hint": "ui"},
        {"text": "第 5 次提交错误密码", "channel_hint": "ui"},
    ]
    payload["scenarios"][0]["expected_results"] = [
        {
            "text": "第 4 次后账号未锁定",
            "after_operation_index": 1,
            "channel_hint": "database",
            "operator": "equals",
            "expected": False,
        },
        {
            "text": "第 5 次后账号已锁定",
            "after_operation_index": 2,
            "channel_hint": "database",
            "operator": "equals",
            "expected": True,
        },
    ]
    pipeline, _ = _pipeline(payload)
    result = pipeline.generate(
        _request(allowed_channels=["ui", "database"])
    )
    approved, review = pipeline.review(
        result,
        decision=ReviewDecision.APPROVED,
        comments="跨渠道双动作时序已核对",
    )
    return pipeline.build_approved_bundle(result, approved, review)


def _approved_api_bundle():
    payload = _valid_candidate()
    scenario = payload["scenarios"][0]
    scenario["required_states"] = []
    scenario["operations"] = [
        {"text": "查询测试账号", "channel_hint": "api"}
    ]
    scenario["expected_results"] = [
        {
            "text": "接口返回 HTTP 200",
            "after_operation_index": 1,
            "channel_hint": "api",
            "operator": "equals",
            "expected": 200,
        }
    ]
    scenario["state_impact"] = {
        "impact": "read_only",
        "rationale": {"text": "只读取账号"},
        "cleanup_goal": None,
    }
    pipeline, _ = _pipeline(payload)
    result = pipeline.generate(_request(allowed_channels=["api"]))
    approved, review = pipeline.review(
        result,
        decision=ReviewDecision.APPROVED,
        comments="API 设计已核对",
    )
    return pipeline.build_approved_bundle(result, approved, review)


def _catalog() -> PlanningCatalogSnapshot:
    return PlanningCatalogSnapshot.build(
        catalog_id="catalog.account.staging.v4",
        system_id="account-web",
        environment="staging",
        available_executors=["procedure_playwright", "database"],
        procedure_profiles=[
            ProcedureCapabilityProfile(
                profile_ref="procedure.login",
                site="account.example.test",
                library_id="site.account.example.test",
                library_hash="sha256:" + "a" * 64,
                description="Reviewed login capability",
                operations=[
                    ProcedureOperation(
                        operation_ref="procedure.login.submit",
                        page_ref="page.login",
                        action="Submit the login form",
                        state_effect="changes_state",
                        procedure_id="account.login.submit",
                        procedure_version=1,
                        procedure_fingerprint="sha256:" + "1" * 64,
                        procedure_parameters=[
                            {
                                "name": "account",
                                "source": "input_data",
                                "source_key": "login_account_id",
                                "required": True,
                                "secret": False,
                            }
                        ],
                        allowed_binding_refs=["binding.procedure.account"],
                    ),
                    ProcedureOperation(
                        operation_ref="procedure.account.ensure-unlocked",
                        page_ref="page.login",
                        action="Ensure the account is unlocked",
                        state_effect="changes_state",
                        procedure_id="account.ensure-unlocked",
                        procedure_version=1,
                        procedure_fingerprint="sha256:" + "2" * 64,
                    ),
                    ProcedureOperation(
                        operation_ref="procedure.login.observe-message",
                        page_ref="page.login",
                        action="Observe the login message",
                        state_effect="read_only",
                        procedure_id="account.login.observe-message",
                        procedure_version=1,
                        procedure_fingerprint="sha256:" + "3" * 64,
                    ),
                ],
                observables=[
                    ProcedureObservable(
                        observable_ref="observable.ui.account-locked",
                        page_ref="page.login",
                        description="Account locked message",
                    )
                ],
            )
        ],
        database_operations=[
            DatabaseOperation(
                operation_ref="db.account.lock-state",
                description="Read account lock state",
                connection_profile_ref="runtime.account.db",
                allowed_binding_refs=["binding.db.account"],
                observables=[
                    DatabaseObservable(
                        observable_ref="observable.db.account-locked",
                        description="Locked column",
                        kind="column",
                        column="locked",
                    )
                ],
            )
        ],
        data_bindings=[
            DataBinding(
                binding_ref="binding.procedure.account",
                description="Account form input",
                executor_kind="procedure_playwright",
                operation_ref="procedure.login.submit",
                input_refs={"input.account": "account"},
            ),
            DataBinding(
                binding_ref="binding.db.account",
                description="Account query parameter",
                executor_kind="database",
                operation_ref="db.account.lock-state",
                input_refs={"param.account": "account"},
            ),
            DataBinding(
                binding_ref="binding.cleanup.account",
                description="Account cleanup input",
                executor_kind="procedure_playwright",
                operation_ref="cleanup.account.unlock",
                input_refs={"account_id": "account"},
            ),
        ],
        cleanup_actions=[
            CleanupAction(
                action_ref="cleanup.account.unlock",
                description="Restore account lock state",
                handler_kind="procedure_playwright",
                policy="restore_state",
                always_run=True,
                evidence_required=True,
                required_data_slots=["account_id"],
            )
        ],
    )


def _ids(bundle):
    scenario = bundle.design.scenarios[0]
    return {
        "scenario": scenario,
        "scenario_id": scenario.scenario_id,
        "operation_id": scenario.operations[0].operation_id,
        "ui_expected_id": scenario.expected_results[0].expected_result_id,
        "db_expected_id": scenario.expected_results[1].expected_result_id,
        "data_id": scenario.data_requirements[0].data_id,
        "required_state_id": scenario.required_states[0].required_state_id,
        "cleanup_goal_id": scenario.state_impact.cleanup_goal.cleanup_goal_id,
    }


def _candidate(bundle, *, stage_order: str = "ui-db", resolve_state: bool = True):
    values = _ids(bundle)
    ui = PlanStageCandidate(
        executor_kind=ExecutorKind.PROCEDURE_PLAYWRIGHT,
        operations=[
            OperationSelection(
                operation_id=values["operation_id"],
                catalog_ref="procedure.login.submit",
            )
        ],
        expected_results=[
            ExpectedResultSelection(
                expected_result_id=values["ui_expected_id"],
                catalog_ref="procedure.login.submit",
                observable_ref="observable.ui.account-locked",
            )
        ],
        data_bindings=[
            DataBindingSelection(
                data_id=values["data_id"],
                consumer_id=values["operation_id"],
                binding_ref="binding.procedure.account",
            )
        ],
    )
    database = PlanStageCandidate(
        executor_kind=ExecutorKind.DATABASE,
        expected_results=[
            ExpectedResultSelection(
                expected_result_id=values["db_expected_id"],
                catalog_ref="db.account.lock-state",
                observable_ref="observable.db.account-locked",
            )
        ],
        data_bindings=[
            DataBindingSelection(
                data_id=values["data_id"],
                consumer_id=values["db_expected_id"],
                binding_ref="binding.db.account",
            )
        ],
    )
    return PlanCandidate(
        flows=[
            PlanFlowCandidate(
                scenario_id=values["scenario_id"],
                stages=[ui, database] if stage_order == "ui-db" else [database, ui],
                required_state_resolutions=(
                    [
                        DataGuaranteeResolutionCandidate(
                            required_state_id=values["required_state_id"],
                            data_id=values["data_id"],
                        )
                    ]
                    if resolve_state
                    else []
                ),
                cleanup=CleanupSelection(
                    cleanup_goal_id=values["cleanup_goal_id"],
                    action_ref="cleanup.account.unlock",
                    data_bindings=[
                        CleanupDataBindingSelection(
                            slot="account_id",
                            data_id=values["data_id"],
                            binding_ref="binding.cleanup.account",
                        )
                    ],
                ),
            )
        ]
    )


class PlanningFlowV4Tests(unittest.TestCase):
    def test_optional_plan_schema_additions_do_not_change_legacy_hash(self):
        bundle = _approved_bundle()
        plan = TestPlanCompiler().build_draft(bundle, _candidate(bundle), _catalog())
        payload = plan.model_dump(mode="json")
        for flow in payload["flows"]:
            for stage in flow["stages"]:
                execution = stage["execution"]
                if execution.get("kind") == "procedure_playwright":
                    execution.pop("navigation_profile", None)
                    execution.pop("navigation_snapshot_hash", None)
                if execution.get("kind") == "database":
                    for operation in execution.get("operations", []):
                        operation.pop("sql", None)
                        operation.pop("parameters_refs", None)
                        operation.pop("sql_origin", None)
                        operation.pop("knowledge_scope_id", None)
        hash_payload = dict(payload)
        hash_payload.pop("status", None)
        hash_payload.pop("blocked_reasons", None)
        legacy_hash = "sha256:" + hashlib.sha256(
            json.dumps(
                hash_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        reparsed = TestPlanDraft.model_validate(payload)

        self.assertEqual(reparsed.content_hash(), legacy_hash)

    def test_compiler_accepts_reviewable_ai_sql_within_resource_schema(self):
        bundle = _approved_bundle()
        values = _ids(bundle)
        catalog_payload = _catalog().model_dump(mode="json")
        catalog_payload["database_schema"] = {
            "connection_profile_ref": "runtime.account.db",
            "dialect": "sqlite",
            "tables": [
                {
                    "name": "accounts",
                    "description": "测试账号状态",
                    "columns": [
                        {
                            "name": "locked",
                            "data_type": "boolean",
                            "description": "锁定状态",
                        }
                    ],
                }
            ],
            "allowed_parameter_refs": [],
        }
        catalog = PlanningCatalogSnapshot.build(**catalog_payload)
        candidate = _candidate(bundle)
        candidate.flows[0].stages[1] = PlanStageCandidate(
            executor_kind=ExecutorKind.DATABASE,
            database_queries=[
                DatabaseQueryDraftCandidate(
                    expected_result_id=values["db_expected_id"],
                    sql="SELECT locked FROM accounts LIMIT 1",
                    check_kind="column",
                    check_column="locked",
                    operator="equals",
                    expected=True,
                )
            ],
        )

        plan = TestPlanCompiler().build_draft(bundle, candidate, catalog)
        operation = plan.flows[0].stages[1].execution.operations[0]

        self.assertEqual(operation.sql_origin, "ai_generated")
        self.assertEqual(operation.sql, "SELECT locked FROM accounts LIMIT 1")
        self.assertEqual(operation.assertions[0].statement, "账号锁定状态为 true")

        candidate.flows[0].stages[1].database_queries[0].sql = (
            "SELECT locked FROM hidden_accounts"
        )
        with self.assertRaisesRegex(ValueError, "未登记数据表"):
            TestPlanCompiler().build_draft(bundle, candidate, catalog)
        candidate.flows[0].stages[1].database_queries[0].sql = (
            "SELECT secret_value FROM accounts"
        )
        with self.assertRaisesRegex(ValueError, "未登记字段"):
            TestPlanCompiler().build_draft(bundle, candidate, catalog)

    def test_compiler_reuses_knowledge_sql_but_resource_still_controls_access(self):
        sql = "SELECT locked FROM accounts LIMIT 1"
        bundle = _approved_bundle(
            knowledge_content=(
                "用途：确认测试账号是否锁定。\n"
                "历史 SQL：\n"
                f"{sql}\n"
                "仅供复用，仍需当前数据库访问策略批准。"
            )
        )
        values = _ids(bundle)
        catalog_payload = _catalog().model_dump(mode="json")
        catalog_payload["database_operations"] = []
        catalog_payload["data_bindings"] = [
            item
            for item in catalog_payload["data_bindings"]
            if item["executor_kind"] != "database"
        ]
        catalog_payload["database_schema"] = {
            "connection_profile_ref": "runtime.account.db",
            "dialect": "sqlite",
            "tables": [
                {
                    "name": "accounts",
                    "description": "测试账号状态",
                    "columns": [
                        {
                            "name": "locked",
                            "data_type": "boolean",
                            "description": "锁定状态",
                        }
                    ],
                }
            ],
            "allowed_parameter_refs": [],
        }
        catalog = PlanningCatalogSnapshot.build(**catalog_payload)
        candidate = _candidate(bundle)
        candidate.flows[0].stages[1] = PlanStageCandidate(
            executor_kind=ExecutorKind.DATABASE,
            database_queries=[
                DatabaseQueryDraftCandidate(
                    expected_result_id=values["db_expected_id"],
                    sql=sql,
                    check_kind="column",
                    check_column="locked",
                    operator="equals",
                    expected=True,
                    knowledge_scope_id="ui:login-approved",
                )
            ],
        )

        plan = TestPlanCompiler().build_draft(bundle, candidate, catalog)
        operation = plan.flows[0].stages[1].execution.operations[0]

        self.assertEqual(operation.sql_origin, "knowledge_reused")
        self.assertEqual(operation.knowledge_scope_id, "ui:login-approved")

        candidate.flows[0].stages[1].database_queries[0].sql = (
            "SELECT secret_value FROM accounts"
        )
        with self.assertRaisesRegex(ValueError, "未登记字段"):
            TestPlanCompiler().build_draft(bundle, candidate, catalog)

    def test_plan_candidate_normalizes_known_model_nesting_only(self):
        candidate = PlanCandidate.model_validate(
            {
                "flows": [
                    {
                        "scenario_id": "SCN-1",
                        "open_questions": [],
                        "stages": [
                            {
                                "executor_kind": "tcp_port",
                                "required_state_resolutions": [
                                    {
                                        "resolution_kind": "data_guarantee",
                                        "required_state_id": "STATE-1",
                                        "data_id": "DATA-1",
                                    }
                                ],
                                "operations": [
                                    {
                                        "operation_id": "OP-1",
                                        "catalog_ref": "port.probe-1",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(candidate.open_questions, [])
        self.assertEqual(
            candidate.flows[0].required_state_resolutions[0].required_state_id,
            "STATE-1",
        )
        self.assertNotIn(
            "required_state_resolutions",
            candidate.flows[0].stages[0].model_dump(),
        )

    def test_plan_candidate_normalizes_single_blocking_question(self):
        candidate = PlanCandidate.model_validate(
            {"question": "缺少可确认的执行前置条件"}
        )
        self.assertEqual(candidate.open_questions[0].question, "缺少可确认的执行前置条件")

    def test_procedure_catalog_compiles_exact_procedure_module(self):
        bundle = _approved_bundle()
        catalog_payload = _catalog().model_dump(mode="json", exclude={"content_hash"})
        catalog = PlanningCatalogSnapshot.build(**catalog_payload)
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(bundle, _candidate(bundle), catalog)
        execution = plan.flows[0].stages[0].execution

        self.assertIn(
            "[procedure_id=account.login.submit;version=1]",
            execution.rows[0].action,
        )
        self.assertEqual(
            execution.procedure_refs,
            ["account.login.submit@v1"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            compiled = compiler.compile(bundle, plan, catalog, tmp)
            procedure_artifact = next(
                item
                for item in compiled.artifacts
                if item.executor_kind == ExecutorKind.PROCEDURE_PLAYWRIGHT
            )
            manifest_path = (
                Path(tmp)
                / "generated-files"
                / "ui"
                / plan.plan_id
                / f"v{plan.version}"
                / procedure_artifact.flow_id
                / procedure_artifact.stage_id
                / procedure_artifact.manifest_path_ref
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(
            manifest["procedure_refs"],
            ["account.login.submit@v1"],
        )

    def test_procedure_input_data_rejects_duplicate_slots(self):
        bindings = [
            BoundData(
                data_id="DATA-1",
                consumer_id="OP-1",
                binding_ref="binding.first",
                input_refs={"input.account": "runtime.first_account"},
            ),
            BoundData(
                data_id="DATA-2",
                consumer_id="OP-1",
                binding_ref="binding.second",
                input_refs={"input.account": "runtime.second_account"},
            ),
        ]
        with self.assertRaisesRegex(ValueError, "重复绑定"):
            format_procedure_input_data(bindings)

        bindings[1] = bindings[1].model_copy(
            update={"input_refs": {"input.ACCOUNT": "runtime.second_account"}}
        )
        with self.assertRaisesRegex(ValueError, "重复绑定"):
            format_procedure_input_data(bindings)

    def test_http_path_binding_allows_placeholder_and_variable_with_same_name(self):
        bundle = _approved_api_bundle()
        scenario = bundle.design.scenarios[0]
        catalog = PlanningCatalogSnapshot.build(
            catalog_id="catalog.account.api.v4",
            system_id="account-web",
            environment="staging",
            available_executors=["http_api"],
            http_operations=[
                HttpOperation(
                    operation_ref="http.account.get",
                    description="Read account",
                    base_url_ref="runtime.account.api",
                    method="GET",
                    path="/accounts/{account}",
                    state_effect="read_only",
                    allowed_binding_refs=["binding.http.account"],
                    observables=[
                        HttpObservable(
                            observable_ref="observable.http.status",
                            description="HTTP status",
                            kind="status",
                        )
                    ],
                )
            ],
            data_bindings=[
                DataBinding(
                    binding_ref="binding.http.account",
                    description="Account path parameter",
                    executor_kind="http_api",
                    operation_ref="http.account.get",
                    input_refs={"path.account": "account"},
                )
            ],
        )
        candidate = PlanCandidate(
            flows=[
                PlanFlowCandidate(
                    scenario_id=scenario.scenario_id,
                    stages=[
                        PlanStageCandidate(
                            executor_kind="http_api",
                            operations=[
                                OperationSelection(
                                    operation_id=scenario.operations[0].operation_id,
                                    catalog_ref="http.account.get",
                                )
                            ],
                            expected_results=[
                                ExpectedResultSelection(
                                    expected_result_id=scenario.expected_results[0].expected_result_id,
                                    catalog_ref="http.account.get",
                                    observable_ref="observable.http.status",
                                )
                            ],
                            data_bindings=[
                                DataBindingSelection(
                                    data_id=scenario.data_requirements[0].data_id,
                                    consumer_id=scenario.operations[0].operation_id,
                                    binding_ref="binding.http.account",
                                )
                            ],
                        )
                    ],
                )
            ]
        )
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(bundle, candidate, catalog)
        with tempfile.TemporaryDirectory() as output:
            result = compiler.compile(bundle, plan, catalog, output)
            self.assertTrue(result.validation.passed, result.validation.findings)
            payload_path = (
                Path(output)
                / "generated-files"
                / "api"
                / plan.plan_id
                / "v1"
                / "FLOW-0001"
                / "STAGE-0001"
                / "execution.json"
            )
            execution = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(execution["requests"][0]["path"], "/accounts/{account}")
            source_path = payload_path.with_name("test_api_generated.py")
            source = source_path.read_text(encoding="utf-8")
            ast.parse(source)
            self.assertIn("httpx.request", source)
            self.assertIn("_assert_response", source)
            self.assertIn("/accounts/{account}", source)
            refs = {
                item.kind: item.path_ref for item in result.artifacts[0].artifact_refs
            }
            self.assertEqual(refs["pytest_source"], "test_api_generated.py")

    def test_planner_prompt_uses_approved_design_without_replaying_raw_evidence(self):
        bundle = _approved_bundle()
        messages = DefaultPlanPromptBuilder().build(bundle, _catalog())
        prompt = "\n".join(item.content for item in messages)
        self.assertIn("flow.stages", prompt)
        self.assertIn('"flows"', prompt)
        self.assertIn("catalog_ref 与 observable_ref 不可互换", prompt)
        self.assertIn('"catalog_ref": "procedure.login.submit"', prompt)
        self.assertIn('"observable_refs"', prompt)
        self.assertNotIn("登录页面入口为 /login", prompt)
        self.assertNotIn("逻辑设计已核对", prompt)
        self.assertNotIn('"input_snapshot"', prompt)

        with_input = DefaultPlanPromptBuilder().build(
            bundle,
            _catalog(),
            execution_input={
                "schema_version": "test-runtime-input.v1",
                "variables": {"account_id": "ACCOUNT-1"},
                "performance_mode": "live",
            },
        )
        input_prompt = "\n".join(item.content for item in with_input)
        self.assertIn("生成执行计划前已冻结的本次输入", input_prompt)
        self.assertIn('"account_id": "ACCOUNT-1"', input_prompt)
        self.assertIn("严禁把输入值直接拼入 SQL", input_prompt)
        with self.assertRaisesRegex(ValueError, "凭据"):
            DefaultPlanPromptBuilder().build(
                bundle,
                _catalog(),
                execution_input={"variables": {"api_token": "actual-secret-value"}},
            )

        revised = DefaultPlanPromptBuilder().build(
            bundle,
            _catalog(),
            review_feedback="将数据库只读校验放到接口请求之后",
        )
        revised_prompt = "\n".join(item.content for item in revised)
        self.assertIn("执行计划审核人的修订意见", revised_prompt)
        self.assertIn("将数据库只读校验放到接口请求之后", revised_prompt)
        with self.assertRaisesRegex(ValueError, "凭据"):
            DefaultPlanPromptBuilder().build(
                bundle,
                _catalog(),
                review_feedback="api_key=actual-planning-secret",
            )

    def test_plan_generator_rejects_secret_in_complete_model_messages(self):
        class UnsafePromptBuilder:
            def build(self, bundle, catalog):
                return [{"role": "user", "content": "password=actual-planning-secret"}]

        class RecordingGateway:
            def __init__(self):
                self.called = False

            def generate(self, messages, output_schema):
                self.called = True
                return {"flows": [], "open_questions": []}

        gateway = RecordingGateway()
        generator = PlanDraftGenerator(UnsafePromptBuilder(), gateway)
        with self.assertRaisesRegex(ValueError, "模型 messages"):
            generator.generate(_approved_bundle(), _catalog())
        self.assertFalse(gateway.called)

    def test_plan_generator_forwards_execution_review_feedback(self):
        class FeedbackPromptBuilder:
            def __init__(self):
                self.feedback = None

            def build(self, bundle, catalog, review_feedback=None):
                self.feedback = review_feedback
                return [{"role": "user", "content": "map approved refs"}]

        class Gateway:
            def generate(self, messages, output_schema):
                return {
                    "flows": [],
                    "open_questions": [{"question": "blocked for test"}],
                }

        class Compiler:
            def build_draft(self, *args, **kwargs):
                return "compiled"

        prompt_builder = FeedbackPromptBuilder()
        result = PlanDraftGenerator(
            prompt_builder,
            Gateway(),
            compiler=Compiler(),
        ).generate(
            _approved_bundle(),
            _catalog(),
            review_feedback="将数据库校验放到接口请求之后",
        )

        self.assertEqual(result, "compiled")
        self.assertEqual(prompt_builder.feedback, "将数据库校验放到接口请求之后")

    def test_plan_generator_repairs_one_deterministic_validation_failure(self):
        class PromptBuilder:
            def build(self, bundle, catalog):
                return [{"role": "user", "content": "map approved refs"}]

        class RecordingGateway:
            def __init__(self):
                self.messages = []

            def generate(self, messages, output_schema):
                self.messages.append(messages)
                return {
                    "flows": [],
                    "open_questions": [{"question": "blocked for test"}],
                }

        class OneFailureCompiler:
            def __init__(self):
                self.calls = 0

            def build_draft(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("flow 引用了其他场景的 operation_id")
                return "compiled"

        gateway = RecordingGateway()
        compiler = OneFailureCompiler()
        generator = PlanDraftGenerator(PromptBuilder(), gateway, compiler=compiler)
        result = generator.generate(_approved_bundle(), _catalog())

        self.assertEqual(result, "compiled")
        self.assertEqual(compiler.calls, 2)
        self.assertEqual(len(gateway.messages), 2)
        self.assertIn("确定性校验", gateway.messages[1][-1].content)
        self.assertIn('"scenario_id"', gateway.messages[1][-1].content)

    def test_cross_channel_observation_must_finish_before_next_operation(self):
        bundle = _approved_two_operation_cross_channel_bundle()
        scenario = bundle.design.scenarios[0]
        operation_ids = [item.operation_id for item in scenario.operations]
        expected_ids = [item.expected_result_id for item in scenario.expected_results]
        data_id = scenario.data_requirements[0].data_id

        def ui_stage(operation_id):
            return PlanStageCandidate(
                executor_kind="procedure_playwright",
                operations=[
                    OperationSelection(
                        operation_id=operation_id,
                        catalog_ref="procedure.login.submit",
                    )
                ],
                data_bindings=[
                    DataBindingSelection(
                        data_id=data_id,
                        consumer_id=operation_id,
                        binding_ref="binding.procedure.account",
                    )
                ],
            )

        def db_stage(expected_id):
            return PlanStageCandidate(
                executor_kind="database",
                expected_results=[
                    ExpectedResultSelection(
                        expected_result_id=expected_id,
                        catalog_ref="db.account.lock-state",
                        observable_ref="observable.db.account-locked",
                    )
                ],
                data_bindings=[
                    DataBindingSelection(
                        data_id=data_id,
                        consumer_id=expected_id,
                        binding_ref="binding.db.account",
                    )
                ],
            )

        def flow(stages):
            return PlanCandidate(
                flows=[
                    PlanFlowCandidate(
                        scenario_id=scenario.scenario_id,
                        stages=stages,
                        required_state_resolutions=[
                            DataGuaranteeResolutionCandidate(
                                required_state_id=scenario.required_states[0].required_state_id,
                                data_id=data_id,
                            )
                        ],
                        cleanup=CleanupSelection(
                            cleanup_goal_id=scenario.state_impact.cleanup_goal.cleanup_goal_id,
                            action_ref="cleanup.account.unlock",
                            data_bindings=[
                                CleanupDataBindingSelection(
                                    slot="account_id",
                                    data_id=data_id,
                                    binding_ref="binding.cleanup.account",
                                )
                            ],
                        ),
                    )
                ]
            )

        invalid = flow(
            [
                PlanStageCandidate(
                    executor_kind="procedure_playwright",
                    operations=[
                        OperationSelection(
                            operation_id=item,
                            catalog_ref="procedure.login.submit",
                        )
                        for item in operation_ids
                    ],
                    data_bindings=[
                        DataBindingSelection(
                            data_id=data_id,
                            consumer_id=operation_ids[0],
                            binding_ref="binding.procedure.account",
                        )
                    ],
                ),
                PlanStageCandidate(
                    executor_kind="database",
                    expected_results=[
                        ExpectedResultSelection(
                            expected_result_id=item,
                            catalog_ref="db.account.lock-state",
                            observable_ref="observable.db.account-locked",
                        )
                        for item in expected_ids
                    ],
                    data_bindings=[
                        DataBindingSelection(
                            data_id=data_id,
                            consumer_id=item,
                            binding_ref="binding.db.account",
                        )
                        for item in expected_ids
                    ],
                ),
            ]
        )
        compiler = TestPlanCompiler()
        invalid_plan = compiler.build_draft(bundle, invalid, _catalog())
        with tempfile.TemporaryDirectory() as output:
            invalid_result = compiler.compile(
                bundle, invalid_plan, _catalog(), output
            )
        self.assertIn(
            "EXPECTED_AFTER_NEXT_OPERATION",
            {item.rule_id for item in invalid_result.validation.findings},
        )

        valid_plan = compiler.build_draft(
            bundle,
            flow(
                [
                    ui_stage(operation_ids[0]),
                    db_stage(expected_ids[0]),
                    ui_stage(operation_ids[1]),
                    db_stage(expected_ids[1]),
                ]
            ),
            _catalog(),
        )
        with tempfile.TemporaryDirectory() as output:
            valid_result = compiler.compile(bundle, valid_plan, _catalog(), output)
        self.assertTrue(valid_result.validation.passed, valid_result.validation.findings)

    def test_compiler_interleaves_observer_after_each_approved_operation(self):
        bundle = _approved_two_operation_ui_bundle()
        scenario = bundle.design.scenarios[0]
        operation_ids = [item.operation_id for item in scenario.operations]
        expected_ids = [item.expected_result_id for item in scenario.expected_results]
        data_id = scenario.data_requirements[0].data_id
        candidate = PlanCandidate(
            flows=[
                PlanFlowCandidate(
                    scenario_id=scenario.scenario_id,
                    stages=[
                        PlanStageCandidate(
                            executor_kind="procedure_playwright",
                            operations=[
                                OperationSelection(
                                    operation_id=operation_id,
                                    catalog_ref="procedure.login.submit",
                                )
                                for operation_id in reversed(operation_ids)
                            ],
                            expected_results=[
                                ExpectedResultSelection(
                                    expected_result_id=expected_id,
                                    catalog_ref="procedure.login.observe-message",
                                    observable_ref="observable.ui.account-locked",
                                )
                                for expected_id in reversed(expected_ids)
                            ],
                            data_bindings=[
                                DataBindingSelection(
                                    data_id=data_id,
                                    consumer_id=operation_ids[0],
                                    binding_ref="binding.procedure.account",
                                )
                            ],
                        )
                    ],
                    required_state_resolutions=[
                        DataGuaranteeResolutionCandidate(
                            required_state_id=scenario.required_states[0].required_state_id,
                            data_id=data_id,
                        )
                    ],
                    cleanup=CleanupSelection(
                        cleanup_goal_id=scenario.state_impact.cleanup_goal.cleanup_goal_id,
                        action_ref="cleanup.account.unlock",
                        data_bindings=[
                            CleanupDataBindingSelection(
                                slot="account_id",
                                data_id=data_id,
                                binding_ref="binding.cleanup.account",
                            )
                        ],
                    ),
                )
            ]
        )
        plan = TestPlanCompiler().build_draft(bundle, candidate, _catalog())
        rows = plan.flows[0].stages[0].execution.rows
        self.assertEqual(
            [(item.source.source_kind, item.source.source_id) for item in rows],
            [
                ("operation", operation_ids[0]),
                ("expected_result", expected_ids[0]),
                ("operation", operation_ids[1]),
                ("expected_result", expected_ids[1]),
            ],
        )
        with tempfile.TemporaryDirectory() as output:
            result = TestPlanCompiler().compile(bundle, plan, _catalog(), output)
        self.assertTrue(result.validation.passed, result.validation.findings)

    def test_ui_action_and_database_observation_compile_as_one_ordered_flow(self):
        bundle = _approved_bundle()
        catalog = _catalog()
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(bundle, _candidate(bundle), catalog)

        self.assertEqual(plan.schema_version, "test-plan.v4")
        self.assertEqual(plan.plan_id, "plan-REQ-LOGIN-001")
        self.assertEqual(len(plan.flows), 1)
        flow = plan.flows[0]
        self.assertEqual(flow.flow_id, "FLOW-0001")
        self.assertEqual(
            [item.stage_id for item in flow.stages],
            ["STAGE-0001", "STAGE-0002"],
        )
        self.assertEqual([item.order for item in flow.stages], [1, 2])
        self.assertEqual(flow.stages[0].operation_ids, [_ids(bundle)["operation_id"]])
        self.assertEqual(flow.stages[1].operation_ids, [])
        self.assertEqual(
            flow.stages[1].execution.operations[0].source.source_kind,
            "expected_result",
        )
        self.assertEqual(
            flow.cleanup.data_bindings[0].variable_ref,
            "account",
        )

        with tempfile.TemporaryDirectory() as output:
            result = compiler.compile(bundle, plan, catalog, output)
            self.assertTrue(result.validation.passed, result.validation.findings)
            self.assertEqual(len(result.artifacts), 2)
            self.assertEqual(
                [item.artifact_id for item in result.artifacts],
                [
                    "ARTIFACT-FLOW-0001-STAGE-0001",
                    "ARTIFACT-FLOW-0001-STAGE-0002",
                ],
            )
            self.assertEqual(
                {(item.flow_id, item.stage_id) for item in result.artifacts},
                {(flow.flow_id, item.stage_id) for item in flow.stages},
            )
            db_manifest = (
                Path(output)
                / "generated-files"
                / "database"
                / plan.plan_id
                / "v1"
                / flow.flow_id
            )
            db_manifest = db_manifest / flow.stages[1].stage_id / "manifest.json"
            manifest = json.loads(db_manifest.read_text(encoding="utf-8"))
            self.assertNotIn("lifecycle", manifest)
            self.assertEqual(
                manifest["flow_cleanup"]["data_bindings"][0]["variable_ref"],
                "account",
            )

        approved, review = compiler.review(
            result,
            decision=PlanReviewDecision.APPROVED,
            comments="flow 和 stage 映射已核对",
        )
        handoff = compiler.build_approved_bundle(
            result, approved, review, catalog
        )
        self.assertEqual(handoff.schema_version, "approved-test-plan-bundle.v4")
        self.assertEqual(
            handoff.compiled_artifacts[0].schema_version,
            "executor-artifact-bundle.v4",
        )
        self.assertRegex(
            handoff.validation.validation_content_hash,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(review.review_content_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            review.validation_content_hash,
            handoff.validation.validation_content_hash,
        )
        stale_result = result.__class__(
            plan=result.plan.model_copy(
                update={"status": PlanStatus.CHANGES_REQUESTED}
            ),
            validation=result.validation,
            artifacts=result.artifacts,
        )
        with self.assertRaisesRegex(ValueError, "已审核|原地批准"):
            compiler.review(
                stale_result,
                decision=PlanReviewDecision.APPROVED,
                comments="未重新编译就尝试批准旧版本",
            )
        with self.assertRaisesRegex(ValueError, "已审核|已结束"):
            compiler.review(
                result,
                decision=PlanReviewDecision.APPROVED,
                comments="重放旧 plan result 尝试重复批准",
            )

        tampered = handoff.model_dump(mode="json")
        tampered["validation"]["findings"].append(
            {
                "rule_id": "TAMPERED",
                "message": "事后增加的提示",
                "field_path": "flows",
                "blocking": False,
            }
        )
        with self.assertRaisesRegex(ValidationError, "validation_content_hash"):
            ApprovedTestPlanBundle.model_validate(tampered)

        for field, value in (
            ("comments", "被事后改写的审核意见"),
            ("review_content_hash", "sha256:" + "0" * 64),
        ):
            tampered = handoff.model_dump(mode="json")
            tampered["review"][field] = value
            with self.assertRaisesRegex(ValidationError, "review_content_hash"):
                ApprovedTestPlanBundle.model_validate(tampered)

        for field in ("review_id", "reviewed_at"):
            tampered = handoff.model_dump(mode="json")
            tampered["review"][field] = "   "
            tampered["review"]["review_content_hash"] = (
                compute_plan_review_content_hash(tampered["review"])
            )
            with self.assertRaisesRegex(ValidationError, "at least 1 character"):
                ApprovedTestPlanBundle.model_validate(tampered)

        blocking_report = handoff.validation.model_dump(mode="json")
        blocking_report["findings"].append(
            {
                "rule_id": "FORGED_BLOCKING",
                "message": "阻塞项",
                "field_path": "flows",
                "blocking": True,
            }
        )
        blocking_report["validation_content_hash"] = (
            compute_plan_validation_content_hash(blocking_report)
        )
        with self.assertRaisesRegex(ValidationError, "passed"):
            PlanValidationReport.model_validate(blocking_report)

        tampered = handoff.model_dump(mode="json")
        tampered["review"]["validation_content_hash"] = "sha256:" + "1" * 64
        tampered["review"]["review_content_hash"] = compute_plan_review_content_hash(
            tampered["review"]
        )
        with self.assertRaisesRegex(ValidationError, "校验报告"):
            ApprovedTestPlanBundle.model_validate(tampered)

        tampered = handoff.model_dump(mode="json")
        tampered["plan"]["blocked_reasons"] = ["forged blocked reason"]
        with self.assertRaisesRegex(ValidationError, "blocked_reasons"):
            ApprovedTestPlanBundle.model_validate(tampered)

        tampered = handoff.model_dump(mode="json")
        tampered["plan"]["open_questions"] = [
            {
                "question_id": "PLAN-Q-9999",
                "question": "伪造问题",
                "blocking": True,
            }
        ]
        with self.assertRaisesRegex(ValidationError, "open_questions"):
            ApprovedTestPlanBundle.model_validate(tampered)

        with self.assertRaisesRegex(ValidationError, "at least 1 character"):
            compiler.review(
                result,
                decision=PlanReviewDecision.APPROVED,
                comments="   ",
            )
        with self.assertRaisesRegex(ValidationError, "凭据"):
            compiler.review(
                result,
                decision=PlanReviewDecision.APPROVED,
                comments="password=actual-secret",
            )

    def test_expected_stage_cannot_run_before_its_trigger_operation(self):
        bundle = _approved_bundle()
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(
            bundle,
            _candidate(bundle, stage_order="db-ui"),
            _catalog(),
        )
        with tempfile.TemporaryDirectory() as output:
            result = compiler.compile(bundle, plan, _catalog(), output)
        self.assertFalse(result.validation.passed)
        self.assertIn(
            "EXPECTED_BEFORE_OPERATION",
            {item.rule_id for item in result.validation.findings},
        )

    def test_missing_required_state_resolution_blocks_without_legacy_error(self):
        bundle = _approved_bundle()
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(
            bundle,
            _candidate(bundle, resolve_state=False),
            _catalog(),
        )
        with tempfile.TemporaryDirectory() as output:
            result = compiler.compile(bundle, plan, _catalog(), output)
        rules = {item.rule_id for item in result.validation.findings}
        self.assertIn("REQUIRED_STATE_UNRESOLVED", rules)
        self.assertNotIn("PRECONDITION_EXECUTOR_UNSUPPORTED", rules)

    def test_setup_stage_is_system_identified_and_cannot_mix_test_work(self):
        bundle = _approved_bundle()
        values = _ids(bundle)
        base = _candidate(bundle).flows[0]
        mixed_stage = base.stages[0]
        candidate = PlanCandidate(
            flows=[
                PlanFlowCandidate(
                    scenario_id=values["scenario_id"],
                    stages=[mixed_stage, base.stages[1]],
                    required_state_resolutions=[
                        SetupStageResolutionCandidate(
                            required_state_id=values["required_state_id"],
                            stage_index=1,
                            catalog_ref="procedure.account.ensure-unlocked",
                        )
                    ],
                    cleanup=base.cleanup,
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "setup stage"):
            TestPlanCompiler().build_draft(bundle, candidate, _catalog())

        schema = PlanCandidate.model_json_schema()
        self.assertNotIn("stage_id", json.dumps(schema, ensure_ascii=False))

    def test_setup_stage_resolves_required_state_before_test_stages(self):
        bundle = _approved_bundle()
        values = _ids(bundle)
        base = _candidate(bundle).flows[0]
        candidate = PlanCandidate(
            flows=[
                PlanFlowCandidate(
                    scenario_id=values["scenario_id"],
                    stages=[
                        PlanStageCandidate(executor_kind="procedure_playwright"),
                        *base.stages,
                    ],
                    required_state_resolutions=[
                        SetupStageResolutionCandidate(
                            required_state_id=values["required_state_id"],
                            stage_index=1,
                            catalog_ref="procedure.account.ensure-unlocked",
                        )
                    ],
                    cleanup=base.cleanup,
                )
            ]
        )
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(bundle, candidate, _catalog())
        flow = plan.flows[0]
        self.assertEqual(flow.stages[0].setup_required_state_ids, [values["required_state_id"]])
        self.assertEqual(
            flow.required_state_resolutions[0].stage_id,
            flow.stages[0].stage_id,
        )
        with tempfile.TemporaryDirectory() as output:
            result = compiler.compile(bundle, plan, _catalog(), output)
        self.assertTrue(result.validation.passed, result.validation.findings)

    def test_setup_stage_rejects_read_only_catalog_resource(self):
        bundle = _approved_bundle()
        values = _ids(bundle)
        base = _candidate(bundle)
        candidate = PlanCandidate(
            flows=[
                PlanFlowCandidate(
                    scenario_id=values["scenario_id"],
                    stages=[
                        PlanStageCandidate(executor_kind="database"),
                        *base.flows[0].stages,
                    ],
                    required_state_resolutions=[
                        SetupStageResolutionCandidate(
                            required_state_id=values["required_state_id"],
                            stage_index=1,
                            catalog_ref="db.account.lock-state",
                        )
                    ],
                    cleanup=base.flows[0].cleanup,
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "不能使用 read_only"):
            TestPlanCompiler().build_draft(bundle, candidate, _catalog())

    def test_candidate_rejects_duplicate_operation_ownership(self):
        bundle = _approved_bundle()
        values = _ids(bundle)
        operation = OperationSelection(
            operation_id=values["operation_id"],
            catalog_ref="procedure.login.submit",
        )
        with self.assertRaisesRegex(ValidationError, "只能由一个 stage"):
            PlanCandidate(
                flows=[
                    PlanFlowCandidate(
                        scenario_id=values["scenario_id"],
                        stages=[
                            PlanStageCandidate(
                                executor_kind="procedure_playwright",
                                operations=[operation],
                            ),
                            PlanStageCandidate(
                                executor_kind="procedure_playwright",
                                operations=[operation],
                            ),
                        ],
                    )
                ]
            )

    def test_channel_hint_mismatch_is_rejected_before_plan_creation(self):
        bundle = _approved_bundle()
        bad = _candidate(bundle)
        bad.flows[0].stages[0].executor_kind = ExecutorKind.DATABASE
        bad.flows[0].stages[0].operations[0].catalog_ref = "db.account.lock-state"
        with self.assertRaisesRegex(ValueError, "channel_hint"):
            TestPlanCompiler().build_draft(bundle, bad, _catalog())

    def test_unused_data_requirement_blocks_flow(self):
        bundle = _approved_bundle(second_data_requirement=True)
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(bundle, _candidate(bundle), _catalog())
        with tempfile.TemporaryDirectory() as output:
            result = compiler.compile(bundle, plan, _catalog(), output)
        self.assertIn(
            "FLOW_DATA_COVERAGE_MISMATCH",
            {item.rule_id for item in result.validation.findings},
        )

    def test_cleanup_bindings_must_cover_every_subject_data_id(self):
        bundle = _approved_bundle(second_data_requirement=True)
        payload = bundle.model_dump(mode="json")
        scenario = payload["design"]["scenarios"][0]
        second_data_id = scenario["data_requirements"][1]["data_id"]
        scenario["state_impact"]["cleanup_goal"]["subject_data_ids"].append(
            second_data_id
        )
        # Re-hashing an approved bundle is intentionally outside this test. Build a
        # second valid design through the first-layer pipeline instead.
        candidate_payload = _valid_candidate()
        candidate_payload["scenarios"][0]["expected_results"] = [
            {
                "text": "页面显示账号已锁定",
                "after_operation_index": 1,
                "channel_hint": "ui",
            },
            {
                "text": "账号锁定状态为 true",
                "after_operation_index": 1,
                "channel_hint": "database",
                "operator": "equals",
                "expected": True,
            },
        ]
        candidate_payload["scenarios"][0]["data_requirements"].append(
            {"text": "第二个待恢复账号", "constraints": []}
        )
        candidate_payload["scenarios"][0]["state_impact"]["cleanup_goal"][
            "subject_data_indexes"
        ] = [1, 2]
        pipeline, _ = _pipeline(candidate_payload)
        generated = pipeline.generate(
            _request(allowed_channels=["ui", "database"])
        )
        approved, review = pipeline.review(
            generated,
            decision=ReviewDecision.APPROVED,
            comments="双主体清理已核对",
        )
        two_subjects = pipeline.build_approved_bundle(generated, approved, review)
        with self.assertRaisesRegex(ValueError, "subject_data_ids"):
            TestPlanCompiler().build_draft(
                two_subjects,
                _candidate(two_subjects),
                _catalog(),
            )


if __name__ == "__main__":
    unittest.main()
