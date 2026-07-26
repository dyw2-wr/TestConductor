from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from apps.test_platform.intent.builder import DefaultDesignBuilder
from apps.test_platform.intent.contracts import (
    RequirementInput,
    ReviewDecision,
    TestDesignRequest,
    contains_secret_literal,
    contains_secret_value,
)
from apps.test_platform.intent.prompt_builder import DefaultDesignPromptBuilder
from apps.test_platform.intent.service import TestDesignPipeline
from apps.test_platform.planning.catalogs import PlanningCatalogSnapshot
from apps.test_platform.planning.compiler import TestPlanCompiler
from apps.test_platform.planning.contracts import PlanCandidate, PlanReviewDecision


FIXTURES = Path(__file__).parent / "fixtures_v4"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FixtureGateway:
    def __init__(self, payload):
        self.payload = payload

    def generate(self, messages, output_schema):
        return self.payload


def _approved_design(request_name: str, candidate_name: str):
    request = TestDesignRequest.model_validate(_load(request_name))
    candidate_payload = _load(candidate_name)
    pipeline = TestDesignPipeline(
        DefaultDesignBuilder(
            DefaultDesignPromptBuilder(),
            _FixtureGateway(candidate_payload),
        )
    )
    result = pipeline.generate(request)
    if not result.validation.passed:
        raise AssertionError(result.validation.findings)
    approved, review = pipeline.review(
        result,
        decision=ReviewDecision.APPROVED,
        comments="v4 fixture 逻辑设计已核对",
    )
    return pipeline.build_approved_bundle(result, approved, review)


class FixtureV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = PlanningCatalogSnapshot.build(
            **_load("planning_catalog_content.json")
        )

    def test_raw_requirement_formats_are_preserved_as_text(self):
        payload = _load("raw_requirement_formats.json")
        self.assertEqual(payload["fixture_version"], "raw-requirement-formats.v4")
        self.assertEqual(
            {item["name"] for item in payload["examples"]},
            {
                "plain_text_without_heading",
                "mixed_numbering",
                "markdown_table",
                "json_text",
            },
        )
        for example in payload["examples"]:
            raw = example["requirement"]["content"]
            requirement = RequirementInput.model_validate(example["requirement"])
            self.assertEqual(requirement.content, raw)
            self.assertFalse(contains_secret_literal(raw), example["name"])

    def test_no_fixture_contains_a_runtime_secret_value(self):
        for path in FIXTURES.glob("*.json"):
            self.assertFalse(
                contains_secret_value(_load(path.name)),
                path.name,
            )

    def test_login_lock_fixture_compiles_in_four_ordered_stages(self):
        bundle = _approved_design(
            "login_lock_design_request.json",
            "login_lock_design_candidate.json",
        )
        candidate = PlanCandidate.model_validate(
            _load("login_lock_plan_candidate.json")
        )
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(bundle, candidate, self.catalog)
        flow = plan.flows[0]

        self.assertEqual(
            [stage.executor_kind.value for stage in flow.stages],
            ["procedure_playwright", "database", "procedure_playwright", "database"],
        )
        self.assertEqual([stage.order for stage in flow.stages], [1, 2, 3, 4])
        self.assertEqual(
            flow.required_state_resolutions[0].resolution_kind,
            "data_guarantee",
        )
        self.assertIsNotNone(flow.cleanup)
        self.assertEqual(len(flow.cleanup.data_bindings), 1)
        self.assertEqual(flow.cleanup.data_bindings[0].slot, "account_id")

        with tempfile.TemporaryDirectory() as output:
            compiled = compiler.compile(bundle, plan, self.catalog, output)
            workbook = next(Path(output).glob("**/case.xlsx"))
            frame = pd.read_excel(workbook, sheet_name="Case")
            self.assertIn("Action", frame.columns)
            self.assertIn("Check", frame.columns)
            self.assertIn("=", str(frame.iloc[0]["Input Data"]))
        self.assertTrue(compiled.validation.passed, compiled.validation.findings)
        self.assertEqual(len(compiled.artifacts), 4)
        guarantee_finding = next(
            item
            for item in compiled.validation.findings
            if item.rule_id == "DATA_GUARANTEE_REQUIRES_REVIEW"
        )
        self.assertFalse(guarantee_finding.blocking)
        approved, review = compiler.review(
            compiled,
            decision=PlanReviewDecision.APPROVED,
            comments="登录锁定 flow、stage 和编译产物已核对",
        )
        handoff = compiler.build_approved_bundle(
            compiled, approved, review, self.catalog
        )
        self.assertEqual(handoff.schema_version, "approved-test-plan-bundle.v4")

    def test_api_database_setup_and_cleanup_fixture_compiles(self):
        bundle = _approved_design(
            "account_api_design_request.json",
            "account_api_design_candidate.json",
        )
        candidate = PlanCandidate.model_validate(
            _load("account_api_plan_candidate.json")
        )
        compiler = TestPlanCompiler()
        plan = compiler.build_draft(bundle, candidate, self.catalog)
        flow = plan.flows[0]

        self.assertEqual(
            [stage.executor_kind.value for stage in flow.stages],
            ["http_api", "http_api", "database"],
        )
        resolution = flow.required_state_resolutions[0]
        self.assertEqual(resolution.resolution_kind, "setup_stage")
        self.assertEqual(resolution.stage_id, flow.stages[0].stage_id)
        self.assertEqual(
            flow.stages[0].setup_required_state_ids,
            [resolution.required_state_id],
        )
        self.assertEqual(flow.cleanup.action_ref, "cleanup.account.delete")
        self.assertTrue(flow.cleanup.always_run)
        self.assertEqual(
            [item.slot for item in flow.cleanup.data_bindings],
            ["account_id"],
        )

        with tempfile.TemporaryDirectory() as output:
            compiled = compiler.compile(bundle, plan, self.catalog, output)
        self.assertTrue(compiled.validation.passed, compiled.validation.findings)
        self.assertEqual(len(compiled.artifacts), 3)
        approved, review = compiler.review(
            compiled,
            decision=PlanReviewDecision.APPROVED,
            comments="接口、数据库、setup 和 cleanup 产物已核对",
        )
        handoff = compiler.build_approved_bundle(
            compiled, approved, review, self.catalog
        )
        self.assertEqual(handoff.schema_version, "approved-test-plan-bundle.v4")

    def test_performance_load_is_generated_by_plan_candidate_within_asset_limits(self):
        bundle = _approved_design(
            "multichannel_initial_design_request.json",
            "multichannel_initial_design_candidate.json",
        )
        payload = _load("multichannel_initial_plan_candidate.json")
        candidate = PlanCandidate.model_validate(payload)
        catalog = PlanningCatalogSnapshot.build(
            **_load("multichannel_initial_catalog_content.json")
        )
        plan = TestPlanCompiler().build_draft(bundle, candidate, catalog)
        execution = next(
            stage.execution
            for flow in plan.flows
            for stage in flow.stages
            if stage.executor_kind.value == "performance"
        )
        self.assertEqual(execution.stages[0].duration_seconds, 0.2)
        self.assertEqual(execution.stages[0].virtual_users, 2)

        performance_stage = next(
            stage
            for flow in payload["flows"]
            for stage in flow["stages"]
            if stage["executor_kind"] == "performance"
        )
        performance_stage["performance_stages"][0]["virtual_users"] = 11
        with self.assertRaisesRegex(ValueError, "超出.*执行上限"):
            TestPlanCompiler().build_draft(
                bundle,
                PlanCandidate.model_validate(payload),
                catalog,
            )


if __name__ == "__main__":
    unittest.main()
