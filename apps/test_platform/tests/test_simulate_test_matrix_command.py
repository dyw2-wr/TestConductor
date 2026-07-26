from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import tempfile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.test_platform.models import (
    ApprovedKnowledgeEntry,
    ExecutionPlanArtifact,
    TestExecutionRun,
    TestPlanArtifact,
    TestResourceProfile,
    TestWorkflow,
)


class SimulateTestMatrixCommandTests(TestCase):
    def test_refuses_nonempty_business_tables_without_append(self):
        TestWorkflow.objects.create(title="existing business row")

        with self.assertRaisesRegex(CommandError, "--append"):
            call_command("simulate_test_matrix")

        self.assertEqual(TestWorkflow.objects.count(), 1)
        self.assertEqual(TestResourceProfile.objects.count(), 0)

    def test_runs_three_databases_composite_and_expected_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with override_settings(
                MEDIA_ROOT=root / "uploads",
                TEST_PLATFORM_ARTIFACT_ROOT=root / "artifacts",
                TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY="",
                TEST_PLATFORM_RUNTIME_CONTEXT_JSON="",
            ):
                output = StringIO()
                call_command(
                    "simulate_test_matrix",
                    stdout=output,
                )
                result = json.loads(output.getvalue())
                summary_path = Path(result["summary_path"])
                summary = json.loads(summary_path.read_text(encoding="utf-8"))

                self.assertTrue(result["started_from_empty"])
                self.assertTrue(result["all_expectations_matched"])
                self.assertEqual(summary["external_model_calls"], 0)
                self.assertNotIn("ui", summary)
                self.assertEqual(len(summary["fixture_databases"]), 3)
                self.assertEqual(
                    {item["key"] for item in summary["fixture_databases"]},
                    {"identity", "commerce", "analytics"},
                )
                runtime_roots = {
                    Path(item["path"]).parent
                    for item in summary["fixture_databases"]
                }
                self.assertEqual(len(runtime_roots), 1)
                runtime_root = runtime_roots.pop()
                self.assertEqual(runtime_root.parent.name, "runtime")
                self.assertTrue(
                    all(
                        Path(item["path"]).is_file()
                        for item in summary["fixture_databases"]
                    )
                )

                matrix = {item["case_id"]: item for item in summary["matrix"]}
                self.assertEqual(
                    {
                        key: value["actual_status"]
                        for key, value in matrix.items()
                    },
                    {
                        "db-identity": "passed",
                        "db-commerce": "passed",
                        "db-analytics": "passed",
                        "api-only": "passed",
                        "performance-only": "passed",
                        "tcp-only": "passed",
                        "composite-commerce": "passed",
                        "expected-failure": "failed",
                        "failure-chain": "failed",
                    },
                )
                self.assertTrue(
                    all(item["reports_verified"] for item in matrix.values())
                )
                self.assertIn(
                    "不能从后台",
                    summary["runtime_limitations"][0],
                )
                self.assertTrue(
                    summary["expected_failure_verification"]["verified"]
                )
                self.assertEqual(
                    summary["expected_failure_verification"][
                        "approved_expected"
                    ],
                    "locked",
                )
                self.assertEqual(
                    summary["expected_failure_verification"][
                        "observed_fixture_value"
                    ],
                    "active",
                )
                self.assertTrue(
                    summary["failure_chain_verification"]["verified"]
                )
                self.assertEqual(
                    summary["failure_chain_verification"]["blocked_stage"][
                        "status"
                    ],
                    "blocked",
                )
                self.assertTrue(summary["matrix_complete"])

                self.assertEqual(ApprovedKnowledgeEntry.objects.count(), 3)
                self.assertGreaterEqual(TestResourceProfile.objects.count(), 4)
                self.assertEqual(
                    TestResourceProfile.objects.filter(enabled=True).count(),
                    0,
                )
                self.assertEqual(TestWorkflow.objects.count(), 9)
                self.assertEqual(TestPlanArtifact.objects.count(), 9)
                self.assertEqual(ExecutionPlanArtifact.objects.count(), 9)
                self.assertEqual(
                    ExecutionPlanArtifact.objects.filter(
                        status=ExecutionPlanArtifact.Status.SUPERSEDED,
                    ).count(),
                    9,
                )
                self.assertEqual(TestExecutionRun.objects.count(), 9)
                for run in TestExecutionRun.objects.select_related(
                    "execution_plan",
                    "resource_profile",
                ):
                    self.assertIsNotNone(run.execution_plan)
                    self.assertIsNotNone(run.resource_profile)
                    self.assertEqual(
                        run.report_status,
                        TestExecutionRun.ReportStatus.AVAILABLE,
                    )
                    artifact_root = (
                        root / "artifacts" / run.storage_root_ref
                    )
                    self.assertTrue(
                        all(
                            (
                                artifact_root
                                / run.report_paths[kind]
                            ).is_file()
                            for kind in ("json", "html", "junit")
                        )
                    )

    def test_append_uses_a_new_immutable_database_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with override_settings(
                MEDIA_ROOT=root / "uploads",
                TEST_PLATFORM_ARTIFACT_ROOT=root / "artifacts",
                TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY="",
                TEST_PLATFORM_RUNTIME_CONTEXT_JSON="",
            ):
                first_output = StringIO()
                call_command(
                    "simulate_test_matrix",
                    stdout=first_output,
                )
                first = json.loads(
                    Path(
                        json.loads(first_output.getvalue())["summary_path"]
                    ).read_text(encoding="utf-8")
                )
                second_output = StringIO()
                call_command(
                    "simulate_test_matrix",
                    append=True,
                    stdout=second_output,
                )
                second = json.loads(
                    Path(
                        json.loads(second_output.getvalue())["summary_path"]
                    ).read_text(encoding="utf-8")
                )

                first_paths = {
                    Path(item["path"])
                    for item in first["fixture_databases"]
                }
                second_paths = {
                    Path(item["path"])
                    for item in second["fixture_databases"]
                }
                self.assertFalse(second["started_from_empty"])
                self.assertTrue(first_paths.isdisjoint(second_paths))
                self.assertTrue(
                    all(path.is_file() for path in first_paths | second_paths)
                )
