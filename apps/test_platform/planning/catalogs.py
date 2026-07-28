"""Typed, immutable-by-contract resource summaries for planning v4.

The catalog contains only reviewed references and semantic summaries. It must not
contain runtime values, SQL, browser locators, filesystem paths, or credentials.
Every item is implicitly scoped to the snapshot's target system and environment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import keyword
import math
import re
from typing import Any, Literal

try:  # Python 3.9 compatibility; the project still uses 3.10+ syntax elsewhere.
    from typing import TypeAlias
except ImportError:  # pragma: no cover - only exercised by older host interpreters
    TypeAlias = Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CatalogExecutorKind: TypeAlias = Literal[
    "stagehand_agent",
    "http_api",
    "database",
    "performance",
    "tcp_port",
]
CatalogStateEffect: TypeAlias = Literal["read_only", "creates_data", "changes_state"]

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
_WINDOWS_OR_UNC_PATH = re.compile(r"(?:^|[\s\"'])(?:[A-Za-z]:[\\/]|\\\\|file://)", re.IGNORECASE)
_UNIX_FILESYSTEM_PATH = re.compile(
    r"(?:^|[\s\"'])/(?:etc|home|mnt|opt|private|tmp|usr|var)(?:/|\b)",
    re.IGNORECASE,
)
_RAW_SQL_PATTERN = re.compile(
    r"(?:\bselect\s+.+?\s+from\b|\binsert\s+into\b|\bupdate\s+\S+\s+set\b|"
    r"\bdelete\s+from\b|\b(?:drop|alter|create|truncate)\s+(?:table|database)\b|"
    r"\bmerge\s+into\b|\bcall\s+[A-Za-z_][A-Za-z0-9_.]*\s*\()",
    re.IGNORECASE | re.DOTALL,
)
_LOCATOR_PATTERN = re.compile(
    r"(?:\b(?:css|xpath)\s*=|(?:^|\s)//[A-Za-z*]|\blocator\s*\(|"
    r"\bget_by_(?:role|text|testid|label)\s*\(|\[\s*data-testid\s*=)",
    re.IGNORECASE,
)
_SECRET_LITERAL_PATTERN = re.compile(
    r"(?:\b(?:password|passwd|secret|token|authorization|api[_ -]?key)\s*[:=]\s*"
    r"(?!\{[^}]+\}|<[^>]+>|policy\b|field\b|name\b|placeholder\b|format\b)[^\s,;]+"
    r"|\b(?:bearer)\s+[A-Za-z0-9._~+/=-]+"
    r"|\b(?:jdbc|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://"
    r"|\bsk-[A-Za-z0-9]{16,})",
    re.IGNORECASE,
)


def _reject_unsafe_text(value: str, field_name: str, *, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    if _SECRET_LITERAL_PATTERN.search(normalized):
        raise ValueError(f"{field_name} must not contain a secret value")
    if _WINDOWS_OR_UNC_PATH.search(normalized) or _UNIX_FILESYSTEM_PATH.search(normalized):
        raise ValueError(f"{field_name} must not contain an absolute filesystem path")
    if _RAW_SQL_PATTERN.search(normalized):
        raise ValueError(f"{field_name} must not contain raw SQL")
    if _LOCATOR_PATTERN.search(normalized):
        raise ValueError(f"{field_name} must not contain a browser locator")
    return normalized


def _require_ref(value: str, field_name: str) -> str:
    normalized = _reject_unsafe_text(value, field_name)
    path_like = normalized.replace("\\", "/")
    if (
        not _REF_PATTERN.fullmatch(normalized)
        or normalized.startswith(("/", "\\"))
        or "://" in normalized
        or any(part == ".." for part in path_like.split("/"))
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ValueError(f"{field_name} must be a safe catalog reference")
    return normalized


def _require_hash(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex digits>")
    return normalized


def _require_unique(values: Sequence[str], field_name: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"{field_name} contains duplicate refs: {duplicates}")


class StrictCatalogModel(BaseModel):
    """Base class shared by every externally supplied catalog object."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HttpObservable(StrictCatalogModel):
    observable_ref: str
    description: str
    kind: Literal["status", "json", "header", "body_contains", "text_contains"]
    path: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def validate_observable(self) -> "HttpObservable":
        _require_ref(self.observable_ref, "HttpObservable.observable_ref")
        _reject_unsafe_text(self.description, "HttpObservable.description")
        if self.kind == "json":
            if not self.path or not self.path.strip().startswith("$"):
                raise ValueError("HTTP json observable requires a JSON path starting with '$'")
            _reject_unsafe_text(self.path, "HttpObservable.path")
        elif self.path is not None:
            raise ValueError("HttpObservable.path is only valid for kind=json")
        if self.kind == "header":
            if not self.name:
                raise ValueError("HTTP header observable requires name")
            _reject_unsafe_text(self.name, "HttpObservable.name")
        elif self.name is not None:
            raise ValueError("HttpObservable.name is only valid for kind=header")
        return self


