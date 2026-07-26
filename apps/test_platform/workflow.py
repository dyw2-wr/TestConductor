"""Thin application facade for the initial intent -> plan -> execution flow.

The facade deliberately keeps review as an explicit operation.  It only wires
the already-tested v4 services together so a CLI, a future HTTP API, and the
local demonstration use the same handoff rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from apps.test_platform.intent.contracts import (
    ApprovedTestDesignBundle,
    ReviewDecision,
    TestDesign,
    TestDesignRequest,
    TestDesignReview,
)
from apps.test_platform.intent.service import (
    TestDesignGenerationResult,
    TestDesignPipeline,
)
from apps.test_platform.ingestion import (
    IngestionLimits,
    IngestionResult,
    InputFile,
    prepare_request,
)
from apps.test_platform.planning.catalogs import PlanningCatalogSnapshot
from apps.test_platform.planning.contracts import (
    ApprovedTestPlanBundle,
    PlanReview,
    PlanReviewDecision,
    TestPlanDraft,
)
from apps.test_platform.planning.compiler import (
    PlanCompilationResult,
    TestPlanCompiler,
)
from apps.test_platform.planning.planner import PlanDraftGenerator
from apps.test_platform.runners.contracts import ExecutionSummary, RuntimeContext
from apps.test_platform.runners.execution import ExecutionCoordinator
from apps.test_platform.run_history import get_default_run_history_recorder


@dataclass(frozen=True)
class DesignReviewOutput:
    result: TestDesignGenerationResult
    design: TestDesign
    review: TestDesignReview
    approved_bundle: ApprovedTestDesignBundle | None


@dataclass(frozen=True)
class PlanReviewOutput:
    result: PlanCompilationResult
    plan: TestPlanDraft
    review: PlanReview
    approved_bundle: ApprovedTestPlanBundle | None


class IntentToExecutionWorkflow:
    """Explicit facade over ingestion and the three v4 layers.

    No method auto-approves a candidate.  Callers may use an actual LLM gateway
    or a deterministic fixture gateway, but both go through the same contracts,
    compiler, hash checks, and runner coordinator.
    """

    def __init__(
        self,
        design_pipeline: TestDesignPipeline,
        plan_generator: PlanDraftGenerator,
        *,
        plan_compiler: TestPlanCompiler | None = None,
        coordinator: ExecutionCoordinator | None = None,
    ) -> None:
        self.design_pipeline = design_pipeline
        self.plan_generator = plan_generator
        self.plan_compiler = plan_compiler or plan_generator.compiler
        self.coordinator = coordinator or ExecutionCoordinator(
            run_history_recorder=get_default_run_history_recorder()
        )

    def prepare_design_request(
        self,
        *,
        frontend_text: str | None = None,
        files: list[InputFile] | None = None,
        target,
        selections,
        request_id: str | None = None,
        limits: IngestionLimits | None = None,
    ) -> IngestionResult:
        """Convert frontend text/files to the existing TestDesignRequest.

        This step is deterministic and does not call the model.  Keeping it
        separate lets a UI show extraction warnings before generation.
        """

        return prepare_request(
            frontend_text=frontend_text,
            files=files,
            target=target,
            selections=selections,
            request_id=request_id,
            limits=limits,
        )

    def generate_design(
        self,
        request: TestDesignRequest,
        *,
        design_id: str | None = None,
        version: int = 1,
        review_feedback: str | None = None,
    ) -> TestDesignGenerationResult:
        """Run only the first-layer model and deterministic validation."""

        return self.design_pipeline.generate(
            request,
            design_id=design_id,
            version=version,
            review_feedback=review_feedback,
        )

    def review_design(
        self,
        result: TestDesignGenerationResult,
        *,
        decision: ReviewDecision,
        comments: str,
    ) -> DesignReviewOutput:
        """Consume the generation result and return the reviewed design."""

        design, review = self.design_pipeline.review(
            result,
            decision=decision,
            comments=comments,
        )
        bundle = None
        if review.decision == ReviewDecision.APPROVED:
            bundle = self.design_pipeline.build_approved_bundle(
                result,
                design,
                review,
            )
        return DesignReviewOutput(
            result=result,
            design=design,
            review=review,
            approved_bundle=bundle,
        )

    def compile_plan(
        self,
        design_bundle: ApprovedTestDesignBundle,
        catalog: PlanningCatalogSnapshot,
        artifact_root: str | Path,
        *,
        plan_id: str | None = None,
        version: int = 1,
        review_feedback: str | None = None,
        execution_input: dict | None = None,
    ) -> PlanCompilationResult:
        """Run the second-layer model, compiler, artifact adapters, and validator."""

        plan = self.plan_generator.generate(
            design_bundle,
            catalog,
            plan_id=plan_id,
            version=version,
            review_feedback=review_feedback,
            execution_input=execution_input,
        )
        # Compilation is intentionally separate from model generation so the
        # caller can inspect the draft before the final plan review.
        return self.plan_compiler.compile(
            design_bundle,
            plan,
            catalog,
            artifact_root,
        )

    def review_plan(
        self,
        result: PlanCompilationResult,
        catalog: PlanningCatalogSnapshot,
        *,
        decision: PlanReviewDecision,
        comments: str,
    ) -> PlanReviewOutput:
        """Consume the compilation result and optionally create an approved bundle."""

        plan, review = self.plan_compiler.review(
            result,
            decision=decision,
            comments=comments,
        )
        bundle = None
        if review.decision == PlanReviewDecision.APPROVED:
            bundle = self.plan_compiler.build_approved_bundle(
                result,
                plan,
                review,
                catalog,
            )
        return PlanReviewOutput(
            result=result,
            plan=plan,
            review=review,
            approved_bundle=bundle,
        )

    def execute(
        self,
        bundle: ApprovedTestPlanBundle,
        artifact_root: str | Path,
        context: RuntimeContext,
        *,
        run_id: str | None = None,
    ) -> ExecutionSummary:
        """Execute only an already approved plan bundle."""

        return self.coordinator.execute(
            bundle,
            artifact_root,
            context,
            run_id=run_id,
        )

__all__ = [
    "DesignReviewOutput",
    "IntentToExecutionWorkflow",
    "PlanReviewOutput",
]
