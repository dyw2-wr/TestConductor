from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.test_platform import models
from apps.test_platform.admin import TestIntentImportForm as IntentImportForm
from apps.test_platform.generation_service import (
    queue_design_generation,
    queue_execution_plan_generation,
)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AdminRecordToolsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.test",
            password="password",
        )
        cls.resource = models.TestResourceProfile.objects.create(
            name="后台回归测试资源",
            system_id="admin-regression",
            environment="test",
            port_host="127.0.0.1",
            port_number=9,
        )
        cls.intent = models.TestWorkflow.objects.create(
            title="后台回归测试意图",
            requirement_text="验证后台记录操作",
            system_id="admin-regression",
            target_environment="test",
            resource_profile=cls.resource,
            allowed_channels=["port"],
            is_marked=True,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def create_plan(self, *, title, status, marked):
        sequence = models.TestPlanArtifact.objects.count() + 1
        return models.TestPlanArtifact.objects.create(
            source_intent=self.intent,
            resource_profile=self.resource,
            title=title,
            test_categories=["port"],
            design_id=f"design-{sequence}",
            version=1,
            content_hash=f"sha256:{sequence}",
            generation_result={
                "design": {"title": title},
                "validation": {"passed": True, "findings": []},
            },
            status=status,
            is_marked=marked,
        )

    def create_execution_plan(self, source_plan):
        sequence = models.ExecutionPlanArtifact.objects.count() + 1
        return models.ExecutionPlanArtifact.objects.create(
            source_test_plan=source_plan,
            resource_profile=self.resource,
            title=f"后台回归执行计划 {sequence}",
            test_categories=["port"],
            plan_id=f"admin-regression-plan-{sequence}",
            version=1,
            content_hash=f"sha256:execution-{sequence}",
            compilation_result={"plan": {"flows": []}},
            approved_bundle={"plan": {"flows": []}},
            artifact_root_ref=f"admin-regression/execution-{sequence}",
            status=models.ExecutionPlanArtifact.Status.APPROVED,
        )

    def test_marked_test_plan_view_includes_every_status(self):
        pending = self.create_plan(
            title="已标记待处理计划",
            status=models.TestPlanArtifact.Status.REVIEW,
            marked=True,
        )
        approved = self.create_plan(
            title="已标记已审批计划",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=True,
        )
        blocked = self.create_plan(
            title="已标记失败计划",
            status=models.TestPlanArtifact.Status.BLOCKED,
            marked=True,
        )
        unmarked = self.create_plan(
            title="未标记计划",
            status=models.TestPlanArtifact.Status.REVIEW,
            marked=False,
        )

        response = self.client.get(
            reverse("admin:test_platform_testplanartifact_changelist"),
            {"marked": "yes"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, pending.title)
        self.assertContains(response, approved.title)
        self.assertContains(response, blocked.title)
        self.assertNotContains(response, unmarked.title)

    def test_default_test_plan_view_remains_pending_only(self):
        pending = self.create_plan(
            title="默认待处理计划",
            status=models.TestPlanArtifact.Status.REVIEW,
            marked=False,
        )
        approved = self.create_plan(
            title="默认已审批计划",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )

        response = self.client.get(
            reverse("admin:test_platform_testplanartifact_changelist")
        )

        self.assertContains(response, pending.title)
        self.assertNotContains(response, approved.title)

    def test_execution_history_detail_has_no_save_button(self):
        source_plan = self.create_plan(
            title="执行历史来源计划",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        execution_plan = self.create_execution_plan(source_plan)
        run = models.TestExecutionRun.objects.create(
            run_id="admin-regression-run",
            status=models.TestExecutionRun.Status.FAILED,
            report_status=models.TestExecutionRun.ReportStatus.FAILED,
            execution_plan=execution_plan,
            resource_profile=self.resource,
            started_at=timezone.now(),
            storage_root_ref="admin-regression/run",
            errors=["预期失败"],
        )

        response = self.client.get(
            reverse("admin:test_platform_testexecutionrun_change", args=[run.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"', html=False)
        self.assertContains(response, 'name="_toggle_mark"', html=False)
        self.assertContains(response, "删除此记录")

    def test_generation_count_is_bounded_and_defaults_to_one(self):
        field = IntentImportForm().fields["generation_count"]

        self.assertEqual(field.min_value, 1)
        self.assertEqual(field.max_value, 10)
        self.assertEqual(field.initial, 1)

    def test_generation_count_is_enforced_by_the_service(self):
        with self.assertRaises(ValidationError):
            queue_design_generation(self.intent, count=11)

        self.intent.refresh_from_db()
        self.assertEqual(self.intent.status, models.TestWorkflow.Status.DRAFT)

    @patch("apps.test_platform.generation_service._launch_execution_worker")
    def test_approved_test_plan_can_create_multiple_execution_plans(
        self,
        launch_worker,
    ):
        source_plan = self.create_plan(
            title="可复用测试计划",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        first = queue_execution_plan_generation(
            source_plan,
            execution_input={"variables": {"account": "A"}},
        )
        first.status = models.ExecutionPlanArtifact.Status.REVIEW
        first.save(update_fields=("status", "updated_at"))

        second = queue_execution_plan_generation(
            source_plan,
            execution_input={"variables": {"account": "B"}},
        )

        self.assertEqual(source_plan.execution_plans.count(), 2)
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(first.execution_input["variables"]["account"], "A")
        self.assertEqual(second.execution_input["variables"]["account"], "B")
        self.assertEqual(launch_worker.call_count, 2)

    def test_bulk_actions_are_available_on_business_records(self):
        self.create_plan(
            title="批量操作待处理来源",
            status=models.TestPlanArtifact.Status.REVIEW,
            marked=False,
        )
        source_plan = self.create_plan(
            title="批量操作来源",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        execution_plan = self.create_execution_plan(source_plan)
        models.TestExecutionRun.objects.create(
            run_id="bulk-actions-run",
            status=models.TestExecutionRun.Status.FAILED,
            report_status=models.TestExecutionRun.ReportStatus.FAILED,
            execution_plan=execution_plan,
            resource_profile=self.resource,
            started_at=timezone.now(),
            storage_root_ref="admin-regression/bulk-actions-run",
        )
        expected_actions = {
            "testintentimport": {"mark_selected", "unmark_selected", "delete_selected"},
            "testplanartifact": {"mark_selected", "unmark_selected", "delete_selected"},
            "executionplanartifact": {
                "run_selected",
                "mark_selected",
                "unmark_selected",
                "delete_selected",
            },
            "testexecutionrun": {"mark_selected", "unmark_selected", "delete_selected"},
        }

        for model_name, actions in expected_actions.items():
            with self.subTest(model=model_name):
                response = self.client.get(
                    reverse(f"admin:test_platform_{model_name}_changelist")
                )
                content = response.content.decode()
                for action in actions:
                    self.assertIn(f'value="{action}"', content)

    @patch("apps.test_platform.execution_service.queue_execution_plan_artifact")
    def test_batch_run_queues_every_selected_approved_plan(self, queue_run):
        queue_run.return_value = SimpleNamespace(run_id="test-run")
        first_source = self.create_plan(
            title="批量运行来源一",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        second_source = self.create_plan(
            title="批量运行来源二",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        first = self.create_execution_plan(first_source)
        second = self.create_execution_plan(second_source)

        response = self.client.post(
            reverse("admin:test_platform_executionplanartifact_changelist"),
            {
                "action": "run_selected",
                "_selected_action": [str(first.pk), str(second.pk)],
                "select_across": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(queue_run.call_count, 2)

    @patch("apps.test_platform.execution_service.queue_execution_plan_artifact")
    @patch(
        "apps.test_platform.admin.ExecutionPlanArtifactAdmin.approve_execution_plan"
    )
    def test_pending_detail_run_refreshes_approved_bundle_before_queue(
        self,
        approve_plan,
        queue_run,
    ):
        source_plan = self.create_plan(
            title="待审批直接运行来源",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        execution_plan = self.create_execution_plan(source_plan)
        execution_plan.status = models.ExecutionPlanArtifact.Status.REVIEW
        execution_plan.approved_bundle = {}
        execution_plan.compilation_result = {
            "plan": {"flows": []},
            "validation": {"passed": True},
            "artifacts": [{"artifact": "ready"}],
        }
        execution_plan.save(
            update_fields=(
                "status",
                "approved_bundle",
                "compilation_result",
                "updated_at",
            )
        )
        fresh_bundle = {"plan": {"source": "fresh approval"}}

        def approve_now(request, queryset):
            queryset.update(
                status=models.ExecutionPlanArtifact.Status.APPROVED,
                approved_bundle=fresh_bundle,
            )

        approve_plan.side_effect = approve_now
        queue_run.return_value = SimpleNamespace(run_id="fresh-detail-run")

        response = self.client.post(
            reverse(
                "admin:test_platform_executionplanartifact_change",
                args=[execution_plan.pk],
            ),
            {
                "_approve_execution_plan": "运行",
                "review_comments": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        queued_artifact = queue_run.call_args.args[0]
        self.assertEqual(queued_artifact.approved_bundle, fresh_bundle)

    @patch("apps.test_platform.execution_service.queue_execution_plan_artifact")
    @patch(
        "apps.test_platform.admin.ExecutionPlanArtifactAdmin.approve_execution_plan"
    )
    def test_pending_batch_run_refreshes_approved_bundle_before_queue(
        self,
        approve_plan,
        queue_run,
    ):
        source_plan = self.create_plan(
            title="待审批批量运行来源",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        execution_plan = self.create_execution_plan(source_plan)
        execution_plan.status = models.ExecutionPlanArtifact.Status.REVIEW
        execution_plan.approved_bundle = {}
        execution_plan.save(
            update_fields=("status", "approved_bundle", "updated_at")
        )
        fresh_bundle = {"plan": {"source": "fresh batch approval"}}

        def approve_now(request, queryset):
            queryset.update(
                status=models.ExecutionPlanArtifact.Status.APPROVED,
                approved_bundle=fresh_bundle,
            )

        approve_plan.side_effect = approve_now
        queue_run.return_value = SimpleNamespace(run_id="fresh-batch-run")

        response = self.client.post(
            reverse("admin:test_platform_executionplanartifact_changelist"),
            {
                "action": "run_selected",
                "_selected_action": [str(execution_plan.pk)],
                "select_across": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        queued_artifact = queue_run.call_args.args[0]
        self.assertEqual(queued_artifact.approved_bundle, fresh_bundle)

    def test_detail_commands_match_each_layer(self):
        source_plan = self.create_plan(
            title="详情按钮来源",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        execution_plan = self.create_execution_plan(source_plan)
        run = models.TestExecutionRun.objects.create(
            run_id="detail-command-run",
            status=models.TestExecutionRun.Status.FAILED,
            report_status=models.TestExecutionRun.ReportStatus.FAILED,
            execution_plan=execution_plan,
            resource_profile=self.resource,
            started_at=timezone.now(),
            storage_root_ref="admin-regression/detail-command-run",
        )
        expectations = (
            (
                reverse(
                    "admin:test_platform_testintentimport_change",
                    args=[self.intent.pk],
                ),
                {"_generate_test_plan", "_toggle_mark"},
                {"_revise_test_plan", "_run_execution_plan"},
            ),
            (
                reverse(
                    "admin:test_platform_testplanartifact_change",
                    args=[source_plan.pk],
                ),
                {"_approve_test_plan", "_revise_test_plan", "_toggle_mark"},
                {"_run_execution_plan"},
            ),
            (
                reverse(
                    "admin:test_platform_executionplanartifact_change",
                    args=[execution_plan.pk],
                ),
                {"_run_execution_plan", "_revise_execution_plan", "_toggle_mark"},
                {"_approve_test_plan"},
            ),
            (
                reverse(
                    "admin:test_platform_testexecutionrun_change",
                    args=[run.pk],
                ),
                {"_toggle_mark"},
                {"_save", "_run_execution_plan", "_revise_execution_plan"},
            ),
        )

        for url, present, absent in expectations:
            with self.subTest(url=url):
                content = self.client.get(url).content.decode()
                for command in present:
                    self.assertIn(f'name="{command}"', content)
                for command in absent:
                    self.assertNotIn(f'name="{command}"', content)

    def test_execution_history_links_follow_business_layer_order(self):
        source_plan = self.create_plan(
            title="来源顺序测试计划",
            status=models.TestPlanArtifact.Status.APPROVED,
            marked=False,
        )
        execution_plan = self.create_execution_plan(source_plan)
        models.TestExecutionRun.objects.create(
            run_id="source-order-run",
            status=models.TestExecutionRun.Status.FAILED,
            report_status=models.TestExecutionRun.ReportStatus.FAILED,
            execution_plan=execution_plan,
            resource_profile=self.resource,
            started_at=timezone.now(),
            storage_root_ref="admin-regression/source-order-run",
        )

        content = self.client.get(
            reverse("admin:test_platform_testexecutionrun_changelist")
        ).content.decode()

        intent_position = content.index("<b>测试意图</b>")
        test_plan_position = content.index("<b>测试计划</b>")
        execution_plan_position = content.index("<b>执行计划</b>")
        self.assertLess(intent_position, test_plan_position)
        self.assertLess(test_plan_position, execution_plan_position)

    def test_workflow_home_keeps_business_layers_in_order(self):
        response = self.client.get(reverse("admin:index"))
        content = response.content.decode()

        intent_position = content.index("导入测试意图")
        test_plan_position = content.index("测试计划")
        execution_plan_position = content.index("执行计划")
        history_position = content.index("执行历史")

        self.assertLess(intent_position, test_plan_position)
        self.assertLess(test_plan_position, execution_plan_position)
        self.assertLess(execution_plan_position, history_position)
