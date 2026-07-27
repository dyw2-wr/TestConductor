"""Run one reviewed intent through UI, API, DB, performance, and TCP artifacts.

The two model boundaries use deterministic demo-data gateways so the demo is
repeatable without an API key.  They still go through the real prompts,
Pydantic contracts, compilers, reviews, hash gates, runners, and evidence
writer. UI produces a Procedure execution plan but is blocked because this demo
does not select a Procedure asset database; the other four flows execute against
services started by this file.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socketserver
import sqlite3
import sys
from threading import Thread
from typing import Any, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.test_platform.intent.builder import DefaultDesignBuilder
from apps.test_platform.intent.contracts import ReviewDecision, TestDesignRequest
from apps.test_platform.intent.prompt_builder import DefaultDesignPromptBuilder
from apps.test_platform.intent.service import TestDesignPipeline
from apps.test_platform.planning.catalogs import PlanningCatalogSnapshot
from apps.test_platform.planning.compiler import TestPlanCompiler
from apps.test_platform.planning.contracts import PlanReviewDecision
from apps.test_platform.planning.planner import (
    DefaultPlanPromptBuilder,
    PlanDraftGenerator,
)
from apps.test_platform.runners.contracts import RunStatus, RuntimeContext
from apps.test_platform.runners.execution import ExecutionCoordinator
from apps.test_platform.runners.performance_http import HttpPerformanceDriver
from apps.test_platform.workflow import IntentToExecutionWorkflow

DEMO_DATA = ROOT / "examples" / "demo_data"


def _load_demo_data(name: str) -> dict[str, Any]:
    return json.loads((DEMO_DATA / name).read_text(encoding="utf-8"))


class _DemoGateway:
    """Stand-in for an LLM; output still has to pass the production schema."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)

    def generate(self, messages, output_schema):
        if not messages:
            raise ValueError("model messages must not be empty")
        return output_schema.model_validate(self.payload)


class _DemoHttpHandler(BaseHTTPRequestHandler):
    server_version = "TestConductorDemo/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/":
            body = (
                b"<!doctype html><html><head><title>TestConductor demo</title></head>"
                b"<body><main><h1>Demo ready</h1></main></body></html>"
            )
            content_type = "text/html; charset=utf-8"
            status = 200
        elif self.path == "/health":
            body = b'{"status":"ok"}'
            content_type = "application/json"
            status = 200
        else:
            body = b'{"error":"not found"}'
            content_type = "application/json"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _DemoTcpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.sendall(b"ready\n")


class _DemoTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass(frozen=True)
class _DemoEnvironment:
    http_base_url: str
    health_url: str
    tcp_host: str
    tcp_port: int
    database_path: Path


@contextmanager
def _local_environment(runtime_root: Path) -> Iterator[_DemoEnvironment]:
    """Start disposable services and keep the SQLite fixture with run artifacts."""

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), _DemoHttpHandler)
    tcp_server = _DemoTcpServer(("127.0.0.1", 0), _DemoTcpHandler)
    http_thread = Thread(target=http_server.serve_forever, daemon=True)
    tcp_thread = Thread(target=tcp_server.serve_forever, daemon=True)
    http_thread.start()
    tcp_thread.start()
    runtime_root.mkdir(parents=True, exist_ok=True)
    database_path = runtime_root / "demo.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE service_status (status TEXT NOT NULL)")
        connection.execute("INSERT INTO service_status(status) VALUES ('ready')")
        connection.commit()
    finally:
        connection.close()
    http_port = int(http_server.server_address[1])
    tcp_port = int(tcp_server.server_address[1])
    try:
        yield _DemoEnvironment(
            http_base_url=f"http://127.0.0.1:{http_port}",
            health_url=f"http://127.0.0.1:{http_port}/health",
            tcp_host="127.0.0.1",
            tcp_port=tcp_port,
            database_path=database_path,
        )
    finally:
        http_server.shutdown()
        tcp_server.shutdown()
        http_server.server_close()
        tcp_server.server_close()
        http_thread.join(timeout=2)
        tcp_thread.join(timeout=2)


