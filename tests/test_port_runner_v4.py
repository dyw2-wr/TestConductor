from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from apps.test_platform.intent.contracts import RequirementInput, ReviewDecision
from apps.test_platform.planning import (
    ExecutorKind,
    ExpectedResultSelection,
    OperationSelection,
    PlanCandidate,
    PlanFlowCandidate,
    PlanStageCandidate,
    PlanningCatalogSnapshot,
    PortObservable,
    TcpPortProbe,
    TestPlanCompiler,
)
from apps.test_platform.runners import PortRunner, RunStatus, RunnerRegistry, RuntimeContext
from tests.test_design_layer_v4 import _pipeline, _request, _valid_candidate


def _approved_port_design(
    *, expected_state: str, include_latency: bool, record_latency_only: bool = False
):
    payload = _valid_candidate()
    scenario = payload["scenarios"][0]
    scenario["title"] = "服务 TCP 端点可连接"
    scenario["requirement_ids"] = ["REQ-PORT"]
    scenario["required_states"] = []
    scenario["operations"] = [
        {"text": "探测服务 TCP 端点", "channel_hint": "port"}
    ]
    scenario["expected_results"] = [
        {
            "text": f"TCP 端点状态为 {expected_state}",
            "after_operation_index": 1,
            "channel_hint": "port",
            "operator": "equals",
            "expected": expected_state,
        }
    ]
    if include_latency:
        latency_expected = {
            "text": "记录 TCP 建连耗时" if record_latency_only else "TCP 建连耗时不超过 2000 ms",
            "after_operation_index": 1,
            "channel_hint": "port",
        }
        if not record_latency_only:
            latency_expected.update(
                {"operator": "lte", "expected": 2000, "unit": "ms"}
            )
        scenario["expected_results"].append(latency_expected)
    scenario["data_requirements"] = []
    scenario["state_impact"] = {
        "impact": "read_only",
        "rationale": {"text": "TCP connect 探测不修改服务状态"},
        "cleanup_goal": None,
    }
    pipeline, _ = _pipeline(payload, knowledge=[])
    result = pipeline.generate(
        _request(
            scopes=[],
            allowed_channels=["port"],
            requirements=[
                RequirementInput(
                    requirement_id="REQ-PORT",
                    content="验证登记的服务 TCP 端点是否可连接，并记录建连耗时。",
                )
            ],
        )
    )
    approved, review = pipeline.review(
        result,
        decision=ReviewDecision.APPROVED,
        comments="端点和预期状态已核对",
    )
    return pipeline.build_approved_bundle(result, approved, review)


def _catalog(port: int) -> PlanningCatalogSnapshot:
    return PlanningCatalogSnapshot.build(
        catalog_id="catalog.port.local.v4",
        system_id="account-web",
        environment="staging",
        available_executors=["tcp_port"],
        tcp_port_probes=[
            TcpPortProbe(
                probe_ref="port.account.service",
                description="Probe the reviewed account service endpoint",
                host_ref="network.account.service",
                port=port,
                timeout_seconds=1,
                observables=[
                    PortObservable(
                        observable_ref="observable.port.state",
                        description="TCP connection state",
                        kind="state",
                    ),
                    PortObservable(
                        observable_ref="observable.port.latency",
                        description="TCP connection latency in milliseconds",
                        kind="connect_latency_ms",
                    ),
                ],
            )
        ],
    )


def _compile_port_stage(
    root: Path,
    port: int,
    *,
    expected_state: str,
    include_latency: bool,
    record_latency_only: bool = False,
):
    bundle = _approved_port_design(
        expected_state=expected_state,
        include_latency=include_latency,
        record_latency_only=record_latency_only,
    )
    scenario = bundle.design.scenarios[0]
    expected = [
        ExpectedResultSelection(
            expected_result_id=scenario.expected_results[0].expected_result_id,
            catalog_ref="port.account.service",
            observable_ref="observable.port.state",
        )
    ]
    if include_latency:
        expected.append(
            ExpectedResultSelection(
                expected_result_id=scenario.expected_results[1].expected_result_id,
                catalog_ref="port.account.service",
                observable_ref="observable.port.latency",
            )
        )
    candidate = PlanCandidate(
        flows=[
            PlanFlowCandidate(
                scenario_id=scenario.scenario_id,
                stages=[
                    PlanStageCandidate(
                        executor_kind=ExecutorKind.TCP_PORT,
                        operations=[
                            OperationSelection(
                                operation_id=scenario.operations[0].operation_id,
                                catalog_ref="port.account.service",
                            )
                        ],
                        expected_results=expected,
                    )
                ],
            )
        ]
    )
    compiler = TestPlanCompiler()
    catalog = _catalog(port)
    plan = compiler.build_draft(bundle, candidate, catalog)
    result = compiler.compile(bundle, plan, catalog, root)
    if not result.validation.passed:
        raise AssertionError(result.validation.findings)
    artifact = result.artifacts[0]
    artifact_dir = (
        root
        / "generated-files"
        / "port"
        / plan.plan_id
        / "v1"
        / artifact.flow_id
        / artifact.stage_id
    )
    return artifact_dir, artifact


