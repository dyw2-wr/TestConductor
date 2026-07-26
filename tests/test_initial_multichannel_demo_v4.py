from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET

import pandas as pd

from examples.initial_multichannel_demo import run_demo


class InitialMultichannelDemoV4Tests(unittest.TestCase):
    def test_reviewed_intent_compiles_and_executes_all_initial_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_demo(root)

            self.assertEqual(result["design_status"], "approved")
            self.assertEqual(result["plan_status"], "approved")
            self.assertEqual(result["overall_status"], "blocked")
            by_executor = {
                item["executor_kind"]: item["status"]
                for item in result["flow_results"]
            }
            self.assertEqual(
                by_executor,
                {
                    "procedure_playwright": "blocked",
                    "http_api": "passed",
                    "database": "passed",
                    "performance": "passed",
                    "tcp_port": "passed",
                },
            )

            workbooks = list(root.glob("**/case.xlsx"))
            self.assertEqual(len(workbooks), 1)
            frame = pd.read_excel(workbooks[0], sheet_name="Case")
            self.assertEqual(
                list(frame.columns),
                [
                    "Test Case ID",
                    "Test Case Name",
                    "Test Step ID",
                    "UR",
                    "Action",
                    "Input Data",
                    "Check",
                ],
            )
            self.assertEqual(frame.iloc[0]["Check"], "页面显示 Demo ready")
            manifest = json.loads(
                workbooks[0].with_name("manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["workbook_schema"], "WorkbookV2")
            self.assertEqual(manifest["payload_format"], "xlsx")

            stage_manifests = list(
                (root / "generated-files").glob(
                    "*/plan-REQ-MULTI-INITIAL-V4/v1/**/manifest.json"
                )
            )
            self.assertEqual(len(stage_manifests), 5)
            for path in stage_manifests:
                stage_manifest = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn(stage_manifest["payload_format"], {"json", "xlsx"})
                self.assertIsInstance(stage_manifest["variable_refs"], list)
                for step in stage_manifest["traceability"]["steps"]:
                    self.assertIn("source", step)
                    self.assertIn("action", step)
                    self.assertIn("check", step)
                    self.assertTrue(
                        all(
                            isinstance(expected_id, str)
                            for expected_id in step["expected_results"]
                        )
                    )
                    self.assertIn("assertions", step)
                if stage_manifest["executor_kind"] == "performance":
                    performance_step = stage_manifest["traceability"]["steps"][0]
                    self.assertIsNone(performance_step["action"])
                    self.assertEqual(len(performance_step["sources"]), 1)

            port_payloads = list(root.glob("**/execution.json"))
            port_payload = next(
                path
                for path in port_payloads
                if json.loads(path.read_text(encoding="utf-8"))["executor_kind"]
                == "tcp_port"
            )
            self.assertNotIn("127.0.0.1", port_payload.read_text(encoding="utf-8"))
            port_evidence = next(
                path
                for path in (root / "evidence").glob("*.json")
                if '"probe_ref": "port.demo.service"'
                in path.read_text(encoding="utf-8")
            )
            self.assertNotIn("127.0.0.1", port_evidence.read_text(encoding="utf-8"))
            self.assertEqual(
                set(result["reports"]),
                {"root", "json", "html", "junit"},
            )
            for relative_path in result["reports"].values():
                self.assertTrue((root / relative_path).exists(), relative_path)
            report = json.loads(
                (root / result["reports"]["json"]).read_text(encoding="utf-8")
            )
            self.assertEqual(report["schema_version"], "test-run-report.v1")
            self.assertEqual(report["summary"]["flows"]["total"], 5)
            self.assertEqual(report["summary"]["flows"]["passed"], 4)
            self.assertEqual(report["summary"]["flows"]["blocked"], 1)
            self.assertEqual(report["summary"]["assertions"]["planned"], 5)
            self.assertEqual(report["summary"]["assertions"]["evaluated"], 4)
            self.assertEqual(report["summary"]["assertions"]["not_evaluated"], 1)
            self.assertTrue(any("沉淀资产库" in item for item in report["limitations"]))
            ui_artifacts = report["flows"][0]["stages"][0]["artifacts"]
            self.assertTrue(
                any(item["path_ref"] == "case.xlsx" for item in ui_artifacts)
            )
            html = (root / result["reports"]["html"]).read_text(encoding="utf-8")
            self.assertIn("智能测试平台测试报告", html)
            self.assertNotIn("127.0.0.1", html)
            junit = ET.parse(root / result["reports"]["junit"]).getroot()
            self.assertEqual(junit.attrib["tests"], "5")
            self.assertEqual(junit.attrib["failures"], "0")
            self.assertEqual(junit.attrib["errors"], "0")
            self.assertEqual(junit.attrib["skipped"], "1")
            self.assertTrue((root / "demo-result.json").is_file())


if __name__ == "__main__":
    unittest.main()
