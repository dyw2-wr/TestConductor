"""第一层 TestDesign v4 编排入口。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from uuid import uuid4

from .builder import DefaultDesignBuilder, DesignBuilder
from .contracts import (
    ApprovedKnowledge,
    ApprovedTestDesignBundle,
    DesignStatus,
    RequirementInput,
    RequirementSnapshot,
    ReviewDecision,
    TestDesign,
    TestDesignCandidate,
    TestDesignInputSnapshot,
    TestDesignRequest,
    TestDesignReview,
    TestDesignValidationReport,
    compute_design_content_hash,
    compute_input_content_hash,
    compute_review_content_hash,
    compute_validation_content_hash,
    contains_secret_literal,
)
from .knowledge import ApprovedKnowledgeResolver, InMemoryApprovedKnowledgeResolver
from .reviewer import InMemoryTestDesignReviewer
from .validator import DefaultDesignValidator, DesignValidator


@dataclass
class TestDesignGenerationResult:
    request: TestDesignRequest
    candidate: TestDesignCandidate
    design: TestDesign
    input_snapshot: TestDesignInputSnapshot
    validation: TestDesignValidationReport


class TestDesignPipeline:
    def __init__(
        self,
        design_builder: DesignBuilder,
        *,
        knowledge_resolver: ApprovedKnowledgeResolver | None = None,
        validator: DesignValidator | None = None,
        reviewer: InMemoryTestDesignReviewer | None = None,
    ):
        self.design_builder = design_builder
        self.knowledge_resolver = knowledge_resolver or InMemoryApprovedKnowledgeResolver()
        self.validator = validator or DefaultDesignValidator()
        self.reviewer = reviewer or InMemoryTestDesignReviewer()

    def generate(
        self,
        request: TestDesignRequest,
        *,
        design_id: str | None = None,
        version: int = 1,
        review_feedback: str | None = None,
    ) -> TestDesignGenerationResult:
        prepared = self._prepare_request(request)
        feedback = self._validate_feedback(review_feedback)

        # 调用方只能选择 scope；实际内容必须由 resolver 返回已批准版本。
        approved_knowledge = self.knowledge_resolver.resolve(
            prepared.selections.knowledge_scope_ids,
            query_text="\n\n".join(item.content for item in prepared.requirements),
        )
        self._validate_model_input(prepared, approved_knowledge)
        input_snapshot = self._build_input_snapshot(
            prepared,
            approved_knowledge,
            feedback,
        )

        candidate = self.design_builder.build_candidate(
            prepared,
            approved_knowledge,
            review_feedback=feedback,
        )
        design = self.design_builder.compile(
            prepared,
            candidate,
            design_id=design_id,
            version=version,
        )
        validation = self.validator.validate(design, input_snapshot)
        return TestDesignGenerationResult(
            request=prepared,
            candidate=candidate,
            design=design,
            input_snapshot=input_snapshot,
            validation=validation,
        )

    @staticmethod
    def _prepare_request(request: TestDesignRequest) -> TestDesignRequest:
        # Round-trip validation and deep copying keep caller-owned mutable values out.
        prepared = TestDesignRequest.model_validate(request.model_dump(mode="json"))
        request_id = prepared.request_id or f"request-{uuid4().hex}"
        used_ids = {
            item.requirement_id
            for item in prepared.requirements
            if item.requirement_id is not None
        }
        requirements: list[RequirementInput] = []
        sequence = 1
        for item in prepared.requirements:
            requirement_id = item.requirement_id
            if requirement_id is None:
                while f"REQ-{sequence:04d}" in used_ids:
                    sequence += 1
                requirement_id = f"REQ-{sequence:04d}"
                used_ids.add(requirement_id)
                sequence += 1
            requirements.append(
                RequirementInput(requirement_id=requirement_id, content=item.content)
            )
        return TestDesignRequest(
            request_id=request_id,
            requirements=requirements,
            target=prepared.target.model_copy(deep=True),
            selections=prepared.selections.model_copy(deep=True),
        )

    @staticmethod
    def _validate_feedback(review_feedback: str | None) -> str | None:
        if review_feedback is None:
            return None
        if not review_feedback.strip() or len(review_feedback.encode("utf-8")) > 4096:
            raise ValueError("review_feedback 必须为 1-4096 字节")
        if contains_secret_literal(review_feedback):
            raise ValueError("review_feedback 不能包含凭据实际值")
        return review_feedback

    @staticmethod
    def _validate_model_input(
        request: TestDesignRequest,
        approved_knowledge: list[ApprovedKnowledge],
    ) -> None:
        requested_scopes = request.selections.knowledge_scope_ids
        resolved_scopes = [item.scope_id for item in approved_knowledge]
        if resolved_scopes != requested_scopes or len(set(resolved_scopes)) != len(
            resolved_scopes
        ):
            raise ValueError("knowledge resolver 必须按选择顺序精确返回已批准范围")

        requirement_ids = [item.requirement_id for item in request.requirements]
        if None in requirement_ids or len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("进入模型前必须分配唯一 requirement_id")
        total_bytes = 0
        for requirement in request.requirements:
            total_bytes += len(requirement.content.encode("utf-8"))
            if contains_secret_literal(requirement.content):
                raise ValueError(
                    f"需求 {requirement.requirement_id} 疑似包含凭据实际值"
                )
        for item in approved_knowledge:
            total_bytes += len(item.content.encode("utf-8"))
            if contains_secret_literal(item.content):
                raise ValueError(f"知识 {item.knowledge_id} 疑似包含凭据实际值")
        if total_bytes > 256 * 1024:
            raise ValueError("模型输入正文总量不能超过 256 KiB；请拆分后分批设计")

    @staticmethod
    def _build_input_snapshot(
        request: TestDesignRequest,
        approved_knowledge: list[ApprovedKnowledge],
        review_feedback: str | None,
    ) -> TestDesignInputSnapshot:
        if request.request_id is None:
            raise ValueError("input snapshot 缺少 request_id")
        requirements = [
            RequirementSnapshot(
                requirement_id=item.requirement_id or "",
                content=item.content,
                content_hash=_text_hash(item.content),
            )
            for item in request.requirements
        ]
        return TestDesignInputSnapshot.build(
            request_id=request.request_id,
            requirements=requirements,
            target=request.target.model_copy(deep=True),
            selections=request.selections.model_copy(deep=True),
            approved_knowledge=[item.model_copy(deep=True) for item in approved_knowledge],
            review_feedback=review_feedback,
        )

    def review(
        self,
        result: TestDesignGenerationResult,
        decision: ReviewDecision,
        comments: str,
    ) -> tuple[TestDesign, TestDesignReview]:
        updated, review = self.reviewer.review(
            result.design,
            result.validation,
            result.input_snapshot,
            decision,
            comments,
        )
        # Consume this exact generation result. Callers cannot keep replaying the
        # original draft reference after a changes_requested/approved decision.
        result.design = updated
        return updated, review

    def regenerate(
        self,
        result: TestDesignGenerationResult,
        changes_requested_review: TestDesignReview,
        revised_request: TestDesignRequest,
    ) -> TestDesignGenerationResult:
        """用修订后的原文生成同一 design 的下一个草稿版本。"""

        review = changes_requested_review
        if review.decision != ReviewDecision.CHANGES_REQUESTED:
            raise ValueError("只有 changes_requested 审核可以触发重新生成")
        if (
            review.design_id != result.design.design_id
            or review.design_version != result.design.version
            or review.design_content_hash != compute_design_content_hash(result.design)
            or review.input_content_hash
            != compute_input_content_hash(result.input_snapshot)
            or review.review_content_hash != compute_review_content_hash(review)
            or review.validation_content_hash
            != compute_validation_content_hash(result.validation)
        ):
            raise ValueError("重新生成审核记录与基础 design/input 不匹配")
        if revised_request.request_id != result.input_snapshot.request_id:
            raise ValueError("重新生成必须沿用原 request_id；新需求应创建新的 design")
        if revised_request.target.model_dump(mode="json") != result.input_snapshot.target.model_dump(
            mode="json"
        ):
            raise ValueError("重新生成不能更换 target；新目标应创建新的 design")
        if any(item.requirement_id is None for item in revised_request.requirements):
            raise ValueError(
                "重新生成的每份需求必须携带首次生成后固定的 requirement_id；"
                "新需求也必须由调用方分配新 ID"
            )
        return self.generate(
            revised_request,
            design_id=result.design.design_id,
            version=result.design.version + 1,
            review_feedback=review.comments,
        )

    def build_approved_bundle(
        self,
        result: TestDesignGenerationResult,
        approved_design: TestDesign,
        review: TestDesignReview,
    ) -> ApprovedTestDesignBundle:
        if approved_design.status != DesignStatus.APPROVED:
            raise ValueError("只能为 approved TestDesign 创建 bundle")
        if approved_design.model_dump(
            mode="json", exclude={"status"}
        ) != result.design.model_dump(mode="json", exclude={"status"}):
            raise ValueError("批准内容与生成结果不一致")
        return ApprovedTestDesignBundle.model_validate(
            {
                "design": approved_design.model_dump(mode="json"),
                "input_snapshot": result.input_snapshot.model_dump(mode="json"),
                "validation": result.validation.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
                "trace_id": f"trace-{approved_design.design_id}-v{approved_design.version}",
            }
        )


def build_default_pipeline(
    prompt_builder,
    model_gateway,
    *,
    knowledge_resolver: ApprovedKnowledgeResolver | None = None,
) -> TestDesignPipeline:
    return TestDesignPipeline(
        DefaultDesignBuilder(prompt_builder, model_gateway),
        knowledge_resolver=knowledge_resolver,
    )


def _text_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
