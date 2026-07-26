"""Single-endpoint TCP port runner.

The artifact contains only reviewed ``host_ref``/port pairs.  The concrete host
is injected through :class:`RuntimeContext.network_hosts` immediately before a
real connection attempt; this runner never expands a range or performs a scan.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import socket
from time import perf_counter
from typing import Any, Mapping
from uuid import uuid4

from .base import (
    ExecutorRunner,
    artifact_stage_identity,
    load_json_payload,
    prepare_artifact,
    write_evidence,
)
from .contracts import (
    RunResult,
    RunStatus,
    RunnerError,
    RuntimeContext,
    StepResult,
    finish_result,
)


_STATE_OPERATORS = {"equals", "not_equals"}
_LATENCY_OPERATORS = {"equals", "lte", "lt", "gte", "gt", "exists"}


@dataclass(frozen=True)
class _PreparedAssertion:
    assertion_id: str
    expected_result_id: str
    kind: str
    operator: str
    expected: Any
    unit: str | None


@dataclass(frozen=True)
class _PreparedProbe:
    probe_id: str
    source: Mapping[str, Any]
    probe_ref: str
    host_ref: str
    host: str
    port: int
    timeout_seconds: float
    assertions: tuple[_PreparedAssertion, ...]


class PortRunner(ExecutorRunner):
    """Run catalog-bound single TCP endpoint probes with structured assertions."""

    executor_kind = "tcp_port"
    payload_schema = "tcp-port-execution-plan.v4"

    def preflight(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> None:
        """Validate the artifact and runtime host references without connecting."""

        self._prepare_plan(artifact_dir, artifact_bundle, context)

    def _prepare_plan(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> tuple[Any, tuple[_PreparedProbe, ...]]:
        workspace = prepare_artifact(
            artifact_dir,
            artifact_bundle,
            expected_executor=self.executor_kind,
        )
        payload_path, payload = load_json_payload(workspace, "payload")
        if payload.get("schema_version") != self.payload_schema:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"{payload_path.name} 不是 {self.payload_schema}",
            )
        allowed_payload_keys = {
            "schema_version",
            "executor_kind",
            "flow_id",
            "stage_id",
            "design_id",
            "design_version",
            "plan_id",
            "plan_version",
            "probes",
        }
        if set(payload) != allowed_payload_keys:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "TCP port payload 字段必须与 v4 adapter 精确一致",
            )
        raw_probes = payload.get("probes")
        if not isinstance(raw_probes, list) or not raw_probes:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "TCP port 计划必须包含非空 probes")
        prepared = tuple(
            self._prepare_probe(index, value, context)
            for index, value in enumerate(raw_probes)
        )
        return workspace, prepared

    @classmethod
    def _prepare_probe(
        cls,
        index: int,
        value: Any,
        context: RuntimeContext,
    ) -> _PreparedProbe:
        if not isinstance(value, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"probes[{index}] 必须是对象")
        allowed_keys = {
            "probe_id",
            "source",
            "probe_ref",
            "host_ref",
            "port",
            "timeout_seconds",
            "assertions",
        }
        if set(value) != allowed_keys:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"probes[{index}] 字段必须与 v4 adapter 精确一致",
            )
        probe_id = cls._nonempty(value.get("probe_id"), f"probes[{index}].probe_id")
        probe_ref = cls._nonempty(value.get("probe_ref"), f"{probe_id}.probe_ref")
        host_ref = cls._nonempty(value.get("host_ref"), f"{probe_id}.host_ref")
        source = value.get("source")
        if not isinstance(source, Mapping) or set(source) != {"source_kind", "source_id"}:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{probe_id}.source 身份无效")
        if source.get("source_kind") not in {
            "operation",
            "expected_result",
            "required_state",
        }:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{probe_id}.source.source_kind 无效")
        cls._nonempty(source.get("source_id"), f"{probe_id}.source.source_id")

        raw_port = value.get("port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int) or not 1 <= raw_port <= 65_535:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{probe_id}.port 超出 1-65535")
        raw_timeout = value.get("timeout_seconds")
        if (
            isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, (int, float))
            or not math.isfinite(float(raw_timeout))
            or not 0 < float(raw_timeout) <= 30
        ):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{probe_id}.timeout_seconds 无效")
        host = context.network_hosts.get(host_ref)
        if not isinstance(host, str) or not host.strip():
            raise RunnerError("RUNTIME_RESOURCE_MISSING", f"未注入 network host: {host_ref}")
        if (
            len(host.strip()) > 253
            or "\x00" in host
            or "://" in host
            or "/" in host
            or "\\" in host
            or "@" in host
        ):
            raise RunnerError("RUNTIME_RESOURCE_INVALID", f"network host 无效: {host_ref}")

        raw_assertions = value.get("assertions")
        if not isinstance(raw_assertions, list):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{probe_id}.assertions 必须是数组")
        assertions = tuple(
            cls._prepare_assertion(probe_id, assertion_index, assertion)
            for assertion_index, assertion in enumerate(raw_assertions)
        )
        return _PreparedProbe(
            probe_id=probe_id,
            source=dict(source),
            probe_ref=probe_ref,
            host_ref=host_ref,
            host=host.strip(),
            port=raw_port,
            timeout_seconds=float(raw_timeout),
            assertions=assertions,
        )

    @staticmethod
    def _nonempty(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{field_name} 必须是非空字符串")
        return value.strip()

    @classmethod
    def _prepare_assertion(
        cls, probe_id: str, index: int, value: Any
    ) -> _PreparedAssertion:
        path = f"{probe_id}.assertions[{index}]"
        if not isinstance(value, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{path} 必须是对象")
        allowed_keys = {
            "assertion_id",
            "expected_result_id",
            "after_operation_id",
            "observable_ref",
            "kind",
            "operator",
            "expected",
            "unit",
        }
        if set(value) != allowed_keys:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{path} 字段不完整或包含未知字段")
        assertion_id = cls._nonempty(value.get("assertion_id"), f"{path}.assertion_id")
        expected_result_id = cls._nonempty(
            value.get("expected_result_id"), f"{path}.expected_result_id"
        )
        cls._nonempty(value.get("after_operation_id"), f"{path}.after_operation_id")
        cls._nonempty(value.get("observable_ref"), f"{path}.observable_ref")
        kind = cls._nonempty(value.get("kind"), f"{path}.kind").lower()
        operator = cls._nonempty(value.get("operator"), f"{path}.operator").lower()
        expected = value.get("expected")
        unit = value.get("unit")
        if unit is not None and (not isinstance(unit, str) or not unit.strip()):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{path}.unit 无效")
        if kind == "state":
            if (
                operator not in _STATE_OPERATORS
                or expected not in {"open", "closed", "filtered"}
                or unit is not None
            ):
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{path} state 断言无效")
        elif kind == "connect_latency_ms":
            records_observation = operator == "exists" and expected is None
            compares_value = (
                operator in (_LATENCY_OPERATORS - {"exists"})
                and not isinstance(expected, bool)
                and isinstance(expected, (int, float))
                and math.isfinite(float(expected))
                and float(expected) >= 0
            )
            if not (records_observation or compares_value) or unit not in {None, "ms"}:
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{path} latency 断言无效")
        else:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{path} 不支持 kind: {kind}")
        return _PreparedAssertion(
            assertion_id=assertion_id,
            expected_result_id=expected_result_id,
            kind=kind,
            operator=operator,
            expected=expected,
            unit=unit.strip() if isinstance(unit, str) else None,
        )

    def run(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> RunResult:
        run_id = f"run-{uuid4().hex}"
        flow_id, stage_id = artifact_stage_identity(artifact_bundle)
        result = RunResult.new(
            run_id=run_id,
            executor_kind=self.executor_kind,
            flow_id=flow_id,
            stage_id=stage_id,
        )
        external_action_started = [False]
        try:
            workspace, probes = self._prepare_plan(artifact_dir, artifact_bundle, context)
            # Keep the result portable and avoid exposing the runtime artifact root.
            result.manifest_path = "manifest.json"
            for probe in probes:
                self._run_probe(result, probe, context, external_action_started)
                if result.steps and result.steps[-1].status != RunStatus.PASSED:
                    break
            if result.steps and all(item.status == RunStatus.PASSED for item in result.steps):
                result.status = RunStatus.PASSED
            elif result.steps:
                result.status = RunStatus.FAILED
            else:
                result.status = RunStatus.INCONCLUSIVE
        except RunnerError as exc:
            result.errors.append(f"{exc.code}: {exc.message}")
            result.status = (
                RunStatus.ERROR
                if external_action_started[0]
                else RunStatus.BLOCKED
            )
        except Exception:
            # Do not expose socket/library messages, which may contain the runtime host.
            result.errors.append("TCP_PORT_RUN_ERROR: socket probe failed")
            result.status = RunStatus.ERROR
        finally:
            result.external_action_started = external_action_started[0]
            finish_result(result, result.status)
        return result

    @staticmethod
    def _compare(operator: str, actual: Any, expected: Any) -> bool:
        if operator == "exists":
            return (
                isinstance(actual, (int, float))
                and not isinstance(actual, bool)
                and math.isfinite(float(actual))
            )
        if operator == "equals":
            return actual == expected
        if operator == "not_equals":
            return actual != expected
        if operator == "lte":
            return actual <= expected
        if operator == "lt":
            return actual < expected
        if operator == "gte":
            return actual >= expected
        if operator == "gt":
            return actual > expected
        return False

    def _run_probe(
        self,
        result: RunResult,
        probe: _PreparedProbe,
        context: RuntimeContext,
        external_action_started: list[bool],
    ) -> None:
        started = perf_counter()
        observed_state = "open"
        connection_outcome = "connected"
        try:
            external_action_started[0] = True
            connection = socket.create_connection(
                (probe.host, probe.port), timeout=probe.timeout_seconds
            )
            try:
                connection.close()
            except Exception:
                pass
        except ConnectionRefusedError:
            observed_state = "closed"
            connection_outcome = "connection_refused"
        except TimeoutError:
            observed_state = "filtered"
            connection_outcome = "connection_timeout"
        except OSError:
            # DNS/routing and other socket failures do not prove that a port is closed.
            observed_state = "unreachable"
            connection_outcome = "connection_error"
        latency_ms = (perf_counter() - started) * 1000
        assertion_results: list[dict[str, Any]] = []
        passed = True
        for assertion in probe.assertions:
            actual = observed_state if assertion.kind == "state" else latency_ms
            ok = self._compare(assertion.operator, actual, assertion.expected)
            assertion_results.append(
                {
                    "assertion_id": assertion.assertion_id,
                    "expected_result_id": assertion.expected_result_id,
                    "kind": assertion.kind,
                    "operator": assertion.operator,
                    "expected": assertion.expected,
                    "actual": actual,
                    "passed": ok,
                }
            )
            passed = passed and ok
        evidence_name = write_evidence(
            context,
            f"{result.run_id}-{probe.probe_id}",
            {
                "probe_id": probe.probe_id,
                "probe_ref": probe.probe_ref,
                "host_ref": probe.host_ref,
                "port": probe.port,
                "connection_outcome": connection_outcome,
                "observed_state": observed_state,
                "connect_latency_ms": latency_ms,
                "assertions": assertion_results,
            },
        )
        if evidence_name:
            result.evidence.append(evidence_name)
        result.steps.append(
            StepResult(
                step_id=probe.probe_id,
                status=RunStatus.PASSED if passed else RunStatus.FAILED,
                message="all port assertions passed" if passed else "one or more port assertions failed",
                duration_ms=latency_ms,
                details={
                    "probe_ref": probe.probe_ref,
                    "host_ref": probe.host_ref,
                    "port": probe.port,
                    "observed_state": observed_state,
                    "connect_latency_ms": latency_ms,
                    "assertions": assertion_results,
                },
                evidence=[evidence_name] if evidence_name else [],
            )
        )


__all__ = ["PortRunner"]
