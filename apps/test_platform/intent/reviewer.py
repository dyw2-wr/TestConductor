"""TestDesign v4 人工审核记录和状态转换。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .contracts import (
    DesignStatus,
    ReviewDecision,
    TestDesign,
    TestDesignInputSnapshot,
    TestDesignReview,
    TestDesignValidationReport,
    compute_design_content_hash,
    compute_input_content_hash,
    compute_review_content_hash,
    compute_validation_content_hash,
    contains_secret_literal,
)


class InMemoryTestDesignReviewer:
    def __init__(self) -> None:
        # A review decision consumes one immutable design revision. This small
        # in-memory ledger prevents replaying an older draft object with a new
        # decision; a persistent store can replace it at the application edge.
        self._reviewed_revisions: set[tuple[str, int, str]] = set()

    def record_review(
        self,
        design: TestDesign,
        validation: TestDesignValidationReport,
        input_snapshot: TestDesignInputSnapshot,
        decision: ReviewDecision,
        comments: str,
    ) -> TestDesignReview:
        if not comments.strip():
            raise ValueError("comments 不能为空")
        if len(comments.encode("utf-8")) > 4096:
            raise ValueError("审核意见不能超过 4096 字节")
        if contains_secret_literal(comments):
            raise ValueError("审核意见不能包含凭据实际值")
        revision_key = (
            design.design_id,
            design.version,
            compute_design_content_hash(design),
        )
        if revision_key in self._reviewed_revisions:
            raise ValueError(
                "同一 design/version/content 已审核，必须重新生成更高版本后再审核"
            )
        payload = {
            "review_id": f"review-{uuid4().hex}",
            "design_id": design.design_id,
            "design_version": design.version,
            "decision": decision,
            "comments": comments,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "design_content_hash": compute_design_content_hash(design),
            "input_content_hash": compute_input_content_hash(input_snapshot),
            "validation_content_hash": compute_validation_content_hash(validation),
        }
        payload["review_content_hash"] = compute_review_content_hash(payload)
        return TestDesignReview(**payload)

    def apply_review(
        self,
        design: TestDesign,
        validation: TestDesignValidationReport,
        review: TestDesignReview,
        input_snapshot: TestDesignInputSnapshot,
    ) -> TestDesign:
        design_hash = compute_design_content_hash(design)
        input_hash = compute_input_content_hash(input_snapshot)
        if validation.design_id != design.design_id or validation.design_version != design.version:
            raise ValueError("校验报告与 TestDesign 版本不匹配")
        if validation.design_content_hash != design_hash:
            raise ValueError("校验报告与 TestDesign 内容不匹配")
        if validation.input_content_hash != input_hash:
            raise ValueError("校验报告与模型输入不匹配")
        if review.design_id != design.design_id or review.design_version != design.version:
            raise ValueError("审核记录与 TestDesign 版本不匹配")
        if review.design_content_hash != design_hash:
            raise ValueError("审核记录与 TestDesign 内容不匹配")
        if review.input_content_hash != input_hash:
            raise ValueError("审核记录与模型输入不匹配")
        if review.validation_content_hash != compute_validation_content_hash(validation):
            raise ValueError("审核记录与校验报告不匹配")
        if review.review_content_hash != compute_review_content_hash(review):
            raise ValueError("审核记录内容 hash 不匹配")
        if design.status not in {
            DesignStatus.DRAFT,
            DesignStatus.IN_REVIEW,
            DesignStatus.CHANGES_REQUESTED,
        }:
            raise ValueError("已结束的 TestDesign 不能重复审核")
        if (
            design.status == DesignStatus.CHANGES_REQUESTED
            and review.decision == ReviewDecision.APPROVED
        ):
            raise ValueError(
                "changes_requested 版本不能原地批准；必须重新生成更高版本"
            )
        if review.decision == ReviewDecision.APPROVED:
            if not validation.passed:
                raise ValueError("确定性校验未通过，不能批准")
            status = DesignStatus.APPROVED
        elif review.decision == ReviewDecision.CHANGES_REQUESTED:
            status = DesignStatus.CHANGES_REQUESTED
        else:
            status = DesignStatus.REJECTED
        return design.model_copy(update={"status": status})

    def review(
        self,
        design: TestDesign,
        validation: TestDesignValidationReport,
        input_snapshot: TestDesignInputSnapshot,
        decision: ReviewDecision,
        comments: str,
    ) -> tuple[TestDesign, TestDesignReview]:
        reviewable = (
            design.model_copy(update={"status": DesignStatus.IN_REVIEW})
            if design.status == DesignStatus.DRAFT
            else design
        )
        review = self.record_review(
            reviewable,
            validation,
            input_snapshot,
            decision,
            comments,
        )
        updated = self.apply_review(reviewable, validation, review, input_snapshot)
        self._reviewed_revisions.add(
            (
                design.design_id,
                design.version,
                compute_design_content_hash(design),
            )
        )
        return updated, review
