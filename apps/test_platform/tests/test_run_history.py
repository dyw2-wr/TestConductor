from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.test_platform.models import TestExecutionRun
from apps.test_platform.reporting import TestReportGenerator
from apps.test_platform.run_history import (
    DjangoRunHistoryRecorder,
    RunIdConflict,
    _category_result_counts,
    generate_run_id,
)
from apps.test_platform.runners.contracts import (
    ExecutionSummary,
    RunStatus,
    RuntimeContext,
)
from apps.test_platform.runners.execution import ExecutionCoordinator
from apps.test_platform.workflow import IntentToExecutionWorkflow


class _FailingReporter:
    def generate(self, **kwargs):
        raise OSError("report storage unavailable")


class _FailingFinalizeRecorder(DjangoRunHistoryRecorder):
    def finalize(self, **kwargs):
        raise OSError("database finalize unavailable")


class DjangoRunHistoryTests(TestCase):
    def test_generated_run_id_starts_with_local_calendar_date(self):
        with patch(
            "apps.test_platform.run_history.timezone.localdate",
            return_value=date(2026, 7, 23),
        ), patch(
            "apps.test_platform.run_history.uuid4",
            return_value=SimpleNamespace(hex="abcdef0123456789"),
        ):
            self.assertEqual(generate_run_id(), "RUN-20260723-ABCDEF012345")

    def test_worker_begin_claims_precreated_queued_record(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            root = storage / "queued"
            root.mkdir()
            record = TestExecutionRun.objects.create(
                run_id="RUN-QUEUED-CLAIM",
                status=TestExecutionRun.Status.QUEUED,
                started_at=datetime.now(timezone.utc),
                storage_root_ref="queued",
            )
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=storage):
                DjangoRunHistoryRecorder().begin(
                    run_id=record.run_id,
                    started_at=datetime.now(timezone.utc).isoformat(),
                    artifact_root=root,
                )

        record.refresh_from_db()
        self.assertEqual(record.status, TestExecutionRun.Status.RUNNING)

    def test_finalize_failure_marks_run_error_instead_of_leaving_running(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            root = storage / "finalize-failure"
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=storage):
                summary = ExecutionCoordinator(
                    run_history_recorder=_FailingFinalizeRecorder()
                ).execute(
                    {"invalid": True},
                    root,
                    RuntimeContext(),
                    run_id="RUN-FINALIZE-FAILURE",
                )

        record = TestExecutionRun.objects.get(run_id=summary.run_id)
        self.assertEqual(record.status, TestExecutionRun.Status.ERROR)
        self.assertEqual(record.errors, ["RUN_HISTORY_FINALIZE_FAILED"])
    def test_category_result_counts_group_compound_stage_results(self):
        result = _category_result_counts(
            {
                "flows": [
                    {
                        "stages": [
                            {"executor_kind": "http_api", "status": "passed"},
                            {"executor_kind": "tcp_port", "status": "failed"},
                            {"executor_kind": "http_api", "status": "blocked"},
                        ]
                    }
                ]
            }
        )

        self.assertEqual(result["api"]["total"], 2)
        self.assertEqual(result["api"]["passed"], 1)
        self.assertEqual(result["api"]["blocked"], 1)
        self.assertEqual(result["port"]["failed"], 1)

        fallback = _category_result_counts(
            {},
            SimpleNamespace(
                stages=[
                    SimpleNamespace(executor_kind="database", status="passed")
                ]
            ),
        )
        self.assertEqual(fallback["database"]["passed"], 1)

    def test_public_workflow_enables_django_history_by_default(self):
        workflow = IntentToExecutionWorkflow(
            SimpleNamespace(),
            SimpleNamespace(compiler=SimpleNamespace()),
        )

        self.assertIsInstance(
            workflow.coordinator.run_history_recorder,
            DjangoRunHistoryRecorder,
        )

    def test_coordinator_persists_early_blocked_run_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            root = storage / "coordinator-blocked"
            context = RuntimeContext(evidence_dir=root / "evidence")
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=storage):
                summary = ExecutionCoordinator(
                    run_history_recorder=DjangoRunHistoryRecorder()
                ).execute(
                    {"invalid": True},
                    root,
                    context,
                    run_id="RUN-DB-COORDINATOR-BLOCKED",
                )

            record = TestExecutionRun.objects.get(run_id=summary.run_id)
            self.assertEqual(summary.status, RunStatus.BLOCKED)
            self.assertEqual(record.status, TestExecutionRun.Status.BLOCKED)
            self.assertEqual(
                record.report_status,
                TestExecutionRun.ReportStatus.AVAILABLE,
            )
            self.assertIsNotNone(record.finished_at)
            self.assertTrue((root / record.report_paths["json"]).is_file())

    def test_report_failure_still_finalizes_database_record(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            root = storage / "report-failure"
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=storage):
                summary = ExecutionCoordinator(
                    reporter=_FailingReporter(),
                    run_history_recorder=DjangoRunHistoryRecorder(),
                ).execute(
                    {"invalid": True},
                    root,
                    RuntimeContext(),
                    run_id="RUN-DB-REPORT-FAILED",
                )

            record = TestExecutionRun.objects.get(run_id=summary.run_id)
            self.assertEqual(record.status, TestExecutionRun.Status.BLOCKED)
            self.assertEqual(
                record.report_status,
                TestExecutionRun.ReportStatus.FAILED,
            )
            self.assertEqual(record.report_paths, {})
            self.assertEqual(record.result_summary["counts"]["flows"]["total"], 0)
            self.assertIn("REPORT_WRITE_FAILED", record.errors)

    def test_begin_and_finalize_persist_report_index_and_redacted_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            root = storage / "batch-one"
            root.mkdir()
            secret = "RUN-HISTORY-SECRET-VALUE"
            context = RuntimeContext(
                variables={"runtime": {"secret": secret}},
                secret_variable_names={"runtime.secret"},
                evidence_dir=root / "evidence",
            )
            started = datetime.now(timezone.utc).isoformat()
            finished = datetime.now(timezone.utc).isoformat()
            summary = ExecutionSummary(
                run_id="RUN-DB-HISTORY-001",
                status=RunStatus.BLOCKED,
                errors=[f"PLAN_HANDOFF_INVALID: token={secret}"],
                started_at=started,
                finished_at=finished,
            )
            plan = SimpleNamespace(
                design_id="design-001",
                design_version=1,
                plan_id="plan-001",
                version=1,
                target_system_id="demo-system",
                target_environment="test",
                flows=[],
                content_hash=lambda: "sha256:" + "1" * 64,
            )
            paths = TestReportGenerator().generate(
                summary=summary,
                artifact_root=root,
                context=context,
                plan=plan,
            )
            summary.report_paths = paths.as_dict()

            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=storage):
                recorder = DjangoRunHistoryRecorder()
                recorder.begin(
                    run_id=summary.run_id,
                    started_at=started,
                    artifact_root=root,
                )
                recorder.finalize(
                    summary=summary,
                    plan=plan,
                    artifact_root=root,
                    context=context,
                )

            record = TestExecutionRun.objects.get(run_id=summary.run_id)
            self.assertEqual(record.status, TestExecutionRun.Status.BLOCKED)
            self.assertEqual(
                record.report_status,
                TestExecutionRun.ReportStatus.AVAILABLE,
            )
            self.assertEqual(record.storage_root_ref, "batch-one")
            self.assertEqual(record.plan_id, "plan-001")
            self.assertEqual(record.target_system_id, "demo-system")
            self.assertEqual(
                set(record.report_paths),
                {"root", "json", "html", "junit"},
            )
            self.assertTrue(record.report_content_hash.startswith("sha256:"))
            self.assertIn("flows", record.result_summary["counts"])
            self.assertEqual(record.errors, ["PLAN_HANDOFF_INVALID"])
            self.assertNotIn(
                secret,
                json.dumps(
                    {
                        "errors": record.errors,
                        "result_summary": record.result_summary,
                        "report_paths": record.report_paths,
                    },
                    ensure_ascii=False,
                ),
            )

    def test_duplicate_run_id_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            root = storage / "batch-two"
            root.mkdir()
            started = datetime.now(timezone.utc).isoformat()
            with override_settings(TEST_PLATFORM_ARTIFACT_ROOT=storage):
                recorder = DjangoRunHistoryRecorder()
                recorder.begin(
                    run_id="RUN-DB-HISTORY-DUPLICATE",
                    started_at=started,
                    artifact_root=root,
                )
                with self.assertRaises(RunIdConflict):
                    recorder.begin(
                        run_id="RUN-DB-HISTORY-DUPLICATE",
                        started_at=started,
                        artifact_root=root,
                    )

        self.assertEqual(
            TestExecutionRun.objects.filter(
                run_id="RUN-DB-HISTORY-DUPLICATE"
            ).count(),
            1,
        )