class HttpOperation(StrictCatalogModel):
    operation_ref: str
    description: str
    base_url_ref: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str
    state_effect: CatalogStateEffect
    allowed_binding_refs: list[str] = Field(default_factory=list)
    observables: list[HttpObservable] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation(self) -> "HttpOperation":
        _require_ref(self.operation_ref, "HttpOperation.operation_ref")
        _require_ref(self.base_url_ref, "HttpOperation.base_url_ref")
        _reject_unsafe_text(self.description, "HttpOperation.description")
        if (
            not self.path.startswith("/")
            or self.path.startswith("//")
            or "://" in self.path
            or "?" in self.path
            or "#" in self.path
            or any(part == ".." for part in self.path.split("/"))
        ):
            raise ValueError("HttpOperation.path must be a safe path relative to base_url_ref")
        _reject_unsafe_text(self.path, "HttpOperation.path")
        for ref in self.allowed_binding_refs:
            _require_ref(ref, "HttpOperation.allowed_binding_refs")
        _require_unique(self.allowed_binding_refs, "HttpOperation.allowed_binding_refs")
        _require_unique(
            [item.observable_ref for item in self.observables],
            "HttpOperation.observables",
        )
        return self


class DatabaseObservable(StrictCatalogModel):
    observable_ref: str
    description: str
    kind: Literal["row_count", "column", "exists"]
    column: str | None = None

    @model_validator(mode="after")
    def validate_observable(self) -> "DatabaseObservable":
        _require_ref(self.observable_ref, "DatabaseObservable.observable_ref")
        _reject_unsafe_text(self.description, "DatabaseObservable.description")
        if self.kind == "column":
            if not self.column:
                raise ValueError("Database column observable requires column")
            _reject_unsafe_text(self.column, "DatabaseObservable.column")
        elif self.column is not None:
            raise ValueError("DatabaseObservable.column is only valid for kind=column")
        return self


class DatabaseOperation(StrictCatalogModel):
    operation_ref: str
    description: str
    connection_profile_ref: str
    operation_kind: Literal["query"] = "query"
    execution_policy: Literal["read_only"] = "read_only"
    state_effect: Literal["read_only"] = "read_only"
    allowed_binding_refs: list[str] = Field(default_factory=list)
    observables: list[DatabaseObservable] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation(self) -> "DatabaseOperation":
        _require_ref(self.operation_ref, "DatabaseOperation.operation_ref")
        _require_ref(self.connection_profile_ref, "DatabaseOperation.connection_profile_ref")
        _reject_unsafe_text(self.description, "DatabaseOperation.description")
        for ref in self.allowed_binding_refs:
            _require_ref(ref, "DatabaseOperation.allowed_binding_refs")
        _require_unique(self.allowed_binding_refs, "DatabaseOperation.allowed_binding_refs")
        _require_unique(
            [item.observable_ref for item in self.observables],
            "DatabaseOperation.observables",
        )
        return self


