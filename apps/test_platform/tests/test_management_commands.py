from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.test_platform.models import TestWorkflow


class WorkerManagementCommandTests(TestCase):
    def test_generate_design_command_reports_artifact_identity(self):
        output = StringIO()
        with patch(
            "apps.test_platform.management.commands.generate_test_design.generate_design_artifact",
            return_value=SimpleNamespace(artifact_id="TP-123"),
        ) as generate:
            call_command(
                "generate_test_design",
                workflow_id=7,
                stdout=output,
            )

        generate.assert_called_once_with(7)
        self.assertIn("TP-123", output.getvalue())

    def test_generate_design_command_marks_workflow_error_on_failure(self):
        workflow = TestWorkflow.objects.create(title="worker failure")
        with patch(
            "apps.test_platform.management.commands.generate_test_design.generate_design_artifact",
            side_effect=RuntimeError("model failed"),
        ):
            with self.assertRaisesRegex(CommandError, "model failed"):
                call_command("generate_test_design", workflow_id=workflow.pk)

        workflow.refresh_from_db()
        self.assertEqual(workflow.status, TestWorkflow.Status.ERROR)
        self.assertEqual(workflow.last_error, "model failed")
        self.assertEqual(workflow.generation_progress["phase"], "failed")
        self.assertEqual(workflow.generation_progress["message"], "测试计划生成失败")

    def test_generate_execution_plan_command_propagates_worker_failure(self):
        with patch(
            "apps.test_platform.management.commands.generate_execution_plan.generate_execution_plan_artifact",
            side_effect=RuntimeError("compile failed"),
        ) as generate:
            with self.assertRaisesRegex(CommandError, "compile failed"):
                call_command("generate_execution_plan", artifact_id=11)
        generate.assert_called_once_with(11)

    def test_rebind_execution_plan_command_reports_artifact_identity(self):
        output = StringIO()
        with patch(
            "apps.test_platform.management.commands.rebind_execution_plan.rebind_execution_plan_artifact",
            return_value=SimpleNamespace(artifact_id="EP-REBIND"),
        ) as rebind:
            call_command(
                "rebind_execution_plan",
                artifact_id=12,
                stdout=output,
            )
        rebind.assert_called_once_with(12)
        self.assertIn("EP-REBIND", output.getvalue())

    def test_run_command_loads_frozen_input_from_execution_plan(self):
        output = StringIO()
        summary = SimpleNamespace(
            run_id="RUN-COMMAND",
            status=SimpleNamespace(value="passed"),
        )
        with patch(
            "apps.test_platform.management.commands.run_test_plan.execute_execution_plan_artifact",
            return_value=summary,
        ) as execute:
            call_command(
                "run_test_plan",
                execution_plan_id=13,
                run_id="RUN-COMMAND",
                stdout=output,
            )

        execute.assert_called_once_with(
            13,
            run_id="RUN-COMMAND",
        )
        self.assertIn("RUN-COMMAND: passed", output.getvalue())
