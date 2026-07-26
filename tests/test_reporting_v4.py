from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from xml.etree import ElementTree as ET

from apps.test_platform.reporting import TestReportGenerator
from apps.test_platform.runners import ExecutionCoordinator
from apps.test_platform.runners.contracts import (
    ExecutionSummary,
    FlowRunResult,
    RunResult,
    RunStatus,
    RuntimeContext,
    StepResult,
)


def _hash(char: str = "1") -> str:
    return "sha256:" + char * 64


def _plan_flow(flow_id: str, status: str = "passed", *, name: str | None = None):
    stage = SimpleNamespace(
        stage_id="STAGE-0001",
        order=1,
        executor_kind="http_api",
        operation_ids=[f"OP-{flow_id}"],
        expected_result_ids=[f"EXP-{flow_id}"],
        setup_required_state_ids=[],
        data_ids=[],
    )
    flow = SimpleNamespace(
        flow_id=flow_id,
        scenario_id=f"SCN-{flow_id}",
        name=name or f"Scenario {flow_id}",
        requirement_ids=[f"REQ-{flow_id}"],
        techniques=["positive"],
        stages=[stage],
    )
    step = StepResult(
        step_id=f"STEP-{flow_id}",
        status=status,
        message=f"step {status}",
        duration_ms=2,
        details={
            "status_code": 200,
            "assertions": [
                {
                    "expected_result_id": f"EXP-{flow_id}",
                    "passed": status == "passed",
                    "message": f"assertion {status}",
                }
            ],
        },
        evidence=[f"{flow_id}.json"],
    )
    stage_result = RunResult(
        run_id="RUN-REPORT",
        executor_kind="http_api",
        flow_id=flow_id,
        stage_id="STAGE-0001",
        status=status,
        started_at="2026-07-19T00:00:00+00:00",
        finished_at="2026-07-19T00:00:00.010000+00:00",
        steps=[step],
        evidence=[f"{flow_id}.json"],
        errors=[f"flow {status}"] if status in {"failed", "error"} else [],
    )
    flow_result = FlowRunResult(
        flow_id=flow_id,
        status=status,
        started_at="2026-07-19T00:00:00+00:00",
        finished_at="2026-07-19T00:00:00.010000+00:00",
        stages=[stage_result],
        errors=list(stage_result.errors),
    )
    return flow, flow_result


def _plan(flows):
    return SimpleNamespace(
        plan_id="PLAN-REPORT",
        version=1,
        design_id="DESIGN-REPORT",
        design_version=1,
        design_content_hash=_hash("1"),
        design_input_content_hash=_hash("2"),
        catalog_id="CATALOG-REPORT",
        catalog_content_hash=_hash("3"),
        target_system_id="report-demo",
        target_environment="test",
        flows=flows,
    )


def _manifest(flow_ids):
    return {
        "schema_version": "run-manifest.v4",
        "run_id": "RUN-REPORT",
        "design_id": "DESIGN-REPORT",
        "design_version": 1,
        "design_content_hash": _hash("1"),
        "design_input_content_hash": _hash("2"),
        "plan_id": "PLAN-REPORT",
        "plan_version": 1,
        "plan_content_hash": _hash("4"),
        "validation_content_hash": _hash("5"),
        "review_content_hash": _hash("6"),
        "artifact_set_hash": _hash("7"),
        "status": "failed",
        "started_at": "2026-07-19T00:00:00+00:00",
        "finished_at": "2026-07-19T00:00:01+00:00",
        "artifacts": [
            {
                "flow_id": flow_id,
                "stage_id": "STAGE-0001",
                "executor_kind": "http_api",
                "artifact_refs": [
                    {
                        "kind": "payload",
                        "path_ref": "execution.json",
                        "sha256": _hash("8"),
                    }
                ],
            }
            for flow_id in flow_ids
        ],
        "flows": [],
        "stages": [],
        "errors": [],
    }


