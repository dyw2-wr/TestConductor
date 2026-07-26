from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.test_platform.approval_service import (
    persist_execution_plan_review,
    persist_test_plan_approval,
    persist_test_plan_changes,
)
from apps.test_platform.models import (
    ExecutionPlanArtifact,
    TestPlanArtifact,
    TestResourceProfile,
    TestWorkflow,
)


class ApprovalConcurrencyTests(TestCase):
    def setUp(self):
        self.profile = TestResourceProfile.objects.create(
            name="Approval resource",
            port_host="127.0.0.1",
            port_number=9000,
        )

    def test_stale_test_plan_approval_cannot_overwrite_winner(self):
        workflow = TestWorkflow.objects.create(
            title="Concurrent approval",
            resource_profile=self.profile,
            allowed_channels=["port"],
        )
        artifact = TestPlanArtifact.objects.create(
            source_intent=workflow,
            resource_profile=self.profile,
            title="Review me",
            test_categories=["port"],
            design_id="DESIGN-CONCURRENT",
            version=1,
            content_hash="sha256:" + "1" * 64,
            status=TestPlanArtifact.Status.REVIEW,
        )
        persist_test_plan_approval(
            artifact.pk,
            expected_status=TestPlanArtifact.Status.REVIEW,
            review_payload={"decision": "approved"},
            approved_bundle={"design": {"design_id": "DESIGN-CONCURRENT"}},
        )
        with self.assertRaisesRegex(ValidationError, "其他请求处理"):
            persist_test_plan_approval(
                artifact.pk,
                expected_status=TestPlanArtifact.Status.REVIEW,
                review_payload={"decision": "approved"},
                approved_bundle={"design": {"design_id": "TAMPERED"}},
            )

        artifact.refresh_from_db()
        workflow.refresh_from_db()
        self.assertEqual(artifact.review_payload["decision"], "approved")
        self.assertEqual(artifact.approved_bundle["design"]["design_id"], "DESIGN-CONCURRENT")
        self.assertEqual(workflow.status, TestWorkflow.Status.DESIGN_APPROVED)

    def test_execution_approval_supersedes_previous_version_atomically(self):
        source = TestPlanArtifact.objects.create(
            resource_profile=self.profile,
            title="Approved design",
            test_categories=["port"],
            design_id="DESIGN-VERSIONS",
            version=1,
            content_hash="sha256:" + "2" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        first = self._execution(source, version=1, status=ExecutionPlanArtifact.Status.APPROVED)
        second = self._execution(source, version=2, status=ExecutionPlanArtifact.Status.REVIEW)

        persist_execution_plan_review(
            second.pk,
            expected_status=ExecutionPlanArtifact.Status.REVIEW,
            review_payload={"decision": "approved"},
            approved_bundle={"plan": {"plan_id": second.plan_id}},
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, ExecutionPlanArtifact.Status.SUPERSEDED)
        self.assertEqual(second.status, ExecutionPlanArtifact.Status.APPROVED)

    def test_stale_return_request_cannot_overwrite_test_plan_approval(self):
        workflow = TestWorkflow.objects.create(
            title="Approve versus return",
            resource_profile=self.profile,
            allowed_channels=["port"],
        )
        artifact = TestPlanArtifact.objects.create(
            source_intent=workflow,
            resource_profile=self.profile,
            title="Review race",
            test_categories=["port"],
            design_id="DESIGN-APPROVE-RETURN",
            version=1,
            content_hash="sha256:" + "5" * 64,
            status=TestPlanArtifact.Status.REVIEW,
        )
        persist_test_plan_approval(
            artifact.pk,
            expected_status=TestPlanArtifact.Status.REVIEW,
            review_payload={"decision": "approved"},
            approved_bundle={"design": {"design_id": artifact.design_id}},
        )
        with self.assertRaisesRegex(ValidationError, "其他请求处理"):
            persist_test_plan_changes(
                artifact.pk,
                expected_status=TestPlanArtifact.Status.REVIEW,
                review_payload={"decision": "changes_requested"},
            )
        artifact.refresh_from_db()
        workflow.refresh_from_db()
        self.assertEqual(artifact.status, TestPlanArtifact.Status.APPROVED)
        self.assertEqual(workflow.status, TestWorkflow.Status.DESIGN_APPROVED)

    def test_stale_execution_approval_cannot_overwrite_winner(self):
        source = TestPlanArtifact.objects.create(
            resource_profile=self.profile,
            title="Approved design",
            test_categories=["port"],
            design_id="DESIGN-STALE-PLAN",
            version=1,
            content_hash="sha256:" + "3" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        artifact = self._execution(source, version=1, status=ExecutionPlanArtifact.Status.REVIEW)
        persist_execution_plan_review(
            artifact.pk,
            expected_status=ExecutionPlanArtifact.Status.REVIEW,
            review_payload={"decision": "approved"},
            approved_bundle={"plan": {"plan_id": artifact.plan_id}},
        )
        with self.assertRaisesRegex(ValidationError, "其他请求处理"):
            persist_execution_plan_review(
                artifact.pk,
                expected_status=ExecutionPlanArtifact.Status.REVIEW,
                review_payload={"decision": "approved"},
                approved_bundle={"plan": {"plan_id": "TAMPERED"}},
            )
        artifact.refresh_from_db()
        self.assertEqual(artifact.approved_bundle["plan"]["plan_id"], artifact.plan_id)

    def test_database_rejects_two_approved_versions(self):
        source = TestPlanArtifact.objects.create(
            resource_profile=self.profile,
            title="Approved design",
            test_categories=["port"],
            design_id="DESIGN-UNIQUE-APPROVAL",
            version=1,
            content_hash="sha256:" + "4" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        self._execution(source, version=1, status=ExecutionPlanArtifact.Status.APPROVED)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._execution(source, version=2, status=ExecutionPlanArtifact.Status.APPROVED)

    def _execution(self, source, *, version, status):
        return ExecutionPlanArtifact.objects.create(
            source_test_plan=source,
            resource_profile=self.profile,
            title=f"Execution v{version}",
            test_categories=["port"],
            plan_id="PLAN-APPROVAL",
            version=version,
            content_hash="sha256:" + str(version) * 64,
            artifact_root_ref=f"plans/v{version}",
            status=status,
        )