def _build_workflow() -> IntentToExecutionWorkflow:
    design_pipeline = TestDesignPipeline(
        DefaultDesignBuilder(
            DefaultDesignPromptBuilder(),
            _DemoGateway(
                _load_demo_data("multichannel_initial_design_candidate.json")
            ),
        )
    )
    compiler = TestPlanCompiler()
    plan_generator = PlanDraftGenerator(
        DefaultPlanPromptBuilder(),
        _DemoGateway(_load_demo_data("multichannel_initial_plan_candidate.json")),
        compiler,
    )
    return IntentToExecutionWorkflow(
        design_pipeline,
        plan_generator,
        plan_compiler=compiler,
        # The deterministic example writes only to its selected artifact root.
        # Production workflow instances use the default database recorder.
        coordinator=ExecutionCoordinator(run_history_recorder=None),
    )


def _catalog_for(environment: _DemoEnvironment) -> PlanningCatalogSnapshot:
    payload = _load_demo_data("multichannel_initial_catalog_content.json")
    payload["tcp_port_probes"][0]["port"] = environment.tcp_port
    return PlanningCatalogSnapshot.build(**payload)


def _default_output_root() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return ROOT / "run_artifacts" / "initial-multichannel-demo" / timestamp


def run_demo(output_root: Path | None = None) -> dict[str, Any]:
    """Execute the demo and return a compact, machine-readable result."""

    root = (output_root or _default_output_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    workflow = _build_workflow()
    request = TestDesignRequest.model_validate(
        _load_demo_data("multichannel_initial_design_request.json")
    )
    generated = workflow.generate_design(request)
    design_review = workflow.review_design(
        generated,
        decision=ReviewDecision.APPROVED,
        comments="多通道逻辑场景与原始需求已核对",
    )
    if design_review.approved_bundle is None:
        raise RuntimeError("demo design was not approved")

    with _local_environment(root / "runtime") as environment:
        catalog = _catalog_for(environment)
        compiled = workflow.compile_plan(
            design_review.approved_bundle,
            catalog,
            root,
        )
        if not compiled.validation.passed:
            raise RuntimeError(f"demo plan validation failed: {compiled.validation.findings}")
        plan_review = workflow.review_plan(
            compiled,
            catalog,
            decision=PlanReviewDecision.APPROVED,
            comments="五类 executor 映射、产物和阈值已核对",
        )
        if plan_review.approved_bundle is None:
            raise RuntimeError("demo plan was not approved")

        context = RuntimeContext(
            variables={
                "runtime": {
                    "demo": {
                        "health_url": environment.health_url,
                    }
                }
            },
            base_urls={"runtime.demo.http": environment.http_base_url},
            query_catalog={
                "db.demo.status": {
                    "read_only": True,
                    "sql": "SELECT status FROM service_status LIMIT 1",
                }
            },
            database_connections={"runtime.demo.db": environment.database_path},
            performance_drivers={"driver.demo.http": HttpPerformanceDriver()},
            performance_profiles={"perf.demo.smoke": {"scope": "local-demo"}},
            performance_mode="live",
            network_hosts={"runtime.demo.tcp": environment.tcp_host},
            evidence_dir=root / "evidence",
            max_performance_duration_seconds=5,
            max_virtual_users=10,
        )
        summary = workflow.execute(
            plan_review.approved_bundle,
            root,
            context,
            run_id="RUN-INITIAL-MULTICHANNEL-V4",
        )

    flow_results = []
    for flow in summary.flows:
        stage = flow.stages[0]
        flow_results.append(
            {
                "flow_id": flow.flow_id,
                "executor_kind": stage.executor_kind,
                "status": flow.status.value
                if isinstance(flow.status, RunStatus)
                else str(flow.status),
                "errors": list(flow.errors),
            }
        )
    result = {
        "candidate_source": "deterministic_demo_gateway",
        "design_status": design_review.design.status.value,
        "plan_status": plan_review.plan.status.value,
        "overall_status": summary.status.value
        if isinstance(summary.status, RunStatus)
        else str(summary.status),
        "flow_results": flow_results,
        "artifact_root": str(root),
        "run_manifest": summary.manifest_path,
        "reports": dict(summary.report_paths),
        "ui_note": "UI Procedure plan generated; no Procedure asset database selected",
    }
    (root / "demo-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Artifact root. Omit it to create a unique run_artifacts directory.",
    )
    args = parser.parse_args()
    result = run_demo(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    by_executor = {
        item["executor_kind"]: item["status"] for item in result["flow_results"]
    }
    expected = {
        "procedure_playwright": "blocked",
        "http_api": "passed",
        "database": "passed",
        "performance": "passed",
        "tcp_port": "passed",
    }
    return 0 if by_executor == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
