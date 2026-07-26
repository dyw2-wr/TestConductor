"""Validated database handoffs between the product workflow pages."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from .intent.contracts import (
    ApprovedTestDesignBundle,
    compute_design_content_hash,
)
from .models import TestPlanArtifact, TestResourceProfile


def import_approved_test_plan(
    payload: dict,
    resource_profile: TestResourceProfile,
) -> TestPlanArtifact:
    """Validate and persist an approved upstream artifact for layer-two use.

    The import is idempotent by design identity, version, and content hash. It
    deliberately accepts no caller-provided status or hash overrides.
    """

    bundle = ApprovedTestDesignBundle.model_validate(payload)
    if not resource_profile.enabled:
        raise ValidationError("测试资源已停用")
    target = bundle.design.target
    if (
        target.system_id != resource_profile.system_id
        or target.environment != resource_profile.environment
    ):
        raise ValidationError("导入产物的目标系统或环境与测试资源不一致")
    selected_channels = {
        item.value if hasattr(item, "value") else str(item)
        for item in bundle.design.selections.allowed_channels
    }
    unsupported = sorted(selected_channels - resource_profile.configured_channels())
    if unsupported:
        raise ValidationError("测试资源不支持导入产物渠道: " + ", ".join(unsupported))

    content_hash = compute_design_content_hash(bundle.design)
    defaults = {
        "resource_profile": resource_profile,
        "source_kind": TestPlanArtifact.SourceKind.IMPORTED,
        "title": bundle.design.title.strip()[:200],
        "test_categories": sorted(selected_channels),
        "review_payload": bundle.review.model_dump(mode="json"),
        "approved_bundle": bundle.model_dump(mode="json"),
        "status": TestPlanArtifact.Status.APPROVED,
    }
    with transaction.atomic():
        artifact, created = TestPlanArtifact.objects.get_or_create(
            design_id=bundle.design.design_id,
            version=bundle.design.version,
            content_hash=content_hash,
            defaults=defaults,
        )
    if not created and artifact.approved_bundle != defaults["approved_bundle"]:
        raise ValidationError("相同设计身份已存在不同审批产物")
    return artifact


__all__ = ["import_approved_test_plan"]
