from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.test_platform.intent.builder import DefaultDesignBuilder
from apps.test_platform.intent.contracts import ReviewDecision, TestDesignRequest
from apps.test_platform.intent.prompt_builder import DefaultDesignPromptBuilder
from apps.test_platform.intent.service import TestDesignPipeline
from apps.test_platform.models import TestExecutionRun, TestResourceProfile
from apps.test_platform.planning.compiler import TestPlanCompiler
from apps.test_platform.planning.contracts import PlanReviewDecision
from apps.test_platform.planning.planner import DefaultPlanPromptBuilder, PlanDraftGenerator
from apps.test_platform.planning.resources import resolve_test_resources
from apps.test_platform.service_factory import get_runtime_context
from apps.test_platform.workflow import IntentToExecutionWorkflow


class _FixtureGateway:
    def __init__(self, payload):
        self.payload = payload

    def generate(self, messages, output_schema):
        if not messages:
            raise AssertionError("the model boundary must receive a prompt")
        return output_schema.model_validate(self.payload)


def _design_candidate() -> dict:
    return {
        "title": "数据库服务状态验证",
        "objective": {
            "text": "验证数据库中的服务状态为 ready",
            "derivation_note": "直接来自输入需求",
        },
        "in_scope": [{"text": "只读查询服务状态"}],
        "out_of_scope": [{"text": "不修改数据库数据"}],
        "scenarios": [
            {
                "title": "服务状态为 ready",
                "techniques": ["positive"],
                "requirement_ids": ["REQ-DB-READY"],
                "operations": [
                    {
                        "text": "读取已登记的服务状态查询",
                        "channel_hint": "database",
                    }
                ],
                "expected_results": [
                    {
                        "text": "服务状态字段为 ready",
                        "after_operation_index": 1,
                        "channel_hint": "database",
                        "operator": "equals",
                        "expected": "ready",
                    }
                ],
                "data_requirements": [],
                "state_impact": {
                    "impact": "read_only",
                    "rationale": {"text": "只执行已审核的 SELECT 查询"},
                },
            }
        ],
        "open_questions": [],
    }


def _plan_candidate() -> dict:
    return {
        "flows": [
            {
                "scenario_id": "design-REQ-DB-PROFILE-V4-SCN-0001",
                "stages": [
                    {
                        "executor_kind": "database",
                        "database_queries": [
                            {
                                "operation_id": "design-REQ-DB-PROFILE-V4-OP-0001",
                                "expected_result_id": "design-REQ-DB-PROFILE-V4-EXP-0001",
                                "sql": "SELECT status FROM service_status LIMIT 1",
                                "parameters_refs": {},
                                "check_kind": "column",
                                "check_column": "status",
                                "operator": "equals",
                                "expected": "ready",
                            }
                        ],
                    }
                ],
            }
        ],
        "open_questions": [],
    }


class TestResourceProfileDatabaseE2ETests(TestCase):
    def test_requirement_profile_to_two_recorded_database_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_dir = root / "runtime"
            runtime_dir.mkdir()
            database_path = runtime_dir / "service.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE service_status (status TEXT NOT NULL)")
            connection.execute("INSERT INTO service_status VALUES ('ready')")
            connection.commit()
            connection.close()

            profile = TestResourceProfile(
                name="Local database demo",
                database_query_file=SimpleUploadedFile(
                    "queries.json",
                    json.dumps(
                        {
                            "schema_version": "database-access-policy.v1",
                            "database_schema": {
                                "dialect": "sqlite",
                                "tables": [
                                    {
                                        "name": "service_status",
                                        "description": "服务状态表",
                                        "columns": [
                                            {
                                                "name": "status",
                                                "data_type": "text",
                                                "description": "服务状态",
                                            }
                                        ],
                                    }
                                ],
                                "allowed_parameter_refs": [],
                            },
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                ),
                database_connection_ref="db.local",
            )

            with override_settings(
                BASE_DIR=root,
                MEDIA_ROOT=root / "uploads",
                TEST_PLATFORM_ARTIFACT_ROOT=root / "artifacts",
                TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY="",
                TEST_PLATFORM_RUNTIME_CONTEXT_JSON="",
            ):
                profile.full_clean()
                profile.save()
                request = TestDesignRequest.model_validate(
                    {
                        "schema_version": "test-design-request.v4",
                        "request_id": "REQ-DB-PROFILE-V4",
                        "requirements": [
                            {
                                "requirement_id": "REQ-DB-READY",
                                "content": "只读验证数据库 service_status 表中的状态为 ready。",
                            }
                        ],
                        "target": {
                            "system_id": profile.system_id,
                            "environment": profile.environment,
                        },
                        "selections": {
                            "techniques": ["positive"],
                            "allowed_channels": ["database"],
                            "required_channels": ["database"],
                            "knowledge_scope_ids": [],
                        },
                    }
                )
                design_pipeline = TestDesignPipeline(
                    DefaultDesignBuilder(
                        DefaultDesignPromptBuilder(),
                        _FixtureGateway(_design_candidate()),
                    )
                )
                compiler = TestPlanCompiler()
                planner = PlanDraftGenerator(
                    DefaultPlanPromptBuilder(),
                    _FixtureGateway(_plan_candidate()),
                    compiler,
                )
                workflow = IntentToExecutionWorkflow(
                    design_pipeline,
                    planner,
                    plan_compiler=compiler,
                )

                generated = workflow.generate_design(request)
                design_review = workflow.review_design(
                    generated,
                    decision=ReviewDecision.APPROVED,
                    comments="需求与测试意图一致",
                )
                self.assertIsNotNone(design_review.approved_bundle)
                resources = resolve_test_resources(
                    profile,
                    design_review.approved_bundle,
                )
                catalog = resources.catalog
                artifact_root = root / "artifacts" / "database-profile-e2e"
                compiled = workflow.compile_plan(
                    design_review.approved_bundle,
                    catalog,
                    artifact_root,
                )
                plan_review = workflow.review_plan(
                    compiled,
                    catalog,
                    decision=PlanReviewDecision.APPROVED,
                    comments="AI 只读 SQL 和数据库访问边界已核对",
                )
                self.assertIsNotNone(plan_review.approved_bundle)
                context = get_runtime_context(
                    evidence_dir=artifact_root / "evidence",
                    runtime_config={
                        **resources.runtime_config,
                        "database_connections": {
                            "db.local": "runtime/service.sqlite3"
                        },
                    },
                )

                first = workflow.execute(
                    plan_review.approved_bundle,
                    artifact_root,
                    context,
                    run_id="RUN-DB-PROFILE-0001",
                )
                second = workflow.execute(
                    plan_review.approved_bundle,
                    artifact_root,
                    context,
                    run_id="RUN-DB-PROFILE-0002",
                )

            self.assertEqual(str(first.status.value), "passed")
            self.assertEqual(str(second.status.value), "passed")
            runs = TestExecutionRun.objects.filter(
                run_id__in=["RUN-DB-PROFILE-0001", "RUN-DB-PROFILE-0002"]
            )
            self.assertEqual(runs.count(), 2)
            for run in runs:
                self.assertEqual(run.status, TestExecutionRun.Status.PASSED)
                self.assertEqual(
                    run.report_status, TestExecutionRun.ReportStatus.AVAILABLE
                )
                self.assertEqual(
                    set(run.report_paths), {"root", "json", "html", "junit"}
                )
                report_root = root / "artifacts" / run.storage_root_ref
                self.assertTrue(
                    all(
                        (report_root / run.report_paths[kind]).is_file()
                        for kind in ("json", "html", "junit")
                    )
                )
