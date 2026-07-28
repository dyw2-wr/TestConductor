"""第三层执行器的最小运行时契约。

第二层只产出执行器文件；本包只读取已经审核通过的文件并运行它们。运行时资源
（变量、连接、transport 和性能 driver）通过 :class:`RuntimeContext` 注入，因而
不会把密码或环境连接信息写回测试计划。UI Agent 的 Action/Check 在审批时冻结进执行产物。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
import json


# Reserved executor names stay visible in plan contracts without being loaded
# into the default runner registry. Keep this list small until an integration
# contract and result callback actually exist.
DEFERRED_EXECUTOR_KINDS = frozenset()


class RunStatus(str, Enum):
    """一次执行或单个步骤的最终状态。"""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    ERROR = "error"
    INCONCLUSIVE = "inconclusive"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class CleanupResult:
    """The only accepted cleanup hook result; success must be explicit."""

    success: bool
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise TypeError("CleanupResult.success 必须是 bool")
        if not isinstance(self.details, dict):
            raise TypeError("CleanupResult.details 必须是 dict")


CleanupCallable = Callable[..., CleanupResult]


@dataclass(frozen=True)
class ReadOnlyDatabaseConnection:
    """A deployment-injected DB-API connection explicitly marked read-only.

    SQLite paths remain the default. Deployments using PostgreSQL/MySQL/etc.
    may inject a connection only through this wrapper, making the read-only
    policy visible at the TestConductor boundary instead of accepting arbitrary
    connection objects.
    """

    connection: Any
    dialect: str = "dbapi"
    close_when_done: bool = False

    def __post_init__(self) -> None:
        if self.connection is None or not hasattr(self.connection, "cursor"):
            raise ValueError("ReadOnlyDatabaseConnection.connection 必须是 DB-API 连接")
        if not isinstance(self.dialect, str) or not self.dialect.strip():
            raise ValueError("ReadOnlyDatabaseConnection.dialect 不能为空")
        if type(self.close_when_done) is not bool:
            raise ValueError("ReadOnlyDatabaseConnection.close_when_done 必须是 bool")


@dataclass
class RuntimeContext:
    """第三层运行时依赖注入。

    这里的值来自部署环境或 secret store，不会被序列化进 artifact。连接对象可以
    当前只能是 ``sqlite`` 文件路径；HTTP transport 和性能 driver 在测试时可用
    fake 实现替换。端口 stage 通过 ``network_hosts`` 将 catalog 的 ``host_ref``
    解析为运行时主机名或 IP，不把真实主机写回计划。``cleanup_hooks`` 只由 flow 协调器调用。计划中的 ``slot`` 会
    成为关键字参数名，对应值由 ``variable_ref`` 从 ``variables`` 解析；stage
    runner 不执行清理。
    """

    variables: dict[str, Any] = field(default_factory=dict)
    base_urls: dict[str, str] = field(default_factory=dict)
    ui_browser_headless: bool = True
    # Catalog host_ref -> runtime hostname/IP. The actual host is never written
    # to a plan or evidence; port probes resolve it only at execution time.
    network_hosts: dict[str, str] = field(default_factory=dict)
    query_catalog: dict[str, Any] = field(default_factory=dict)
    database_schemas: dict[str, Any] = field(default_factory=dict)
    database_connections: dict[str, Any] = field(default_factory=dict)
    cleanup_hooks: dict[str, CleanupCallable] = field(default_factory=dict)
    performance_drivers: dict[str, Any] = field(default_factory=dict)
    performance_profiles: dict[str, Any] = field(default_factory=dict)
    # Explicit attestation from the runtime fixture provider. The coordinator
    # requires an exact required_state_id -> data_id match before executing a
    # PlanDataGuaranteeResolution; it never infers guarantees from variable names.
    data_guarantees: dict[str, str] = field(default_factory=dict)
    performance_mode: str = "dry_run"
    secret_variable_names: set[str] = field(default_factory=set)
    max_response_bytes: int = 1_000_000
    max_performance_duration_seconds: float = 86_400.0
    max_virtual_users: int = 100_000
    evidence_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        if type(self.ui_browser_headless) is not bool:
            raise ValueError("ui_browser_headless 必须是 bool")
        for host_ref, host in self.network_hosts.items():
            if (
                not isinstance(host_ref, str)
                or not host_ref.strip()
                or not isinstance(host, str)
                or not host.strip()
                or host != host.strip()
                or any(char.isspace() for char in host)
                or len(host.strip()) > 253
                or "\x00" in host
                or "://" in host
                or "/" in host
                or "\\" in host
                or "@" in host
            ):
                raise ValueError(
                    "network_hosts 必须映射非空 host_ref 到无 scheme/路径/用户信息的主机名或 IP"
                )
        if any(
            not isinstance(state_id, str)
            or not state_id.strip()
            or not isinstance(data_id, str)
            or not data_id.strip()
            for state_id, data_id in self.data_guarantees.items()
        ):
            raise ValueError("data_guarantees 必须映射非空 required_state_id 到 data_id")
        if self.evidence_dir is not None:
            self.evidence_dir = Path(self.evidence_dir)
        if self.performance_mode not in {"dry_run", "live"}:
            raise ValueError("performance_mode 必须是 dry_run 或 live")
        if self.max_response_bytes < 1 or self.max_response_bytes > 50_000_000:
            raise ValueError("max_response_bytes 超出允许范围")
        if self.max_performance_duration_seconds <= 0 or self.max_performance_duration_seconds > 604_800:
            raise ValueError("max_performance_duration_seconds 超出允许范围")
        if self.max_virtual_users < 1 or self.max_virtual_users > 1_000_000:
            raise ValueError("max_virtual_users 超出允许范围")


@dataclass
class StepResult:
    """一个请求、查询或性能场景的可审计结果。"""

    step_id: str
    status: RunStatus | str
    message: str = ""
    duration_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = str(self.status.value if isinstance(self.status, Enum) else self.status)
        return value


@dataclass
class RunResult:
    """第三层统一返回值；不携带未经脱敏的响应或数据库行。"""

    run_id: str
    executor_kind: str
    flow_id: str
    stage_id: str
    status: RunStatus | str
    started_at: str
    finished_at: str
    steps: list[StepResult] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    manifest_path: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    external_action_started: bool = False

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        executor_kind: str,
        flow_id: str,
        stage_id: str,
    ) -> "RunResult":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            run_id=run_id,
            executor_kind=executor_kind,
            flow_id=flow_id,
            stage_id=stage_id,
            status=RunStatus.INCONCLUSIVE,
            started_at=now,
            finished_at=now,
        )

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = str(self.status.value if isinstance(self.status, Enum) else self.status)
        value["steps"] = [step.as_dict() for step in self.steps]
        return value

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, default=str)


@dataclass
class RunManifest:
    """一次第三层运行的统一审计摘要，不保存原始响应、SQL 或秘密。"""

    schema_version: str
    run_id: str
    design_id: str
    design_version: int
    design_content_hash: str
    design_input_content_hash: str
    plan_id: str
    plan_version: int
    plan_content_hash: str
    validation_content_hash: str
    review_content_hash: str
    artifact_set_hash: str
    status: RunStatus | str
    started_at: str
    finished_at: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    flows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value if isinstance(self.status, Enum) else self.status
        return value


@dataclass
class ExecutionSummary:
    """协调器执行一个 approved plan 后返回的结果和报告路径。"""

    run_id: str
    status: RunStatus | str
    stages: list[RunResult] = field(default_factory=list)
    flows: list["FlowRunResult"] = field(default_factory=list)
    manifest_path: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    report_paths: dict[str, str] = field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value if isinstance(self.status, Enum) else self.status,
            "stages": [stage.as_dict() for stage in self.stages],
            "flows": [flow.as_dict() for flow in self.flows],
            "manifest_path": self.manifest_path,
            "errors": list(self.errors),
            "report_paths": dict(self.report_paths),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class FlowRunResult:
    """One sequential PlanFlow result, including its single cleanup outcome."""

    flow_id: str
    status: RunStatus | str
    started_at: str
    finished_at: str
    stages: list[RunResult] = field(default_factory=list)
    cleanup: Optional[StepResult] = None
    evidence: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "status": self.status.value if isinstance(self.status, Enum) else str(self.status),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stages": [stage.as_dict() for stage in self.stages],
            "cleanup": self.cleanup.as_dict() if self.cleanup is not None else None,
            "evidence": list(self.evidence),
            "errors": list(self.errors),
        }


class RunnerError(RuntimeError):
    """带稳定错误码的执行阻断/运行错误。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def finish_result(result: RunResult, status: RunStatus | str) -> RunResult:
    """设置最终状态和结束时间，供各 runner 的 finally 使用。"""

    result.status = status
    result.finished_at = datetime.now(timezone.utc).isoformat()
    return result


def ensure_mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{field_name} 必须是对象")
    return value
