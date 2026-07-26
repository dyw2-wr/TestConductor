from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.test_platform.execution_service import (
    _artifact_root,
    _validated_execution_inputs,
    execute_execution_plan_artifact,
    mark_run_error,
    queue_execution_plan_artifact,
)
from apps.test_platform.models import (
    ExecutionPlanArtifact,
    TestExecutionRun,
    TestPlanArtifact,
    TestResourceProfile,
)


class ExecutionQueueTests(TestCase):
    def _artifact(self, *, name="Queue port", execution_input=None):
        profile = TestResourceProfile.objects.create(
            name=name,
            port_host="127.0.0.1",
            port_number=9000,
        )
        test_plan = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Queue plan",
            test_categories=["port"],
            design_id=f"DESIGN-{profile.pk}",
            version=1,
            content_hash="sha256:" + "1" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=test_plan,
            resource_profile=profile,
            title="Queue execution",
            test_categories=["port"],
            plan_id=f"PLAN-{profile.pk}",
            version=1,
            content_hash="sha256:" + "2" * 64,
            artifact_root_ref=f"queue-plan-{profile.pk}",
            execution_input=execution_input or {},
            status=ExecutionPlanArtifact.Status.APPROVED,
        )
        return execution

    def test_queue_is_persisted_and_duplicate_active_run_is_rejected(self):
        profile = TestResourceProfile.objects.create(
            name="Queue port",
            port_host="127.0.0.1",
            port_number=9000,
        )
        test_plan = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Queue plan",
            test_categories=["port"],
            design_id="DESIGN-QUEUE",
            version=1,
            content_hash="sha256:" + "1" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=test_plan,
            resource_profile=profile,
            title="Queue execution",
            test_categories=["port"],
            plan_id="PLAN-QUEUE",
            version=1,
            content_hash="sha256:" + "2" * 64,
            artifact_root_ref="queue-plan",
            status=ExecutionPlanArtifact.Status.APPROVED,
        )

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.execution_service._validated_execution_inputs"
        ), patch("apps.test_platform.execution_service.subprocess.Popen") as popen:
            run = queue_execution_plan_artifact(execution)
            with self.assertRaisesRegex(ValidationError, "已有排队中或运行中"):
                queue_execution_plan_artifact(execution)

        run.refresh_from_db()
        self.assertEqual(run.status, TestExecutionRun.Status.QUEUED)
        self.assertEqual(run.execution_plan, execution)
        command = popen.call_args.args[0]
        self.assertIn("run_test_plan", command)
        self.assertIn(run.run_id, command)
        self.assertNotIn("token", " ".join(command).lower())

    def test_queue_uses_input_frozen_on_execution_plan(self):
        profile = TestResourceProfile.objects.create(
            name="Runtime input port",
            port_host="127.0.0.1",
            port_number=9000,
        )
        test_plan = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Runtime input plan",
            test_categories=["port"],
            design_id="DESIGN-RUNTIME-INPUT",
            version=1,
            content_hash="sha256:" + "4" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        execution = ExecutionPlanArtifact.objects.create(
            source_test_plan=test_plan,
            resource_profile=profile,
            title="Runtime input execution",
            test_categories=["port"],
            plan_id="PLAN-RUNTIME-INPUT",
            version=1,
            content_hash="sha256:" + "5" * 64,
            artifact_root_ref="runtime-input-plan",
            execution_input={
                "schema_version": "test-runtime-input.v1",
                "variables": {"account_id": "ACCOUNT-1"},
                "performance_mode": "live",
            },
            status=ExecutionPlanArtifact.Status.APPROVED,
        )

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.execution_service._validated_execution_inputs"
        ), patch("apps.test_platform.execution_service.subprocess.Popen") as popen:
            run = queue_execution_plan_artifact(execution)

        run.refresh_from_db()
        self.assertNotIn(
            "TEST_PLATFORM_EXECUTION_INPUT_JSON",
            popen.call_args.kwargs["env"],
        )
        self.assertEqual(execution.execution_input["variables"], {"account_id": "ACCOUNT-1"})
        self.assertNotIn("ACCOUNT-1", " ".join(popen.call_args.args[0]))

    def test_queue_rejects_secret_runtime_values_before_creating_a_run(self):
        execution = self._artifact(
            name="Secret runtime input",
            execution_input={"variables": {"api_token": "should-never-be-persisted"}},
        )
        with self.assertRaisesRegex(ValidationError, "不能包含秘密值"):
            queue_execution_plan_artifact(execution)
        self.assertFalse(TestExecutionRun.objects.exists())

    def test_worker_start_failure_marks_the_queued_run_terminal(self):
        execution = self._artifact(name="Failed worker start")
        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.execution_service._validated_execution_inputs"
        ), patch(
            "apps.test_platform.execution_service.subprocess.Popen",
            side_effect=OSError("worker unavailable"),
        ):
            with self.assertRaisesRegex(ValidationError, "后台执行进程启动失败"):
                queue_execution_plan_artifact(execution)

        run = TestExecutionRun.objects.get(execution_plan=execution)
        execution.refresh_from_db()
        self.assertEqual(run.status, TestExecutionRun.Status.ERROR)
        self.assertEqual(run.report_status, TestExecutionRun.ReportStatus.FAILED)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.errors, ["EXECUTION_WORKER_FAILED"])
        self.assertIn("worker unavailable", execution.last_error)

    def test_mark_run_error_does_not_downgrade_an_existing_terminal_run(self):
        execution = self._artifact(name="Terminal run")
        run = TestExecutionRun.objects.create(
            run_id="RUN-TERMINAL",
            status=TestExecutionRun.Status.PASSED,
            report_status=TestExecutionRun.ReportStatus.AVAILABLE,
            execution_plan=execution,
            resource_profile=execution.resource_profile,
            started_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
            storage_root_ref=execution.artifact_root_ref,
        )

        mark_run_error(run.run_id, "late worker exception")

        run.refresh_from_db()
        execution.refresh_from_db()
        self.assertEqual(run.status, TestExecutionRun.Status.PASSED)
        self.assertEqual(run.report_status, TestExecutionRun.ReportStatus.AVAILABLE)
        self.assertEqual(execution.last_error, "")

    def test_synchronous_worker_attaches_run_identity_and_updates_last_error(self):
        execution = self._artifact(name="Synchronous execution")
        execution.execution_input = {"variables": {"account": "A-1"}}
        execution.save(update_fields=("execution_input", "updated_at"))
        run = TestExecutionRun.objects.create(
            run_id="RUN-SYNC",
            status=TestExecutionRun.Status.RUNNING,
            report_status=TestExecutionRun.ReportStatus.PENDING,
            started_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
            storage_root_ref=execution.artifact_root_ref,
        )
        summary = SimpleNamespace(run_id=run.run_id, status="failed", errors=["boom"])
        workflow = SimpleNamespace(execute=lambda *args, **kwargs: summary)
        resources = SimpleNamespace(runtime_config={"network_hosts": {}})

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.execution_service._validated_execution_inputs",
            return_value=(SimpleNamespace(), resources),
        ), patch(
            "apps.test_platform.execution_service.get_runtime_context",
            return_value=SimpleNamespace(),
        ), patch(
            "apps.test_platform.execution_service.get_workflow",
            return_value=workflow,
        ):
            returned = execute_execution_plan_artifact(
                execution.pk,
                run_id=run.run_id,
            )

        self.assertIs(returned, summary)
        run.refresh_from_db()
        execution.refresh_from_db()
        self.assertEqual(run.execution_plan, execution)
        self.assertEqual(run.resource_profile, execution.resource_profile)
        self.assertEqual(execution.last_error, "boom")

    def test_execution_artifact_root_cannot_escape_platform_storage(self):
        execution = self._artifact(name="Escaping artifact root")
        execution.artifact_root_ref = "../outside"
        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ):
            with self.assertRaisesRegex(ValidationError, "超出平台存储范围"):
                _artifact_root(execution)

    def test_execution_input_validation_detects_catalog_and_runtime_drift(self):
        execution = self._artifact(name="Drift validation")
        execution.runtime_config_hash = "sha256:" + "3" * 64
        execution.catalog_snapshot = {"content_hash": "frozen"}
        execution.approved_bundle = {"plan": "approved"}
        execution.source_test_plan.approved_bundle = {"design": "approved"}
        execution.source_test_plan.save(update_fields=("approved_bundle", "updated_at"))
        frozen = SimpleNamespace(content_hash="catalog-hash")
        current = SimpleNamespace(
            catalog=SimpleNamespace(content_hash="catalog-hash"),
            runtime_config_hash=execution.runtime_config_hash,
        )
        with patch(
            "apps.test_platform.execution_service.ApprovedTestPlanBundle.model_validate",
            return_value=SimpleNamespace(plan_id="PLAN"),
        ), patch(
            "apps.test_platform.execution_service.ApprovedTestDesignBundle.model_validate",
            return_value=SimpleNamespace(design_id="DESIGN"),
        ), patch(
            "apps.test_platform.execution_service.PlanningCatalogSnapshot.model_validate",
            return_value=frozen,
        ), patch(
            "apps.test_platform.execution_service.resolve_test_resources",
            return_value=current,
        ):
            approved, resources = _validated_execution_inputs(execution)
            self.assertEqual(approved.plan_id, "PLAN")
            self.assertIs(resources, current)

            current.catalog.content_hash = "changed"
            with self.assertRaisesRegex(ValidationError, "测试能力已经变化"):
                _validated_execution_inputs(execution)
            current.catalog.content_hash = "catalog-hash"
            current.runtime_config_hash = "changed"
            with self.assertRaisesRegex(ValidationError, "运行地址、查询或负载配置已经变化"):
                _validated_execution_inputs(execution)
