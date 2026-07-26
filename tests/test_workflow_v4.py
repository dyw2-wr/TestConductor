from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from apps.test_platform.intent.builder import DefaultDesignBuilder
from apps.test_platform.intent.contracts import (
    DesignStatus,
    ReviewDecision,
    TestDesignRequest,
)
from apps.test_platform.intent.prompt_builder import DefaultDesignPromptBuilder
from apps.test_platform.intent.service import TestDesignPipeline
from apps.test_platform.planning.catalogs import PlanningCatalogSnapshot
from apps.test_platform.planning.compiler import TestPlanCompiler
from apps.test_platform.planning.contracts import PlanReviewDecision, PlanStatus
from apps.test_platform.planning.planner import (
    DefaultPlanPromptBuilder,
    PlanDraftGenerator,
)
from apps.test_platform.runners.contracts import (
    ExecutionSummary,
    RunStatus,
    RuntimeContext,
)
from apps.test_platform.workflow import IntentToExecutionWorkflow


FIXTURES = Path(__file__).parent / "fixtures_v4"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FixtureGateway:
    def __init__(self, payload):
        self.payload = payload

    def generate(self, messages, output_schema):
        return self.payload


class _RecordingCoordinator:
    def __init__(self):
        self.calls = []

    def execute(self, bundle, artifact_root, context, *, run_id=None):
        self.calls.append((bundle, Path(artifact_root), context, run_id))
        return ExecutionSummary(
            run_id=run_id or "RUN-WORKFLOW-V4",
            status=RunStatus.PASSED,
        )


def _build_workflow(coordinator=None):
    design_pipeline = TestDesignPipeline(
        DefaultDesignBuilder(
            DefaultDesignPromptBuilder(),
            _FixtureGateway(_load("account_api_design_candidate.json")),
        )
    )
    compiler = TestPlanCompiler()
    plan_generator = PlanDraftGenerator(
        DefaultPlanPromptBuilder(),
        _FixtureGateway(_load("account_api_plan_candidate.json")),
        compiler,
    )
    return IntentToExecutionWorkflow(
        design_pipeline,
        plan_generator,
        coordinator=coordinator,
    )


class WorkflowV4Tests(unittest.TestCase):
    def setUp(self):
        self.request = TestDesignRequest.model_validate(
            _load("account_api_design_request.json")
        )
        self.catalog = PlanningCatalogSnapshot.build(
            **_load("planning_catalog_content.json")
        )

    def test_explicit_reviewed_handoffs_reach_execution(self):
        coordinator = _RecordingCoordinator()
        workflow = _build_workflow(coordinator)

        generated = workflow.generate_design(self.request)
        reviewed_design = workflow.review_design(
            generated,
            decision=ReviewDecision.APPROVED,
            comments="逻辑场景与需求已核对",
        )
        self.assertEqual(reviewed_design.design.status, DesignStatus.APPROVED)
        self.assertIsNotNone(reviewed_design.approved_bundle)

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            compiled = workflow.compile_plan(
                reviewed_design.approved_bundle,
                self.catalog,
                root,
            )
            reviewed_plan = workflow.review_plan(
                compiled,
                self.catalog,
                decision=PlanReviewDecision.APPROVED,
                comments="执行资源、顺序与清理已核对",
            )
            self.assertEqual(reviewed_plan.plan.status, PlanStatus.APPROVED)
            self.assertIsNotNone(reviewed_plan.approved_bundle)
            self.assertTrue(
                any(
                    (category / reviewed_plan.plan.plan_id / f"v{reviewed_plan.plan.version}").is_dir()
                    for category in (root / "generated-files").iterdir()
                )
            )

            context = RuntimeContext()
            summary = workflow.execute(
                reviewed_plan.approved_bundle,
                root,
                context,
                run_id="RUN-WORKFLOW-V4",
            )

        self.assertEqual(summary.status, RunStatus.PASSED)
        self.assertEqual(len(coordinator.calls), 1)
        called_bundle, called_root, called_context, called_run_id = coordinator.calls[0]
        self.assertIs(called_bundle, reviewed_plan.approved_bundle)
        self.assertEqual(called_root, root)
        self.assertIs(called_context, context)
        self.assertEqual(called_run_id, "RUN-WORKFLOW-V4")

    def test_non_approved_review_has_no_handoff_bundle(self):
        workflow = _build_workflow()
        generated = workflow.generate_design(self.request)

        reviewed = workflow.review_design(
            generated,
            decision=ReviewDecision.CHANGES_REQUESTED,
            comments="需要补充失败分支",
        )

        self.assertEqual(reviewed.design.status, DesignStatus.CHANGES_REQUESTED)
        self.assertIsNone(reviewed.approved_bundle)


if __name__ == "__main__":
    unittest.main()
