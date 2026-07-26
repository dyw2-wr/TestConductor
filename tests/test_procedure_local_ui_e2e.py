"""Opt-in real Playwright execution against an auto_ui_test asset database.

Set ``TEST_CONDUCTOR_UI_E2E=1`` and ``AUTO_UI_TEST_ROOT``. The test starts only the
static mechanics page; TestConductor loads and executes the published Procedure
locally, without an auto_ui_test API or navigation service.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from urllib.request import urlopen

from apps.test_platform.planning.contracts import ExecutorArtifactBundle, ExecutorArtifactRef
from apps.test_platform.runners import ProcedureRunner
from apps.test_platform.runners.contracts import RunStatus, RuntimeContext
from apps.test_platform.ui_modules import UiModuleCatalog


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(
    os.getenv("TEST_CONDUCTOR_UI_E2E") == "1",
    "set TEST_CONDUCTOR_UI_E2E=1 to run the local Procedure E2E",
)
class ProcedureLocalUiE2ETests(unittest.TestCase):
    def test_test_conductor_executes_published_procedure_without_auto_ui_test_api(self):
        source_root = str(
            os.getenv("AUTO_UI_TEST_ROOT")
            or os.getenv("PROCEDURE_PLAYWRIGHT_ROOT")
            or ""
        ).strip()
        if not source_root:
            self.skipTest("AUTO_UI_TEST_ROOT is required for the opt-in E2E")
        auto_ui_test = Path(source_root).resolve()
        producer_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(producer_temporary.cleanup)
        library_root = Path(producer_temporary.name) / "libraries"
        producer_script = """
from pathlib import Path
import sys
from procedures.asset_library import ProcedureAssetLibrary
from procedures.catalog import ProcedureCatalog
ProcedureAssetLibrary(ProcedureCatalog(Path(sys.argv[1])), root=Path(sys.argv[2])).sync_site(sys.argv[3])
"""
        subprocess.run(
            [
                os.fspath(Path(os.sys.executable)),
                "-c",
                producer_script,
                os.fspath(
                    auto_ui_test
                    / "runtime_assets"
                    / "knowledge_base"
                    / "procedures"
                ),
                os.fspath(library_root),
                "127.0.0.1",
            ],
            cwd=auto_ui_test,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [
                        os.fspath(auto_ui_test / "code"),
                        os.fspath(auto_ui_test / "runtime_assets"),
                    ]
                ),
            },
            check=True,
            capture_output=True,
            text=True,
        )
        database = library_root / "127.0.0.1.sqlite"
        self.assertTrue(database.is_file(), "auto_ui_test producer did not publish SQLite")

        catalog = UiModuleCatalog.from_asset_database(database)
        procedure = catalog.get("local.mechanics.drag_pair", 1)
        page_url = "http://127.0.0.1:8765/static/test-pages/mechanics.html"
        process = subprocess.Popen(
            [
                os.fspath(Path(os.sys.executable)),
                "-m",
                "http.server",
                "8765",
                "--directory",
                os.fspath(auto_ui_test / "code" / "web_ui"),
            ],
            cwd=auto_ui_test,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self._wait_for(page_url)
            with tempfile.TemporaryDirectory() as temporary:
                artifact_root = Path(temporary)
                workbook = artifact_root / "case.xlsx"
                workbook.write_bytes(b"review workbook")
                identity = {
                    "artifact_id": "ARTIFACT-FLOW-E2E-STAGE-0001",
                    "artifact_schema_version": "procedure-stage-bundle.v4",
                    "plan_id": "PLAN-UI-E2E",
                    "plan_version": 1,
                    "design_id": "DESIGN-UI-E2E",
                    "design_version": 1,
                    "design_content_hash": "sha256:" + "1" * 64,
                    "design_input_content_hash": "sha256:" + "2" * 64,
                    "catalog_id": "CATALOG-UI-E2E",
                    "catalog_content_hash": "sha256:" + "3" * 64,
                    "plan_content_hash": "sha256:" + "4" * 64,
                    "executor_kind": "procedure_playwright",
                    "flow_id": "FLOW-E2E",
                    "stage_id": "STAGE-0001",
                }
                manifest = {
                    **identity,
                    "compiled_artifact_hashes": {"case.xlsx": _sha256(workbook)},
                    "library_id": catalog.library_id,
                    "library_hash": "sha256:" + catalog.library_hash,
                    "procedure_calls": [
                        {
                            "row_id": "ROW-0001",
                            "operation_ref": "ui.local.mechanics.drag_pair",
                            "procedure_id": procedure.procedure_id,
                            "procedure_version": procedure.version,
                            "procedure_fingerprint": "sha256:" + procedure.fingerprint,
                            "data_bindings": [
                                {
                                    "data_id": "DATA-1",
                                    "consumer_id": "ui.local.mechanics.drag_pair",
                                    "binding_ref": "binding.slider",
                                    "input_refs": {
                                        "input.slider_value": "runtime.slider_value"
                                    },
                                }
                            ],
                            "assertions": [],
                        }
                    ],
                }
                manifest_path = artifact_root / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                bundle = ExecutorArtifactBundle(
                    **identity,
                    manifest_path_ref="manifest.json",
                    artifact_refs=[
                        ExecutorArtifactRef(
                            kind="workbook",
                            path_ref="case.xlsx",
                            sha256=_sha256(workbook),
                        ),
                        ExecutorArtifactRef(
                            kind="manifest",
                            path_ref="manifest.json",
                            sha256=_sha256(manifest_path),
                        ),
                    ],
                )
                result = ProcedureRunner().run(
                    artifact_root,
                    bundle,
                    RuntimeContext(
                        variables={"runtime.slider_value": "60"},
                        procedure_asset_database=str(database),
                        procedure_library_id=catalog.library_id,
                        procedure_library_hash=catalog.library_hash,
                        evidence_dir=artifact_root / "evidence",
                    ),
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

        self.assertEqual(result.status, RunStatus.PASSED, result.errors)
        self.assertTrue(result.external_action_started)
        self.assertEqual(len(result.steps), 1)

    @staticmethod
    def _wait_for(url: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=1) as response:
                    if response.status < 500:
                        return
            except OSError:
                pass
            time.sleep(0.25)
        raise AssertionError(f"local page did not start: {url}")


if __name__ == "__main__":
    unittest.main()
