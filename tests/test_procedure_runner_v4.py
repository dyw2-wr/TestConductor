from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from apps.test_platform.runners.procedure import ProcedureRunner
from apps.test_platform.runners.contracts import RunStatus, RunnerError, RuntimeContext
from apps.test_platform.tests.ui_asset_fixture import build_asset_database


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(root: Path, library: dict, fingerprint: str):
    workbook = root / "case.xlsx"
    workbook.write_bytes(b"review workbook")
    identity = {
        "artifact_id": "ARTIFACT-FLOW-1-STAGE-1",
        "artifact_schema_version": "procedure-stage-bundle.v4",
        "plan_id": "PLAN-1",
        "plan_version": 1,
        "design_id": "DESIGN-1",
        "design_version": 1,
        "design_content_hash": "sha256:" + "1" * 64,
        "design_input_content_hash": "sha256:" + "2" * 64,
        "catalog_id": "CATALOG-1",
        "catalog_content_hash": "sha256:" + "3" * 64,
        "plan_content_hash": "sha256:" + "4" * 64,
        "executor_kind": "procedure_playwright",
        "flow_id": "FLOW-1",
        "stage_id": "STAGE-1",
    }
    manifest = {
        **identity,
        "compiled_artifact_hashes": {"case.xlsx": _sha(workbook)},
        "library_id": library["library_id"],
        "library_hash": "sha256:" + library["library_hash"],
        "procedure_calls": [
            {
                "row_id": "ROW-0001",
                "operation_ref": "ui.account.login",
                "procedure_id": "account.login",
                "procedure_version": 1,
                "procedure_fingerprint": "sha256:" + fingerprint,
                "data_bindings": [
                    {
                        "data_id": "DATA-1",
                        "consumer_id": "ui.account.login",
                        "binding_ref": "binding.account",
                        "input_refs": {"input.account": "runtime.account_id"},
                    }
                ],
                "assertions": [],
            }
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return {
        **identity,
        "manifest_path_ref": "manifest.json",
        "artifact_refs": [
            {"kind": "workbook", "path_ref": "case.xlsx", "sha256": _sha(workbook)},
            {
                "kind": "manifest",
                "path_ref": "manifest.json",
                "sha256": _sha(manifest_path),
            },
        ],
    }


class _Locator:
    def __init__(self, events, target):
        self.events = events
        self.target = target

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def fill(self, value):
        self.events.append(("fill", self.target, value))

    def click(self, **kwargs):
        self.events.append(("click", self.target))

    def wait_for(self, **kwargs):
        self.events.append(("check", self.target, kwargs["state"]))


class _Page:
    def __init__(self):
        self.url = ""
        self.events = []

    def goto(self, url, **kwargs):
        self.url = url
        self.events.append(("goto", url))

    def get_by_label(self, target, **kwargs):
        return _Locator(self.events, target)

    def get_by_placeholder(self, target, **kwargs):
        return _Locator(self.events, target)

    def get_by_role(self, role, **kwargs):
        return _Locator(self.events, kwargs.get("name"))

    def get_by_text(self, target, **kwargs):
        return _Locator(self.events, target)

    def locator(self, target):
        return _Locator(self.events, target)


class ProcedureRunnerV4Tests(unittest.TestCase):
    def test_executes_exact_procedure_from_asset_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "assets.sqlite"
            library = build_asset_database(database)
            fingerprint = library["rows"][0]["fingerprint"]
            bundle = _artifact(root, library, fingerprint)
            page = _Page()

            @contextmanager
            def session_factory(_headless):
                yield page

            result = ProcedureRunner(session_factory=session_factory).run(
                root,
                bundle,
                RuntimeContext(
                    variables={"runtime.account_id": "alice"},
                    procedure_asset_database=str(database.resolve()),
                    procedure_library_id=library["library_id"],
                    procedure_library_hash=library["library_hash"],
                ),
            )

            self.assertEqual(result.status, RunStatus.PASSED)
            self.assertEqual(
                page.events,
                [
                    ("goto", "https://account.example.test/login"),
                    ("fill", "Account", "alice"),
                    ("click", "Login"),
                    ("check", "Welcome", "visible"),
                    ("check", "Welcome", "visible"),
                ],
            )

    def test_blocks_changed_asset_fingerprint_before_browser_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "assets.sqlite"
            library = build_asset_database(database)
            bundle = _artifact(root, library, "0" * 64)

            with self.assertRaisesRegex(RunnerError, "沉淀资产指纹"):
                ProcedureRunner().preflight(
                    root,
                    bundle,
                    RuntimeContext(
                        variables={"runtime.account_id": "alice"},
                        procedure_asset_database=str(database.resolve()),
                    ),
                )

    def test_blocks_missing_runtime_parameter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "assets.sqlite"
            library = build_asset_database(database)
            bundle = _artifact(root, library, library["rows"][0]["fingerprint"])

            with self.assertRaisesRegex(RunnerError, "缺少运行时变量"):
                ProcedureRunner().preflight(
                    root,
                    bundle,
                    RuntimeContext(
                        procedure_asset_database=str(database.resolve()),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