class PortRunnerV4Tests(unittest.TestCase):
    def test_record_only_latency_is_compiled_as_exists_and_runs(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                artifact_dir, artifact = _compile_port_stage(
                    root,
                    port,
                    expected_state="open",
                    include_latency=True,
                    record_latency_only=True,
                )
                payload = json.loads(
                    (artifact_dir / "execution.json").read_text(encoding="utf-8")
                )
                latency = payload["probes"][0]["assertions"][1]
                self.assertEqual(latency["operator"], "exists")
                self.assertIsNone(latency["expected"])

                result = PortRunner().run(
                    artifact_dir,
                    artifact,
                    RuntimeContext(
                        network_hosts={"network.account.service": "127.0.0.1"},
                        evidence_dir=root / "evidence",
                    ),
                )

                self.assertEqual(result.status, RunStatus.PASSED)
                self.assertTrue(result.steps[0].details["assertions"][1]["passed"])
        finally:
            listener.close()

    def test_compiled_open_probe_runs_and_records_redacted_evidence(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                artifact_dir, artifact = _compile_port_stage(
                    root,
                    port,
                    expected_state="open",
                    include_latency=True,
                )
                payload = (artifact_dir / "execution.json").read_text(encoding="utf-8")
                self.assertNotIn("127.0.0.1", payload)

                evidence_dir = root / "evidence"
                context = RuntimeContext(
                    network_hosts={"network.account.service": "127.0.0.1"},
                    evidence_dir=evidence_dir,
                )
                runner = PortRunner()
                with patch(
                    "apps.test_platform.runners.port.socket.create_connection",
                    wraps=socket.create_connection,
                ) as connect:
                    runner.preflight(artifact_dir, artifact, context)
                    connect.assert_not_called()
                    result = runner.run(artifact_dir, artifact, context)
                    connect.assert_called_once()

                self.assertEqual(result.status, RunStatus.PASSED)
                self.assertEqual(result.manifest_path, "manifest.json")
                self.assertTrue(result.external_action_started)
                self.assertEqual(result.steps[0].details["observed_state"], "open")
                evidence = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in evidence_dir.glob("*.json")
                )
                self.assertNotIn("127.0.0.1", evidence)
                self.assertIn("network.account.service", evidence)
        finally:
            listener.close()

    def test_compiled_closed_probe_passes_closed_state_assertion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact_dir, artifact = _compile_port_stage(
                root,
                443,
                expected_state="closed",
                include_latency=False,
            )
            with patch(
                "apps.test_platform.runners.port.socket.create_connection",
                side_effect=ConnectionRefusedError,
            ):
                result = PortRunner().run(
                    artifact_dir,
                    artifact,
                    RuntimeContext(
                        network_hosts={"network.account.service": "example.test"}
                    ),
                )
            self.assertEqual(result.status, RunStatus.PASSED)
            self.assertEqual(result.steps[0].details["observed_state"], "closed")

    def test_timeout_is_filtered_instead_of_false_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact_dir, artifact = _compile_port_stage(
                root,
                443,
                expected_state="filtered",
                include_latency=False,
            )
            with patch(
                "apps.test_platform.runners.port.socket.create_connection",
                side_effect=TimeoutError,
            ):
                result = PortRunner().run(
                    artifact_dir,
                    artifact,
                    RuntimeContext(
                        network_hosts={"network.account.service": "example.test"}
                    ),
                )

            self.assertEqual(result.status, RunStatus.PASSED)
            self.assertEqual(result.steps[0].details["observed_state"], "filtered")

    def test_catalog_rejects_ranges_duplicates_and_missing_executor_resources(self):
        with self.assertRaises(ValueError):
            TcpPortProbe(
                probe_ref="port.range",
                description="Invalid range",
                host_ref="network.service",
                port="80-90",
                timeout_seconds=1,
            )
        with self.assertRaises(ValueError):
            PlanningCatalogSnapshot.build(
                catalog_id="catalog.empty.port.v4",
                system_id="account-web",
                environment="staging",
                available_executors=["tcp_port"],
            )
        first = TcpPortProbe(
            probe_ref="port.one",
            description="First",
            host_ref="network.service",
            port=443,
            timeout_seconds=1,
        )
        second = TcpPortProbe(
            probe_ref="port.two",
            description="Second",
            host_ref="network.service",
            port=443,
            timeout_seconds=1,
        )
        with self.assertRaises(ValueError):
            PlanningCatalogSnapshot.build(
                catalog_id="catalog.duplicate.port.v4",
                system_id="account-web",
                environment="staging",
                available_executors=["tcp_port"],
                tcp_port_probes=[first, second],
            )

    def test_registry_registers_tcp_port_runner(self):
        registry = RunnerRegistry()
        self.assertIn("tcp_port", registry.registered_kinds)
        self.assertIsInstance(registry.get("tcp_port"), PortRunner)


if __name__ == "__main__":
    unittest.main()
