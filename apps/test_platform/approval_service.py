"""Atomic persistence boundaries for human approval decisions."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ExecutionPlanArtifact, TestPlanArtifact, TestWorkflow


def persist_test_plan_approval(
    artifact_id: int,
    *,
    expected_status: str,
    review_payload: dict[str, Any],
    approved_bundle: dict[str, Any],
) -> TestPlanArtifact:
    """Approve exactly one still-reviewable test-plan revision.

    The status predicate is the concurrency token.  A stale browser request can
    no longer overwrite the approved snapshot saved by the winner.
    """

    if expected_status != TestPlanArtifact.Status.REVIEW:
        raise ValidationError("只有待审批测试计划可以审批通过")
    now = timezone.now()
    with transaction.atomic():
        claimed = TestPlanArtifact.objects.filter(
            pk=artifact_id,
            status=expected_status,
        ).update(
            review_payload=review_payload,
            approved_bundle=approved_bundle,
            status=TestPlanArtifact.Status.APPROVED,
            last_error="",
            updated_at=now,
        )
        if claimed != 1:
            raise ValidationError("该测试计划已被其他请求处理，请刷新页面")
        artifact = TestPlanArtifact.objects.get(pk=artifact_id)
        if artifact.source_intent_id:
            TestWorkflow.objects.filter(pk=artifact.source_intent_id).update(
                status=TestWorkflow.Status.DESIGN_APPROVED,
                updated_at=now,
            )
    return artifact


def persist_execution_plan_review(
    artifact_id: int,
    *,
    expected_status: str,
    review_payload: dict[str, Any],
    approved_bundle: dict[str, Any] | None,
) -> ExecutionPlanArtifact:
    """Persist one execution-plan decision without last-writer-wins races."""

    if expected_status not in {
        ExecutionPlanArtifact.Status.REVIEW,
        ExecutionPlanArtifact.Status.BLOCKED,
    }:
        raise ValidationError("只有待审批或已阻塞执行计划可以处理")
    target_status = (
        ExecutionPlanArtifact.Status.APPROVED
        if approved_bundle is not None
        else ExecutionPlanArtifact.Status.CHANGES
    )
    now = timezone.now()
    try:
        with transaction.atomic():
            current = ExecutionPlanArtifact.objects.only(
                "source_test_plan_id"
            ).get(pk=artifact_id)
            # Lock the source test plan so simultaneous review requests cannot
            # overwrite the same execution-plan snapshot.
            TestPlanArtifact.objects.select_for_update().get(
                pk=current.source_test_plan_id
            )
            values: dict[str, Any] = {
                "review_payload": review_payload,
                "status": target_status,
                "last_error": "",
                "updated_at": now,
            }
            if approved_bundle is not None:
                values["approved_bundle"] = approved_bundle
            claimed = ExecutionPlanArtifact.objects.filter(
                pk=artifact_id,
                status=expected_status,
            ).update(**values)
            if claimed != 1:
                raise ValidationError("该执行计划已被其他请求处理，请刷新页面")
    except IntegrityError as exc:
        raise ValidationError("执行计划审批保存失败，请刷新页面后重试") from exc
    return ExecutionPlanArtifact.objects.get(pk=artifact_id)


__all__ = [
    "persist_execution_plan_review",
    "persist_test_plan_approval",
]