class DatabaseColumn(StrictCatalogModel):
    name: str
    data_type: str
    description: str = ""

    @model_validator(mode="after")
    def validate_column(self) -> "DatabaseColumn":
        _require_ref(self.name, "DatabaseColumn.name")
        _reject_unsafe_text(self.data_type, "DatabaseColumn.data_type")
        if self.description:
            _reject_unsafe_text(
                self.description,
                "DatabaseColumn.description",
                allow_empty=True,
            )
        return self


class DatabaseTable(StrictCatalogModel):
    name: str
    description: str = ""
    columns: list[DatabaseColumn] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_table(self) -> "DatabaseTable":
        _require_ref(self.name, "DatabaseTable.name")
        if self.description:
            _reject_unsafe_text(
                self.description,
                "DatabaseTable.description",
                allow_empty=True,
            )
        _require_unique(
            [item.name for item in self.columns],
            "DatabaseTable.columns",
        )
        return self


class DatabaseSchema(StrictCatalogModel):
    connection_profile_ref: str
    dialect: str
    tables: list[DatabaseTable] = Field(min_length=1)
    allowed_parameter_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_schema(self) -> "DatabaseSchema":
        _require_ref(
            self.connection_profile_ref,
            "DatabaseSchema.connection_profile_ref",
        )
        _require_ref(self.dialect, "DatabaseSchema.dialect")
        _require_unique(
            [item.name for item in self.tables],
            "DatabaseSchema.tables",
        )
        for value in self.allowed_parameter_refs:
            _require_ref(value, "DatabaseSchema.allowed_parameter_refs")
        _require_unique(
            self.allowed_parameter_refs,
            "DatabaseSchema.allowed_parameter_refs",
        )
        return self


class PortObservable(StrictCatalogModel):
    """One reviewed observation for a registered TCP endpoint."""

    observable_ref: str
    description: str
    kind: Literal["state", "connect_latency_ms"]

    @model_validator(mode="after")
    def validate_observable(self) -> "PortObservable":
        _require_ref(self.observable_ref, "PortObservable.observable_ref")
        _reject_unsafe_text(self.description, "PortObservable.description")
        return self


