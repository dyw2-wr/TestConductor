"""第三层执行器注册表。

HTTP、数据库、性能、端口和 UI Procedure runner 均由本项目执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import DeferredRunner, ExecutorRunner
from .procedure import ProcedureRunner
from .contracts import (
    CleanupResult,
    DEFERRED_EXECUTOR_KINDS,
    ExecutionSummary,
    FlowRunResult,
    RunManifest,
    RunResult,
    RunnerError,
    RunStatus,
    RuntimeContext,
)
from .database import DatabaseRunner
from .execution import ExecutionCoordinator
from .http import HttpRunner
from .performance import PerformanceRunner
from .port import PortRunner


class RunnerRegistry:
    """根据 executor_kind 选择已实现的 runner。"""

    def __init__(
        self,
        *,
        http: ExecutorRunner | None = None,
        database: ExecutorRunner | None = None,
        performance: ExecutorRunner | None = None,
        port: ExecutorRunner | None = None,
        procedure: ExecutorRunner | None = None,
    ):
        self._runners: dict[str, ExecutorRunner] = {
            "http_api": http or HttpRunner(),
            "database": database or DatabaseRunner(),
            "performance": performance or PerformanceRunner(),
            "tcp_port": port or PortRunner(),
            "procedure_playwright": procedure or ProcedureRunner(),
        }

    @property
    def registered_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._runners))

    def get(self, executor_kind: str) -> ExecutorRunner:
        if executor_kind in self._runners:
            return self._runners[executor_kind]
        if executor_kind in DEFERRED_EXECUTOR_KINDS:
            return DeferredRunner(executor_kind)
        raise RunnerError("EXECUTOR_UNKNOWN", f"未登记 executor: {executor_kind}")

    def is_deferred(self, executor_kind: str) -> bool:
        return isinstance(self.get(executor_kind), DeferredRunner)

    def run(
        self,
        executor_kind: str,
        artifact_dir: str | Path,
        artifact_bundle: Any,
        context: RuntimeContext,
        *,
        correlation: dict[str, str] | None = None,
    ) -> RunResult:
        runner = self.get(executor_kind)
        if isinstance(runner, ProcedureRunner):
            return runner.run(
                Path(artifact_dir),
                artifact_bundle,
                context,
                correlation=correlation,
            )
        return runner.run(Path(artifact_dir), artifact_bundle, context)

    def preflight(
        self,
        executor_kind: str,
        artifact_dir: str | Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> None:
        runner = self.get(executor_kind)
        preflight = getattr(runner, "preflight", None)
        if not callable(preflight):
            raise RunnerError(
                "RUNNER_PREFLIGHT_UNAVAILABLE",
                f"runner 未实现 preflight: {executor_kind}",
            )
        preflight(Path(artifact_dir), artifact_bundle, context)


__all__ = [
    "DatabaseRunner",
    "ProcedureRunner",
    "CleanupResult",
    "DEFERRED_EXECUTOR_KINDS",
    "DeferredRunner",
    "ExecutorRunner",
    "HttpRunner",
    "PerformanceRunner",
    "PortRunner",
    "RunResult",
    "RunManifest",
    "RunnerError",
    "ExecutionSummary",
    "FlowRunResult",
    "RunStatus",
    "RunnerRegistry",
    "RuntimeContext",
    "ExecutionCoordinator",
]
