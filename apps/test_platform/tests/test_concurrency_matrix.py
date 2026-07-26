from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, close_old_connections, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.test_platform.approval_service import (
    persist_execution_plan_review,
    persist_test_plan_approval,
)
from apps.test_platform.execution_service import (
    _canonical_resolved_path,
    queue_execution_plan_artifact,
)
from apps.test_platform.models import (
    ExecutionPlanArtifact,
    TestExecutionRun,
    TestPlanArtifact,
    TestResourceProfile,
    TestWorkflow,
)


class ConcurrentRequestMatrixTests(TransactionTestCase):
    """Exercise real database races instead of sequential stale-request calls."""

    reset_sequences = True

    def _profile(self, suffix: str, *, port: int) -> TestResourceProfile:
        return TestResourceProfile.objects.create(
            name=f"Concurrency resource {suffix}",
            port_host="127.0.0.1",
            port_number=port,
        )

    def _test_plan(
        self,
        profile: TestResourceProfile,
        suffix: str,
        *,
        status: str = TestPlanArtifact.Status.APPROVED,
        workflow: TestWorkflow | None = None,
    ) -> TestPlanArtifact:
        return TestPlanArtifact.objects.create(
            source_intent=workflow,
            resource_profile=profile,
            title=f"Concurrent test plan {suffix}",
            test_categories=["port"],
            design_id=f"DESIGN-CONCURRENCY-{suffix}",
            version=1,
            content_hash="sha256:" + sha256(suffix.encode("utf-8")).hexdigest(),
            status=status,
        )

    def _execution_plan(
        self,
        source: TestPlanArtifact,
        suffix: str,
        *,
        status: str = ExecutionPlanArtifact.Status.APPROVED,
    ) -> ExecutionPlanArtifact:
        return ExecutionPlanArtifact.objects.create(
            source_test_plan=source,
            resource_profile=source.resource_profile,
            title=f"Concurrent execution plan {suffix}",
            test_categories=["port"],
            plan_id=f"PLAN-CONCURRENCY-{suffix}",
            version=1,
            content_hash="sha256:" + sha256(suffix.encode("utf-8")).hexdigest(),
            artifact_root_ref=f"concurrency/{suffix.lower()}",
            status=status,
        )

    def _race(self, jobs: dict[str, object]) -> list[dict[str, object]]:
        """Start jobs together and retry only SQLite's transient write lock."""

        barrier = threading.Barrier(len(jobs))

        def invoke(label, callback):
            close_old_connections()
            lock_messages: list[str] = []
            try:
                barrier.wait(timeout=10)
                for attempt in range(60):
                    try:
                        value = callback()
                        return {
                            "label": label,
                            "outcome": "success",
                            "value": value,
                            "lock_messages": lock_messages,
                        }
                    except ValidationError as exc:
                        return {
                            "label": label,
                            "outcome": "validation_conflict",
                            "error": str(exc),
                            "lock_messages": lock_messages,
                        }
                    except IntegrityError as exc:
                        return {
                            "label": label,
                            "outcome": "integrity_conflict",
                            "error": str(exc),
                            "lock_messages": lock_messages,
                        }
                    except OperationalError as exc:
                        message = str(exc)
                        if "locked" not in message.lower():
                            return {
                                "label": label,
                                "outcome": "unexpected",
                                "error": repr(exc),
                                "lock_messages": lock_messages,
                            }
                        lock_messages.append(message)
                        # SQLite serializes writes.  Reopen this thread's
                        # connection before retrying so a transient table lock
                        # cannot masquerade as the expected business conflict.
                        connections["default"].close()
                        time.sleep(min(0.003 * (attempt + 1), 0.05))
                    except Exception as exc:  # pragma: no cover - diagnostic path
                        return {
                            "label": label,
                            "outcome": "unexpected",
                            "error": repr(exc),
                            "lock_messages": lock_messages,
                        }
                return {
                    "label": label,
                    "outcome": "lock_exhausted",
                    "error": lock_messages[-1] if lock_messages else "",
                    "lock_messages": lock_messages,
                }
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = [
                executor.submit(invoke, label, callback)
                for label, callback in jobs.items()
            ]
            outcomes = [future.result(timeout=20) for future in futures]

        for outcome in outcomes:
            if outcome["lock_messages"]:
                self.assertEqual(
                    connections["default"].vendor,
                    "sqlite",
                    f"非 SQLite 后端出现未解释的锁冲突: {outcome}",
                )
            self.assertNotIn(
                outcome["outcome"],
                {"unexpected", "lock_exhausted"},
                f"并发任务没有收敛到业务终态: {outcome}",
            )
        return outcomes

    def test_two_requests_concurrently_approve_one_test_plan_exactly_once(self):
        profile = self._profile("test-plan", port=19101)
        workflow = TestWorkflow.objects.create(
            title="Concurrent test-plan approval",
            resource_profile=profile,
            allowed_channels=["port"],
        )
        artifact = self._test_plan(
            profile,
            "TEST-PLAN-A",
            status=TestPlanArtifact.Status.REVIEW,
            workflow=workflow,
        )

        def approve():
            return persist_test_plan_approval(
                artifact.pk,
                expected_status=TestPlanArtifact.Status.REVIEW,
                review_payload={"decision": "approved"},
                approved_bundle={
                    "design": {"design_id": artifact.design_id}
                },
            ).pk

        outcomes = self._race(
            {
                "request-a": approve,
                "request-b": approve,
            }
        )

        successes = [item for item in outcomes if item["outcome"] == "success"]
        conflicts = [
            item
            for item in outcomes
            if item["outcome"] == "validation_conflict"
        ]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        self.assertIn("其他请求处理", conflicts[0]["error"])

        artifact.refresh_from_db()
        workflow.refresh_from_db()
        self.assertEqual(artifact.status, TestPlanArtifact.Status.APPROVED)
        self.assertEqual(artifact.review_payload["decision"], "approved")
        self.assertEqual(workflow.status, TestWorkflow.Status.DESIGN_APPROVED)

    def test_windows_extended_path_alias_is_normalized_before_containment_check(self):
        if os.name != "nt":
            self.skipTest("Windows extended-path aliases only exist on Windows")
        extended = Path(r"\\?\C:\safe-artifacts\plan-a")
        with patch.object(Path, "resolve", return_value=extended):
            normalized = _canonical_resolved_path(
                Path(r"C:\safe-artifacts\plan-a")
            )
        self.assertEqual(normalized, Path(r"C:\safe-artifacts\plan-a"))

    def test_two_requests_concurrently_review_one_execution_plan_consistently(self):
        profile = self._profile("execution-approval", port=19102)
        source = self._test_plan(profile, "EXECUTION-SOURCE-B")
        artifact = self._execution_plan(
            source,
            "EXECUTION-REVIEW-B",
            status=ExecutionPlanArtifact.Status.REVIEW,
        )

        def approve():
            return persist_execution_plan_review(
                artifact.pk,
                expected_status=ExecutionPlanArtifact.Status.REVIEW,
                review_payload={"decision": "approved"},
                approved_bundle={
                    "plan": {"plan_id": artifact.plan_id}
                },
            ).pk

        outcomes = self._race(
            {
                "request-a": approve,
                "request-b": approve,
            }
        )

        successes = [item for item in outcomes if item["outcome"] == "success"]
        conflicts = [
            item
            for item in outcomes
            if item["outcome"] == "validation_conflict"
        ]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        self.assertIn("其他请求处理", conflicts[0]["error"])

        artifact.refresh_from_db()
        self.assertEqual(artifact.status, ExecutionPlanArtifact.Status.APPROVED)
        self.assertEqual(artifact.review_payload["decision"], "approved")

    def test_same_execution_plan_concurrent_queue_allows_one_active_run(self):
        profile = self._profile("same-plan-queue", port=19103)
        source = self._test_plan(profile, "QUEUE-SOURCE-C")
        artifact = self._execution_plan(source, "QUEUE-PLAN-C")

        def queue():
            current = ExecutionPlanArtifact.objects.get(pk=artifact.pk)
            return queue_execution_plan_artifact(current).run_id

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.execution_service._validated_execution_inputs"
        ), patch("apps.test_platform.execution_service.subprocess.Popen") as popen:
            outcomes = self._race({"request-a": queue, "request-b": queue})

        successes = [item for item in outcomes if item["outcome"] == "success"]
        conflicts = [
            item
            for item in outcomes
            if item["outcome"] == "validation_conflict"
        ]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        self.assertIn("已有排队中或运行中", conflicts[0]["error"])
        active = TestExecutionRun.objects.filter(
            execution_plan=artifact,
            status__in=(
                TestExecutionRun.Status.QUEUED,
                TestExecutionRun.Status.RUNNING,
            ),
        )
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().run_id, successes[0]["value"])
        self.assertEqual(popen.call_count, 1)

    def test_active_run_constraint_covers_queued_and_running_race(self):
        profile = self._profile("queue-running-race", port=19104)
        source = self._test_plan(profile, "ACTIVE-SOURCE-D")
        artifact = self._execution_plan(source, "ACTIVE-PLAN-D")

        def create_active(run_id: str, status: str):
            return TestExecutionRun.objects.create(
                run_id=run_id,
                status=status,
                report_status=TestExecutionRun.ReportStatus.PENDING,
                execution_plan_id=artifact.pk,
                resource_profile_id=profile.pk,
                started_at=timezone.now(),
                storage_root_ref=artifact.artifact_root_ref,
            ).run_id

        outcomes = self._race(
            {
                "queued-request": lambda: create_active(
                    "RUN-QUEUED-RACE", TestExecutionRun.Status.QUEUED
                ),
                "running-worker": lambda: create_active(
                    "RUN-RUNNING-RACE", TestExecutionRun.Status.RUNNING
                ),
            }
        )

        successes = [item for item in outcomes if item["outcome"] == "success"]
        conflicts = [
            item
            for item in outcomes
            if item["outcome"] == "integrity_conflict"
        ]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        active = TestExecutionRun.objects.filter(
            execution_plan=artifact,
            status__in=(
                TestExecutionRun.Status.QUEUED,
                TestExecutionRun.Status.RUNNING,
            ),
        )
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().run_id, successes[0]["value"])

    def test_different_execution_plans_queue_independently_under_concurrency(self):
        first_profile = self._profile("independent-a", port=19105)
        second_profile = self._profile("independent-b", port=19106)
        first_artifact = self._execution_plan(
            self._test_plan(first_profile, "INDEPENDENT-SOURCE-E"),
            "INDEPENDENT-PLAN-E",
        )
        second_artifact = self._execution_plan(
            self._test_plan(second_profile, "INDEPENDENT-SOURCE-F"),
            "INDEPENDENT-PLAN-F",
        )

        def queue(artifact_id: int):
            current = ExecutionPlanArtifact.objects.get(pk=artifact_id)
            return queue_execution_plan_artifact(current).run_id

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.execution_service._validated_execution_inputs"
        ), patch("apps.test_platform.execution_service.subprocess.Popen") as popen:
            outcomes = self._race(
                {
                    "plan-a": lambda: queue(first_artifact.pk),
                    "plan-b": lambda: queue(second_artifact.pk),
                }
            )

        self.assertEqual(
            [item["outcome"] for item in outcomes].count("success"),
            2,
            outcomes,
        )
        self.assertEqual(
            TestExecutionRun.objects.filter(
                status=TestExecutionRun.Status.QUEUED
            ).count(),
            2,
        )
        self.assertEqual(
            set(
                TestExecutionRun.objects.values_list(
                    "execution_plan_id", flat=True
                )
            ),
            {first_artifact.pk, second_artifact.pk},
        )
        self.assertEqual(popen.call_count, 2)

    def test_failed_run_retry_gets_new_identity_without_overwriting_old_report(self):
        profile = self._profile("retry", port=19107)
        source = self._test_plan(profile, "RETRY-SOURCE-G")
        artifact = self._execution_plan(source, "RETRY-PLAN-G")
        old_run_id = "RUN-20260726-FAILED000001"
        old_report_hash = "sha256:" + "a" * 64
        old_report_paths = {
            "json": f"reports/{old_run_id}/report.json",
            "html": f"reports/{old_run_id}/report.html",
        }

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ):
            artifact_root = Path(directory) / artifact.artifact_root_ref
            old_report = artifact_root / old_report_paths["json"]
            old_report.parent.mkdir(parents=True)
            old_report.write_text('{"old": true}', encoding="utf-8")
            old_run = TestExecutionRun.objects.create(
                run_id=old_run_id,
                status=TestExecutionRun.Status.FAILED,
                report_status=TestExecutionRun.ReportStatus.AVAILABLE,
                execution_plan=artifact,
                resource_profile=profile,
                started_at=timezone.now(),
                finished_at=timezone.now(),
                storage_root_ref=artifact.artifact_root_ref,
                report_paths=old_report_paths,
                report_content_hash=old_report_hash,
                result_summary={"counts": {"flows": {"failed": 1}}},
                errors=["ASSERTION_FAILED"],
            )

            new_run_id = "RUN-20260726-RETRY000001"
            with patch(
                "apps.test_platform.execution_service._validated_execution_inputs"
            ), patch(
                "apps.test_platform.execution_service.generate_run_id",
                return_value=new_run_id,
            ), patch(
                "apps.test_platform.execution_service.subprocess.Popen"
            ) as popen:
                retry = queue_execution_plan_artifact(
                    ExecutionPlanArtifact.objects.get(pk=artifact.pk),
                )

            old_run.refresh_from_db()
            retry.refresh_from_db()
            self.assertEqual(retry.run_id, new_run_id)
            self.assertNotEqual(retry.run_id, old_run.run_id)
            self.assertEqual(retry.status, TestExecutionRun.Status.QUEUED)
            self.assertEqual(retry.report_status, TestExecutionRun.ReportStatus.PENDING)
            self.assertEqual(retry.report_paths, {})
            self.assertEqual(retry.report_content_hash, "")

            self.assertEqual(old_run.status, TestExecutionRun.Status.FAILED)
            self.assertEqual(old_run.report_status, TestExecutionRun.ReportStatus.AVAILABLE)
            self.assertEqual(old_run.report_paths, old_report_paths)
            self.assertEqual(old_run.report_content_hash, old_report_hash)
            self.assertEqual(old_run.errors, ["ASSERTION_FAILED"])
            self.assertEqual(old_report.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual(popen.call_count, 1)

        self.assertEqual(
            TestExecutionRun.objects.filter(execution_plan=artifact).count(),
            2,
        )