class TcpPortProbe(StrictCatalogModel):
    """A single read-only host_ref + port; never a range or scanner."""

    probe_ref: str
    description: str
    host_ref: str
    port: int = Field(gt=0, le=65_535)
    timeout_seconds: float = Field(gt=0, le=30)
    state_effect: Literal["read_only"] = "read_only"
    observables: list[PortObservable] = Field(default_factory=list)

    @field_validator("port", mode="before")
    @classmethod
    def reject_boolean_port(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("TcpPortProbe.port 不能是 bool")
        return value

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def reject_boolean_timeout(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("TcpPortProbe.timeout_seconds 不能是 bool")
        return value

    @model_validator(mode="after")
    def validate_probe(self) -> "TcpPortProbe":
        _require_ref(self.probe_ref, "TcpPortProbe.probe_ref")
        _reject_unsafe_text(self.description, "TcpPortProbe.description")
        _require_ref(self.host_ref, "TcpPortProbe.host_ref")
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("TcpPortProbe.timeout_seconds must be finite")
        _require_unique(
            [item.observable_ref for item in self.observables],
            "TcpPortProbe.observables",
        )
        return self


class LoadStage(StrictCatalogModel):
    duration_seconds: float = Field(gt=0, le=86_400)
    virtual_users: int = Field(gt=0, le=1_000_000)

    @field_validator("duration_seconds", "virtual_users", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("load stage numeric values must not be booleans")
        return value

    @model_validator(mode="after")
    def require_finite_duration(self) -> "LoadStage":
        if not math.isfinite(self.duration_seconds):
            raise ValueError("LoadStage.duration_seconds must be finite")
        return self


class PerformanceObservable(StrictCatalogModel):
    observable_ref: str
    description: str
    metric: str
    unit: str | None = None
    percentile: str | None = None

    @model_validator(mode="after")
    def validate_observable(self) -> "PerformanceObservable":
        _require_ref(self.observable_ref, "PerformanceObservable.observable_ref")
        _require_ref(self.metric, "PerformanceObservable.metric")
        _reject_unsafe_text(self.description, "PerformanceObservable.description")
        if self.unit is not None:
            _reject_unsafe_text(self.unit, "PerformanceObservable.unit")
        if self.percentile is not None and not re.fullmatch(r"p(?:100|[1-9]?[0-9])(?:\.\d+)?", self.percentile):
            raise ValueError("PerformanceObservable.percentile must look like p95 or p99.9")
        return self


class PerformanceProfile(StrictCatalogModel):
    profile_ref: str
    description: str
    driver_ref: str
    state_effect: CatalogStateEffect
    max_duration_seconds: float = Field(gt=0, le=86_400)
    max_virtual_users: int = Field(gt=0, le=1_000_000)
    observables: list[PerformanceObservable]

    @field_validator("max_duration_seconds", "max_virtual_users", mode="before")
    @classmethod
    def reject_boolean_limits(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("performance profile limits must not be booleans")
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> "PerformanceProfile":
        _require_ref(self.profile_ref, "PerformanceProfile.profile_ref")
        _require_ref(self.driver_ref, "PerformanceProfile.driver_ref")
        _reject_unsafe_text(self.description, "PerformanceProfile.description")
        if not math.isfinite(self.max_duration_seconds):
            raise ValueError("PerformanceProfile.max_duration_seconds must be finite")
        if not self.observables:
            raise ValueError("PerformanceProfile requires at least one observable")
        _require_unique(
            [item.observable_ref for item in self.observables],
            "PerformanceProfile.observables",
        )
        return self


class AgentUiOperation(StrictCatalogModel):
    operation_ref: str
    description: str
    state_effect: CatalogStateEffect | Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_operation(self) -> "AgentUiOperation":
        _require_ref(self.operation_ref, "AgentUiOperation.operation_ref")
        _reject_unsafe_text(self.description, "AgentUiOperation.description")
        return self


class AgentUiObservable(StrictCatalogModel):
    observable_ref: str
    description: str

    @model_validator(mode="after")
    def validate_observable(self) -> "AgentUiObservable":
        _require_ref(self.observable_ref, "AgentUiObservable.observable_ref")
        _reject_unsafe_text(self.description, "AgentUiObservable.description")
        return self


class AgentUiCapabilityProfile(StrictCatalogModel):
    profile_ref: str
    start_url: str
    max_steps: int = Field(ge=1, le=200)
    operations: list[AgentUiOperation] = Field(min_length=1)
    observables: list[AgentUiObservable] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_profile(self) -> "AgentUiCapabilityProfile":
        from urllib.parse import urlsplit

        _require_ref(self.profile_ref, "AgentUiCapabilityProfile.profile_ref")
        parsed = urlsplit(self.start_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AgentUiCapabilityProfile.start_url 必须是绝对 HTTP(S) URL")
        _require_unique(
            [item.operation_ref for item in self.operations],
            "AgentUiCapabilityProfile.operations",
        )
        _require_unique(
            [item.observable_ref for item in self.observables],
            "AgentUiCapabilityProfile.observables",
        )
        return self


class DataBinding(StrictCatalogModel):
    binding_ref: str
    description: str
    executor_kind: CatalogExecutorKind
    operation_ref: str
    input_refs: dict[str, str]

    @model_validator(mode="after")
    def validate_binding(self) -> "DataBinding":
        _require_ref(self.binding_ref, "DataBinding.binding_ref")
        _require_ref(self.operation_ref, "DataBinding.operation_ref")
        _reject_unsafe_text(self.description, "DataBinding.description")
        if not self.input_refs:
            raise ValueError("DataBinding.input_refs must not be empty")
        for input_name, variable_ref in self.input_refs.items():
            _require_ref(input_name, "DataBinding.input_refs key")
            _require_ref(variable_ref, f"DataBinding.input_refs[{input_name}]")
        return self


class CleanupAction(StrictCatalogModel):
    action_ref: str
    description: str
    handler_kind: CatalogExecutorKind
    policy: str
    target: str | None = None
    always_run: bool
    evidence_required: bool
    required_data_slots: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_action(self) -> "CleanupAction":
        _require_ref(self.action_ref, "CleanupAction.action_ref")
        _require_ref(self.handler_kind, "CleanupAction.handler_kind")
        _require_ref(self.policy, "CleanupAction.policy")
        _reject_unsafe_text(self.description, "CleanupAction.description")
        for slot in self.required_data_slots:
            _require_ref(slot, "CleanupAction.required_data_slots")
            if not slot.isidentifier() or keyword.iskeyword(slot):
                raise ValueError(
                    "CleanupAction.required_data_slots 必须是安全的 Python 参数名"
                )
        _require_unique(
            self.required_data_slots,
            "CleanupAction.required_data_slots",
        )
        if self.target is not None:
            _reject_unsafe_text(self.target, "CleanupAction.target")
        return self


CatalogResource: TypeAlias = (
    HttpOperation
    | HttpObservable
    | DatabaseOperation
    | DatabaseObservable
    | TcpPortProbe
    | PortObservable
    | PerformanceProfile
    | PerformanceObservable
    | AgentUiCapabilityProfile
    | AgentUiOperation
    | AgentUiObservable
    | DataBinding
    | CleanupAction
)


class _PlanningCatalogContent(StrictCatalogModel):
    schema_version: Literal["planning-catalog.v4"] = "planning-catalog.v4"
    catalog_id: str
    system_id: str
    environment: str
    available_executors: list[CatalogExecutorKind]
    http_operations: list[HttpOperation] = Field(default_factory=list)
    database_operations: list[DatabaseOperation] = Field(default_factory=list)
    database_schema: DatabaseSchema | None = None
    tcp_port_probes: list[TcpPortProbe] = Field(default_factory=list)
    performance_profiles: list[PerformanceProfile] = Field(default_factory=list)
    agent_ui_profiles: list[AgentUiCapabilityProfile] = Field(default_factory=list)
    data_bindings: list[DataBinding] = Field(default_factory=list)
    cleanup_actions: list[CleanupAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_catalog_content(self) -> "_PlanningCatalogContent":
        _require_ref(self.catalog_id, "PlanningCatalogSnapshot.catalog_id")
        _require_ref(self.system_id, "PlanningCatalogSnapshot.system_id")
        _require_ref(self.environment, "PlanningCatalogSnapshot.environment")
        if not self.available_executors:
            raise ValueError("PlanningCatalogSnapshot.available_executors must not be empty")
        _require_unique(self.available_executors, "PlanningCatalogSnapshot.available_executors")

        available = set(self.available_executors)
        if "http_api" in available and not self.http_operations:
            raise ValueError("http_api is available but http_operations is empty")
        if (
            "database" in available
            and not self.database_operations
            and self.database_schema is None
        ):
            raise ValueError(
                "database executor is available but database operations/schema are empty"
            )
        if "tcp_port" in available and not self.tcp_port_probes:
            raise ValueError("tcp_port executor is available but tcp_port_probes is empty")
        if "performance" in available and not self.performance_profiles:
            raise ValueError("performance is available but performance_profiles is empty")
        if "stagehand_agent" in available and not self.agent_ui_profiles:
            raise ValueError("stagehand_agent is available but agent_ui_profiles is empty")

        definitions: list[tuple[str, str]] = []
        definitions.extend((item.operation_ref, "http operation") for item in self.http_operations)
        definitions.extend((item.operation_ref, "database operation") for item in self.database_operations)
        definitions.extend((item.probe_ref, "tcp port probe") for item in self.tcp_port_probes)
        definitions.extend((item.profile_ref, "performance profile") for item in self.performance_profiles)
        definitions.extend((item.profile_ref, "agent UI profile") for item in self.agent_ui_profiles)
        definitions.extend((item.binding_ref, "data binding") for item in self.data_bindings)
        definitions.extend((item.action_ref, "cleanup action") for item in self.cleanup_actions)
        for operation in self.http_operations:
            definitions.extend((item.observable_ref, "http observable") for item in operation.observables)
        for operation in self.database_operations:
            definitions.extend((item.observable_ref, "database observable") for item in operation.observables)
        for probe in self.tcp_port_probes:
            definitions.extend((item.observable_ref, "tcp port observable") for item in probe.observables)
        for profile in self.performance_profiles:
            definitions.extend((item.observable_ref, "performance observable") for item in profile.observables)
        for profile in self.agent_ui_profiles:
            definitions.extend((item.operation_ref, "agent UI operation") for item in profile.operations)
            definitions.extend((item.observable_ref, "agent UI observable") for item in profile.observables)

        seen: dict[str, str] = {}
        for ref, domain in definitions:
            previous = seen.get(ref)
            if previous is not None:
                raise ValueError(f"catalog ref {ref!r} is defined by both {previous} and {domain}")
            seen[ref] = domain

        bindings = {item.binding_ref: item for item in self.data_bindings}
        http_operations = {item.operation_ref: item for item in self.http_operations}
        database_operations = {item.operation_ref: item for item in self.database_operations}
        endpoint_keys = [(item.host_ref, item.port) for item in self.tcp_port_probes]
        if len(set(endpoint_keys)) != len(endpoint_keys):
            raise ValueError("tcp_port_probes 不能重复登记同一个 host_ref + port")
        performance_profiles = {item.profile_ref: item for item in self.performance_profiles}
        cleanup_actions = {item.action_ref: item for item in self.cleanup_actions}

        for operation in self.http_operations:
            self._validate_allowed_bindings(
                operation.operation_ref,
                "http_api",
                operation.allowed_binding_refs,
                bindings,
            )
        for operation in self.database_operations:
            for ref in operation.allowed_binding_refs:
                binding = bindings.get(ref)
                if binding is None:
                    raise ValueError(f"database operation references unknown binding_ref: {ref}")
                if (
                    binding.executor_kind != "database"
                    or binding.operation_ref != operation.operation_ref
                ):
                    raise ValueError(
                        f"binding_ref {ref} does not belong to database operation "
                        f"{operation.operation_ref}"
                    )
        for binding in self.data_bindings:
            cleanup_action = cleanup_actions.get(binding.operation_ref)
            if cleanup_action is not None:
                if binding.executor_kind != cleanup_action.handler_kind:
                    raise ValueError(
                        f"cleanup DataBinding {binding.binding_ref} executor_kind "
                        f"does not match {cleanup_action.action_ref}.handler_kind"
                    )
                unknown_slots = set(binding.input_refs) - set(
                    cleanup_action.required_data_slots
                )
                if unknown_slots:
                    raise ValueError(
                        f"cleanup DataBinding {binding.binding_ref} contains unknown "
                        f"data slots: {sorted(unknown_slots)}"
                    )
                continue
            for input_name in binding.input_refs:
                if binding.executor_kind == "http_api" and not (
                    input_name in {"body", "body_ref", "headers", "headers_ref"}
                    or input_name.startswith("query.")
                    or input_name.startswith("path.")
                ):
                    raise ValueError(
                        "HTTP input slot 必须是 body/headers/query.<name>/path.<name>"
                    )
                if binding.executor_kind == "database" and not input_name.startswith(
                    "param."
                ):
                    raise ValueError("database input slot 必须是 param.<name>")
                if binding.executor_kind == "performance" and not input_name.startswith(
                    "input."
                ):
                    raise ValueError("UI/性能输入项必须是 input.<name>")
            if binding.executor_kind == "http_api":
                target = http_operations.get(binding.operation_ref)
                allowed = target.allowed_binding_refs if target else []
            elif binding.executor_kind == "database":
                target = database_operations.get(binding.operation_ref)
                allowed = target.allowed_binding_refs if target else []
            elif binding.executor_kind == "tcp_port":
                raise ValueError(
                    "tcp_port 不接受动态 DataBinding；host/port 必须固定在 catalog probe"
                )
            else:
                target = performance_profiles.get(binding.operation_ref)
                allowed = [binding.binding_ref] if target else []
            if target is None:
                raise ValueError(
                    f"DataBinding {binding.binding_ref} references unknown operation/profile: {binding.operation_ref}"
                )
            if binding.binding_ref not in allowed:
                raise ValueError(
                    f"DataBinding {binding.binding_ref} is not allowed by {binding.operation_ref}"
                )
        for action in self.cleanup_actions:
            bound_slots = {
                slot
                for binding in self.data_bindings
                if binding.operation_ref == action.action_ref
                for slot in binding.input_refs
            }
            missing_slots = set(action.required_data_slots) - bound_slots
            if missing_slots:
                raise ValueError(
                    f"CleanupAction {action.action_ref} required_data_slots have no "
                    f"catalog DataBinding: {sorted(missing_slots)}"
        )
        return self

    @staticmethod
    def _validate_allowed_bindings(
        operation_ref: str,
        executor_kind: str,
        allowed_refs: list[str],
        bindings: dict[str, DataBinding],
    ) -> None:
        for ref in allowed_refs:
            binding = bindings.get(ref)
            if binding is None:
                raise ValueError(f"{operation_ref} references unknown binding_ref: {ref}")
            if binding.executor_kind != executor_kind or binding.operation_ref != operation_ref:
                raise ValueError(f"binding_ref {ref} does not belong to {operation_ref}")


class PlanningCatalogSnapshot(_PlanningCatalogContent):
    """One canonical, target-scoped snapshot consumed by the v4 planner."""

    content_hash: str

    @model_validator(mode="after")
    def validate_content_hash(self) -> "PlanningCatalogSnapshot":
        _require_hash(self.content_hash, "PlanningCatalogSnapshot.content_hash")
        expected = compute_catalog_content_hash(self)
        if self.content_hash != expected:
            raise ValueError("PlanningCatalogSnapshot.content_hash does not match canonical catalog content")
        return self

    @classmethod
    def build(cls, **content: Any) -> "PlanningCatalogSnapshot":
        """Validate catalog content, calculate its hash, and build the snapshot."""

        content.pop("content_hash", None)
        normalized = _PlanningCatalogContent.model_validate(content).model_dump(mode="json")
        normalized["content_hash"] = compute_catalog_content_hash(normalized)
        return cls.model_validate(normalized)

    def computed_content_hash(self) -> str:
        return compute_catalog_content_hash(self)

    def matches_target(self, system_id: str, environment: str) -> bool:
        return self.system_id == system_id and self.environment == environment

    def require_target(self, system_id: str, environment: str) -> None:
        if not self.matches_target(system_id, environment):
            raise ValueError(
                "planning catalog target does not match the requested system/environment"
            )

    def get_http_operation(self, ref: str) -> HttpOperation | None:
        return next((item for item in self.http_operations if item.operation_ref == ref), None)

    def get_database_operation(self, ref: str) -> DatabaseOperation | None:
        return next((item for item in self.database_operations if item.operation_ref == ref), None)

    def get_database_schema(self) -> DatabaseSchema | None:
        return self.database_schema

    def get_tcp_port_probe(self, ref: str) -> TcpPortProbe | None:
        return next((item for item in self.tcp_port_probes if item.probe_ref == ref), None)

    def get_performance_profile(self, ref: str) -> PerformanceProfile | None:
        return next((item for item in self.performance_profiles if item.profile_ref == ref), None)

    def get_agent_ui_profile(self, ref: str) -> AgentUiCapabilityProfile | None:
        return next((item for item in self.agent_ui_profiles if item.profile_ref == ref), None)

    def get_agent_ui_operation(self, ref: str) -> AgentUiOperation | None:
        return next(
            (
                item
                for profile in self.agent_ui_profiles
                for item in profile.operations
                if item.operation_ref == ref
            ),
            None,
        )

    def get_data_binding(self, ref: str) -> DataBinding | None:
        return next((item for item in self.data_bindings if item.binding_ref == ref), None)

    def get_cleanup_action(self, ref: str) -> CleanupAction | None:
        return next((item for item in self.cleanup_actions if item.action_ref == ref), None)

    def get_observable(
        self,
        ref: str,
    ) -> HttpObservable | DatabaseObservable | PortObservable | PerformanceObservable | AgentUiObservable | None:
        values: list[
            HttpObservable | DatabaseObservable | PortObservable | PerformanceObservable | AgentUiObservable
        ] = []
        values.extend(item for operation in self.http_operations for item in operation.observables)
        values.extend(item for operation in self.database_operations for item in operation.observables)
        values.extend(item for probe in self.tcp_port_probes for item in probe.observables)
        values.extend(item for profile in self.performance_profiles for item in profile.observables)
        values.extend(item for profile in self.agent_ui_profiles for item in profile.observables)
        return next((item for item in values if item.observable_ref == ref), None)

    def get_ref(self, ref: str) -> CatalogResource | None:
        primary: list[CatalogResource] = [
            *self.http_operations,
            *self.database_operations,
            *self.tcp_port_probes,
            *self.performance_profiles,
            *self.agent_ui_profiles,
            *self.data_bindings,
            *self.cleanup_actions,
        ]
        for item in primary:
            candidate = (
                getattr(item, "operation_ref", None)
                or getattr(item, "probe_ref", None)
                or getattr(item, "profile_ref", None)
                or getattr(item, "binding_ref", None)
                or getattr(item, "action_ref", None)
            )
            if candidate == ref:
                return item
        return self.get_observable(ref)


def compute_catalog_content_hash(value: PlanningCatalogSnapshot | Mapping[str, Any] | BaseModel) -> str:
    """Hash canonical content without invalidating older optional fields.

    Optional fields added to the v4 schema after a snapshot was approved are
    deliberately excluded when they were absent in that stored snapshot.
    """

    stored_hash = None
    if isinstance(value, BaseModel):
        stored_hash = getattr(value, "content_hash", None)
        payload = value.model_dump(mode="json", exclude={"content_hash"})
    elif isinstance(value, Mapping):
        payload = dict(value)
        stored_hash = payload.pop("content_hash", None)
    else:  # pragma: no cover - protected by the public type contract
        raise TypeError("catalog hash input must be a model or mapping")
    normalized = _PlanningCatalogContent.model_validate(payload).model_dump(mode="json")
    if normalized.get("database_schema") is None:
        normalized.pop("database_schema", None)
    def digest(content: dict[str, Any]) -> str:
        canonical = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    current_hash = digest(normalized)
    compatibility = json.loads(json.dumps(normalized, ensure_ascii=False))
    compatibility_hash = digest(compatibility)
    if stored_hash == compatibility_hash:
        return compatibility_hash
    return current_hash


__all__ = [
    "AgentUiCapabilityProfile",
    "AgentUiObservable",
    "AgentUiOperation",
    "CatalogExecutorKind",
    "CatalogStateEffect",
    "CleanupAction",
    "DataBinding",
    "DatabaseColumn",
    "DatabaseObservable",
    "DatabaseOperation",
    "DatabaseSchema",
    "DatabaseTable",
    "HttpObservable",
    "HttpOperation",
    "LoadStage",
    "PerformanceObservable",
    "PerformanceProfile",
    "PlanningCatalogSnapshot",
    "PortObservable",
    "TcpPortProbe",
    "compute_catalog_content_hash",
]
