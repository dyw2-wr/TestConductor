from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest

from apps.test_platform.runners import (
    DatabaseRunner,
    ExecutionCoordinator,
    HttpRunner,
    RunnerRegistry,
)
from apps.test_platform.runners.contracts import (
    CleanupResult,
    ReadOnlyDatabaseConnection,
    RunStatus,
    RuntimeContext,
)
from tests.test_runner_flow_v4 import _approved_bundle, _write_artifact


class _Transport:
    def __init__(self, statuses: list[int]):
        self.statuses = list(statuses)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        status = self.statuses.pop(0)
        return {
            "status_code": status,
            "headers": {},
            "json": {"ok": 200 <= status < 300},
        }


class RunnerHardeningV4Tests(unittest.TestCase):
    def test_http_json_path_assertion_reads_nested_array_value(self):
        class JsonTransport:
            def request(self, method, url, **kwargs):
                return {
                    "status_code": 200,
                    "headers": {"Content-Type": "application/json"},
                    "json": {"items": [{"state": "ready"}]},
                }

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            payload = {
                "schema_version": "http-execution-plan.v4",
                "executor_kind": "http_api",
                "flow_id": "FLOW-JSON",
                "stage_id": "STAGE-JSON",
                "base_url_ref": "api.main",
                "requests": [
                    {
                        "request_id": "HTTP-JSON-1",
                        "source": {"source_kind": "operation", "source_id": "OP-1"},
                        "method": "GET",
                        "path": "/items",
                        "assertions": [
                            {
                                "expected_result_id": "EXP-1",
                                "kind": "json",
                                "path": "$.items[0].state",
                                "operator": "equals",
                                "expected": "ready",
                            }
                        ],
                    }
                ],
            }
            artifact = _write_artifact(
                root / "json",
                executor_kind="http_api",
                artifact_schema_version="http-execution-plan.v4",
                flow_id="FLOW-JSON",
                stage_id="STAGE-JSON",
                payload=payload,
            )
            result = HttpRunner(transport=JsonTransport()).run(
                root / "json",
                artifact,
                RuntimeContext(base_urls={"api.main": "https://example.test"}),
            )

        self.assertEqual(result.status, RunStatus.PASSED)
        self.assertTrue(result.steps[0].details["assertions"][0]["passed"])

    def test_http_header_injection_is_blocked_before_transport(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            payload = {
                "schema_version": "http-execution-plan.v4",
                "executor_kind": "http_api",
                "flow_id": "FLOW-HEADER",
                "stage_id": "STAGE-HEADER",
                "base_url_ref": "api.main",
                "requests": [
                    {
                        "request_id": "HTTP-HEADER-1",
                        "source": {"source_kind": "operation", "source_id": "OP-1"},
                        "method": "GET",
                        "path": "/health",
                        "headers_ref": "request_headers",
                        "assertions": [],
                    }
                ],
            }
            artifact = _write_artifact(
                root / "headers",
                executor_kind="http_api",
                artifact_schema_version="http-execution-plan.v4",
                flow_id="FLOW-HEADER",
                stage_id="STAGE-HEADER",
                payload=payload,
            )
            transport = _Transport([200])
            result = HttpRunner(transport=transport).run(
                root / "headers",
                artifact,
                RuntimeContext(
                    base_urls={"api.main": "https://example.test"},
                    variables={"request_headers": {"X-Test": "ok\r\nInjected: yes"}},
                ),
            )

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertIn("非法 header value", result.errors[0])
        self.assertEqual(transport.calls, [])

    def test_payload_identity_mismatch_blocks_before_transport(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            payload = {
                "schema_version": "http-execution-plan.v4",
                "executor_kind": "database",
                "flow_id": "FLOW-WRONG",
                "stage_id": "STAGE-WRONG",
                "base_url_ref": "api.main",
                "requests": [
                    {
                        "request_id": "HTTP-0001",
                        "method": "GET",
                        "path": "/health",
                        "assertions": [],
                    }
                ],
            }
            artifact = _write_artifact(
                root / "http",
                executor_kind="http_api",
                artifact_schema_version="http-execution-plan.v4",
                flow_id="FLOW-HTTP",
                stage_id="STAGE-HTTP",
                payload=payload,
            )
            transport = _Transport([200])
            result = HttpRunner(transport=transport).run(
                root / "http",
                artifact,
                RuntimeContext(base_urls={"api.main": "https://example.test"}),
            )

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertTrue(any("payload.executor_kind" in item for item in result.errors))
        self.assertEqual(transport.calls, [])

    def test_required_state_http_setup_requires_2xx(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            payload = {
                "schema_version": "http-execution-plan.v4",
                "executor_kind": "http_api",
                "flow_id": "FLOW-SETUP",
                "stage_id": "STAGE-SETUP",
                "base_url_ref": "api.main",
                "requests": [
                    {
                        "request_id": "HTTP-SETUP-0001",
                        "source": {
                            "source_kind": "required_state",
                            "source_id": "REQUIRED-STATE-0001",
                        },
                        "method": "POST",
                        "path": "/test-support/setup",
                        "assertions": [],
                    }
                ],
            }
            artifact = _write_artifact(
                root / "setup",
                executor_kind="http_api",
                artifact_schema_version="http-execution-plan.v4",
                flow_id="FLOW-SETUP",
                stage_id="STAGE-SETUP",
                payload=payload,
            )
            transport = _Transport([500])
            result = HttpRunner(transport=transport).run(
                root / "setup",
                artifact,
                RuntimeContext(base_urls={"api.main": "https://example.test"}),
            )

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertTrue(result.external_action_started)
        self.assertEqual(result.steps[0].status, RunStatus.FAILED)
        self.assertIn("2xx", result.steps[0].details["assertions"][0]["message"])

    def test_data_guarantee_requires_exact_runtime_attestation(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            bundle = _approved_bundle(root, ["http_api"], data_guarantee=True)
            blocked_transport = _Transport([200])
            blocked = ExecutionCoordinator(
                RunnerRegistry(http=HttpRunner(transport=blocked_transport))
            ).execute(
                bundle,
                root,
                RuntimeContext(base_urls={"api.main": "https://example.test"}),
            )
            passing_transport = _Transport([200])
            passed = ExecutionCoordinator(
                RunnerRegistry(http=HttpRunner(transport=passing_transport))
            ).execute(
                bundle,
                root,
                RuntimeContext(
                    base_urls={"api.main": "https://example.test"},
                    data_guarantees={"REQUIRED-STATE-0001": "DATA-ACCOUNT"},
                ),
            )

        self.assertEqual(blocked.status, RunStatus.BLOCKED)
        self.assertTrue(any("DATA_GUARANTEE_MISSING" in item for item in blocked.errors))
        self.assertEqual(blocked_transport.calls, [])
        self.assertEqual(passed.status, RunStatus.PASSED)
        self.assertEqual(len(passing_transport.calls), 1)

    def test_performance_dry_run_never_calls_cleanup(self):
        cleanup_calls: list[str] = []

        def cleanup(*, body_ref):
            cleanup_calls.append(body_ref)
            return CleanupResult(success=True)

        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            bundle = _approved_bundle(root, ["performance"], cleanup=True)
            summary = ExecutionCoordinator().execute(
                bundle,
                root,
                RuntimeContext(
                    variables={"runtime": {"account_id": "ACCOUNT-1"}},
                    cleanup_hooks={"cleanup.account.restore": cleanup},
                    performance_profiles={"perf.smoke": {}},
                    performance_mode="dry_run",
                    evidence_dir=root / "evidence",
                ),
            )

        self.assertEqual(summary.stages[0].status, RunStatus.DRY_RUN)
        self.assertFalse(summary.stages[0].external_action_started)
        self.assertEqual(cleanup_calls, [])
        self.assertIsNone(summary.flows[0].cleanup)

    def test_database_missing_column_cannot_pass_as_null(self):
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            database_path = root / "state.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE state (actual_column TEXT)")
            connection.execute("INSERT INTO state(actual_column) VALUES (NULL)")
            connection.commit()
            connection.close()
            payload = {
                "schema_version": "database-execution-plan.v4",
                "executor_kind": "database",
                "flow_id": "FLOW-DB-COLUMN",
                "stage_id": "STAGE-DB-COLUMN",
                "connection_profile_ref": "db.main",
                "read_only": True,
                "queries": [
                    {
                        "query_id": "DB-0001",
                        "query_ref": "db.state",
                        "parameters_refs": {},
                        "assertions": [
                            {
                                "expected_result_id": "EXPECTED-DB-0001",
                                "kind": "column",
                                "column": "misspelled_column",
                                "operator": "null",
                                "expected": None,
                            }
                        ],
                    }
                ],
            }
            artifact = _write_artifact(
                root / "db",
                executor_kind="database",
                artifact_schema_version="database-execution-plan.v4",
                flow_id="FLOW-DB-COLUMN",
                stage_id="STAGE-DB-COLUMN",
                payload=payload,
            )
            result = DatabaseRunner().run(
                root / "db",
                artifact,
                RuntimeContext(
                    database_connections={"db.main": database_path},
                    query_catalog={
                        "db.state": {
                            "read_only": True,
                            "sql": "SELECT actual_column FROM state",
                        }
                    },
                ),
            )

        self.assertEqual(result.status, RunStatus.FAILED)
        message = result.steps[0].details["assertions"][0]["message"]
        self.assertIn("不存在列", message)

    def test_database_accepts_explicit_read_only_dbapi_connection(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE state (status TEXT)")
        connection.execute("INSERT INTO state(status) VALUES ('ready')")
        connection.commit()
        try:
            with tempfile.TemporaryDirectory() as output:
                root = Path(output)
                payload = {
                    "schema_version": "database-execution-plan.v4",
                    "executor_kind": "database",
                    "flow_id": "FLOW-DBAPI",
                    "stage_id": "STAGE-DBAPI",
                    "connection_profile_ref": "db.readonly",
                    "read_only": True,
                    "queries": [
                        {
                            "query_id": "DB-0001",
                            "query_ref": "db.status",
                            "parameters_refs": {},
                            "assertions": [
                                {
                                    "expected_result_id": "EXPECTED-DB-0001",
                                    "kind": "column",
                                    "column": "status",
                                    "operator": "equals",
                                    "expected": "ready",
                                }
                            ],
                        }
                    ],
                }
                artifact = _write_artifact(
                    root / "dbapi",
                    executor_kind="database",
                    artifact_schema_version="database-execution-plan.v4",
                    flow_id="FLOW-DBAPI",
                    stage_id="STAGE-DBAPI",
                    payload=payload,
                )
                result = DatabaseRunner().run(
                    root / "dbapi",
                    artifact,
                    RuntimeContext(
                        database_connections={
                            "db.readonly": ReadOnlyDatabaseConnection(
                                connection=connection,
                                dialect="sqlite-test",
                            )
                        },
                        query_catalog={
                            "db.status": {
                                "read_only": True,
                                "sql": "SELECT status FROM state",
                            }
                        },
                    ),
                )
        finally:
            connection.close()

        self.assertEqual(result.status, RunStatus.PASSED)


if __name__ == "__main__":
    unittest.main()
