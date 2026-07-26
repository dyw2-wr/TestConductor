from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import tempfile
import unittest
from unittest.mock import patch

from apps.test_platform.runners.contracts import RunStatus, RuntimeContext
from apps.test_platform.runners.performance import PerformanceRunner
from apps.test_platform.runners.performance_http import HttpPerformanceDriver
from tests.test_runner_flow_v4 import _write_artifact


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        return


class HttpPerformanceDriverTests(unittest.TestCase):
    def test_validates_driver_bounds_and_http_input_contract(self):
        for timeout in (0, -1, 61):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "0-60"):
                    HttpPerformanceDriver(request_timeout_seconds=timeout)

        driver = HttpPerformanceDriver()
        invalid_payloads = (
            ({}, "requires inputs"),
            ({"inputs": {"url": "/relative"}}, "absolute credential-free URL"),
            (
                {"inputs": {"url": "http://user:pass@localhost/path"}},
                "absolute credential-free URL",
            ),
            (
                {"inputs": {"url": "http://localhost/path", "headers": []}},
                "headers must be an object",
            ),
        )
        for payload, message in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    driver.run(payload, RuntimeContext())

    def test_aggregates_failures_and_nearest_rank_percentiles(self):
        driver = HttpPerformanceDriver()
        with patch.object(
            driver,
            "_worker",
            return_value=([4.0, 1.0, 2.0, 3.0], 1, 4),
        ):
            result = driver.run(
                {
                    "inputs": {"url": "http://127.0.0.1/health"},
                    "stages": [{"duration_seconds": 0.01, "virtual_users": 1}],
                },
                RuntimeContext(),
            )

        self.assertEqual(result["metrics"]["request_count"], 4)
        self.assertEqual(result["metrics"]["error_rate"], 0.25)
        self.assertEqual(
            result["metrics"]["latency_ms"],
            {"p50": 2.0, "p95": 4.0, "p99": 4.0, "unit": "ms"},
        )

    def test_runs_bounded_local_http_load_and_returns_percentiles(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = HttpPerformanceDriver(request_timeout_seconds=2).run(
                {
                    "inputs": {"url": f"http://127.0.0.1:{server.server_address[1]}/health"},
                    "stages": [{"duration_seconds": 0.05, "virtual_users": 1}],
                },
                RuntimeContext(),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertGreater(result["metrics"]["request_count"], 0)
        self.assertEqual(result["metrics"]["error_rate"], 0)
        self.assertGreaterEqual(result["metrics"]["latency_ms"]["p95"], 0)

    def test_performance_runner_uses_profile_bound_http_target_in_live_mode(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                payload = {
                    "schema_version": "performance-execution-plan.v4",
                    "executor_kind": "performance",
                    "flow_id": "FLOW-PERF-HTTP",
                    "stage_id": "STAGE-PERF-HTTP",
                    "driver_ref": "driver.http",
                    "load_profile_ref": "perf.health.smoke",
                    "stages": [{"duration_seconds": 0.05, "virtual_users": 1}],
                    "input_refs": {},
                    "thresholds": [
                        {
                            "threshold_id": "THRESHOLD-1",
                            "expected_result_id": "EXPECTED-PERF",
                            "metric": "latency_ms",
                            "operator": "lte",
                            "value": 5000,
                            "unit": "ms",
                            "percentile": "p95",
                        }
                    ],
                }
                artifact = _write_artifact(
                    root / "perf-stage",
                    executor_kind="performance",
                    artifact_schema_version="performance-execution-plan.v4",
                    flow_id="FLOW-PERF-HTTP",
                    stage_id="STAGE-PERF-HTTP",
                    payload=payload,
                )
                result = PerformanceRunner().run(
                    root / "perf-stage",
                    artifact,
                    RuntimeContext(
                        performance_mode="live",
                        performance_profiles={
                            "perf.health.smoke": {
                                "inputs": {
                                    "url": (
                                        f"http://127.0.0.1:{server.server_address[1]}"
                                        "/health"
                                    )
                                }
                            }
                        },
                        performance_drivers={"driver.http": HttpPerformanceDriver()},
                        evidence_dir=root / "evidence",
                    ),
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.status, RunStatus.PASSED)
        self.assertTrue(result.external_action_started)


if __name__ == "__main__":
    unittest.main()
