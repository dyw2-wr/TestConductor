from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from apps.test_platform.run_history import RunIdConflict
from apps.test_platform.runners.contracts import RunStatus, RuntimeContext
from apps.test_platform.runners.execution import ExecutionCoordinator


class _RecordingHistory:
    def __init__(self):
        self.begins = []
        self.finals = []

    def begin(self, **values):
        self.begins.append(values)

    def finalize(self, **values):
        self.finals.append(values)


class _ConflictingHistory(_RecordingHistory):
    def begin(self, **values):
        self.begins.append(values)
        raise RunIdConflict("already exists")


class RunHistoryContractV4Tests(unittest.TestCase):
    def test_blocked_handoff_is_finalized_with_report_and_batch_times(self):
        recorder = _RecordingHistory()
        coordinator = ExecutionCoordinator(run_history_recorder=recorder)

        with tempfile.TemporaryDirectory() as directory:
            summary = coordinator.execute(
                {"invalid": True},
                Path(directory),
                RuntimeContext(),
                run_id="RUN-HISTORY-BLOCKED",
            )
            self.assertTrue((Path(directory) / summary.report_paths["json"]).is_file())

        self.assertEqual(summary.status, RunStatus.BLOCKED)
        self.assertIsNotNone(summary.started_at)
        self.assertIsNotNone(summary.finished_at)
        self.assertEqual(len(recorder.begins), 1)
        self.assertEqual(len(recorder.finals), 1)
        self.assertIs(recorder.finals[0]["summary"], summary)

    def test_duplicate_run_id_is_blocked_before_report_or_finalize(self):
        recorder = _ConflictingHistory()
        coordinator = ExecutionCoordinator(run_history_recorder=recorder)

        with tempfile.TemporaryDirectory() as directory:
            summary = coordinator.execute(
                {"invalid": True},
                Path(directory),
                RuntimeContext(),
                run_id="RUN-HISTORY-DUPLICATE",
            )

        self.assertEqual(summary.status, RunStatus.BLOCKED)
        self.assertEqual(summary.report_paths, {})
        self.assertIn("RUN_ID_CONFLICT", summary.errors[0])
        self.assertEqual(len(recorder.begins), 1)
        self.assertEqual(recorder.finals, [])

    def test_unsafe_run_id_is_rejected_before_history_or_execution(self):
        recorder = _RecordingHistory()
        coordinator = ExecutionCoordinator(run_history_recorder=recorder)

        with self.assertRaisesRegex(ValueError, "run_id"):
            coordinator.execute(
                {"invalid": True},
                Path("."),
                RuntimeContext(),
                run_id="unsafe/run",
            )

        self.assertEqual(recorder.begins, [])
        self.assertEqual(recorder.finals, [])

        for unsafe in ("run-history", "RUN_", "CON"):
            with self.subTest(run_id=unsafe), self.assertRaisesRegex(
                ValueError,
                "run_id",
            ):
                coordinator.execute(
                    {"invalid": True},
                    Path("."),
                    RuntimeContext(),
                    run_id=unsafe,
                )


if __name__ == "__main__":
    unittest.main()