class ReportingV4Tests(unittest.TestCase):
    def test_report_paths_do_not_break_legacy_positional_summary_construction(self):
        summary = ExecutionSummary(
            "RUN-LEGACY-POSITIONAL",
            RunStatus.ERROR,
            [],
            [],
            "manifest.json",
            ["legacy error"],
        )

        self.assertEqual(summary.errors, ["legacy error"])
        self.assertEqual(summary.report_paths, {})

    def test_report_redacts_secrets_escapes_html_and_rejects_unsafe_refs(self):
        plan_flow, flow_result = _plan_flow(
            "FLOW-XSS",
            "failed",
            name='<script>alert("x")</script>',
        )
        secret = "TOP-SECRET-REPORT-VALUE"
        flow_result.stages[0].steps[0].message = f"token={secret}"
        flow_result.stages[0].steps[0].details["unsafe_detail"] = secret
        flow_result.stages[0].metadata = {
            "stage_order": 1,
            "unsafe_metadata": secret,
        }
        flow_result.stages[0].evidence = [
            "../escape.json",
            r"C:\private\raw.json",
            "safe.json",
        ]
        flow_result.cleanup = StepResult(
            step_id="cleanup:demo",
            status=RunStatus.FAILED,
            message=f"cleanup token={secret}",
            details={"cleanup_goal_id": "CLEANUP-1", "handler_kind": "http_api"},
        )
        summary = ExecutionSummary(
            run_id="RUN-REPORT",
            status=RunStatus.FAILED,
            flows=[flow_result],
            stages=list(flow_result.stages),
            errors=[f"authorization={secret}"],
        )
        manifest = _manifest(["FLOW-XSS"])
        manifest["artifacts"][0]["artifact_refs"].extend(
            [
                {"kind": "bad", "path_ref": "../../secret.txt", "sha256": _hash("9")},
                {"kind": "bad", "path_ref": "https://example.test/x", "sha256": _hash("9")},
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            evidence_file = evidence_dir / "safe.json"
            evidence_file.write_text("{}", encoding="utf-8")
            paths = TestReportGenerator().generate(
                summary=summary,
                artifact_root=root,
                context=RuntimeContext(
                    variables={"runtime": {"secret": secret}},
                    secret_variable_names={"runtime.secret"},
                    evidence_dir=evidence_dir,
                ),
                plan=_plan([plan_flow]),
                manifest=manifest,
                manifest_path=r"C:\private\manifest.json",
            )
            report_text = (root / paths.json).read_text(encoding="utf-8")
            html = (root / paths.html).read_text(encoding="utf-8")
            junit = ET.parse(root / paths.junit).getroot()
            evidence_link_resolves = (
                (root / paths.html).parent / "../../evidence/safe.json"
            ).resolve().is_file()

        self.assertNotIn(secret, report_text)
        self.assertNotIn("unsafe_detail", report_text)
        self.assertNotIn("unsafe_metadata", report_text)
        self.assertNotIn("../../secret.txt", report_text)
        self.assertNotIn("https://example.test/x", report_text)
        self.assertNotIn("C:\\private", report_text)
        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertNotIn("运行身份", html)
        self.assertNotIn("审计 Hash", html)
        self.assertIn("结果概览", html)
        self.assertIn('href="../../evidence/safe.json"', html)
        self.assertTrue(evidence_link_resolves)
        report = json.loads(report_text)
        self.assertEqual(report["manifest_path"], "manifest.json")
        self.assertEqual(report["flows"][0]["cleanup"]["status"], "failed")
        self.assertEqual(
            [item["path_ref"] for item in report["flows"][0]["stages"][0]["artifacts"]],
            ["execution.json"],
        )
        unhashed = dict(report)
        content_hash = unhashed.pop("report_content_hash")
        canonical = json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        self.assertEqual(
            content_hash,
            "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(junit.attrib["failures"], "1")
        self.assertEqual(junit.attrib["errors"], "0")
        self.assertEqual(junit.attrib["skipped"], "0")

    def test_junit_and_json_preserve_all_run_statuses(self):
        statuses = ["passed", "failed", "error", "blocked", "inconclusive", "dry_run"]
        pairs = [
            _plan_flow(f"FLOW-{index}", status)
            for index, status in enumerate(statuses, start=1)
        ]
        summary = ExecutionSummary(
            run_id="RUN-REPORT",
            status=RunStatus.ERROR,
            flows=[pair[1] for pair in pairs],
            stages=[stage for _, result in pairs for stage in result.stages],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = TestReportGenerator().generate(
                summary=summary,
                artifact_root=root,
                context=RuntimeContext(),
                plan=_plan([pair[0] for pair in pairs]),
                manifest=_manifest([pair[0].flow_id for pair in pairs]),
            )
            report = json.loads((root / paths.json).read_text(encoding="utf-8"))
            junit = ET.parse(root / paths.junit).getroot()

        for status in statuses:
            self.assertEqual(report["summary"]["flows"][status], 1)
        self.assertEqual(junit.attrib["tests"], "6")
        self.assertEqual(junit.attrib["failures"], "1")
        self.assertEqual(junit.attrib["errors"], "1")
        self.assertEqual(junit.attrib["skipped"], "3")

    def test_invalid_handoff_still_gets_minimal_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = ExecutionCoordinator().execute(
                {"invalid": True},
                root,
                RuntimeContext(),
                run_id="RUN-BLOCKED-REPORT",
            )
            report = json.loads(
                (root / summary.report_paths["json"]).read_text(encoding="utf-8")
            )
            junit = ET.parse(root / summary.report_paths["junit"]).getroot()

            self.assertEqual(summary.status, RunStatus.BLOCKED)
            self.assertEqual(set(summary.report_paths), {"root", "json", "html", "junit"})
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["summary"]["flows"]["total"], 0)
            self.assertIsNone(report["manifest_path"])
            self.assertTrue(any("run-manifest" in item for item in report["limitations"]))
            self.assertEqual(junit.attrib["tests"], "1")
            self.assertEqual(junit.attrib["skipped"], "1")


if __name__ == "__main__":
    unittest.main()
