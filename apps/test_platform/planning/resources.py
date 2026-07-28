"""Resolve simple test-resource inputs into the layer-two planning snapshot.

Users configure real resources.  This module is the only boundary that turns
those inputs into ``PlanningCatalogSnapshot`` references.  It does not approve a
plan and it does not execute any test action.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from apps.test_platform.intent.contracts import (
    ApprovedTestDesignBundle,
    contains_secret_value,
)
from apps.test_platform.ingestion.adapters import parse_input_file
from apps.test_platform.ingestion.contracts import (
    IngestionError,
    IngestionLimits,
    InputFile,
)
from .catalogs import PlanningCatalogSnapshot
from .resource_normalization import (
    NormalizedResourceDraft,
    normalize_resource_sources,
)


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass(frozen=True)
class ResolvedTestResources:
    catalog: PlanningCatalogSnapshot
    runtime_config: dict[str, Any]
    runtime_config_hash: str


def runtime_config_content_hash(value: Mapping[str, Any]) -> str:
    """Bind non-secret execution routing/configuration without storing credentials."""

    if not isinstance(value, Mapping):
        raise ValueError("runtime config 必须是对象")
    if contains_secret_value(value):
        raise ValueError("runtime config 不能包含凭据实际值")
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime config 必须是规范 JSON 数据") from exc
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_ref(value: str, *, prefix: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:@/-]+", "-", str(value or "").strip())
    normalized = normalized.strip("-./")
    if not normalized:
        normalized = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}.{normalized}"


def _read_field_bytes(field, *, label: str) -> bytes:
    is_stored_field = bool(getattr(field, "_committed", False))
    field.open("rb")
    try:
        raw = field.read(5 * 1024 * 1024 + 1)
    finally:
        if is_stored_field:
            field.close()
        else:
            field.seek(0)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError(f"{label}不能超过 5 MiB")
    return raw


def _read_structured(field, *, label: str) -> dict[str, Any]:
    raw = _read_field_bytes(field, label=label)
    try:
        value = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"{label}不是有效的 UTF-8 JSON/YAML") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}顶层必须是对象")
    if contains_secret_value(value):
        raise ValueError(f"{label}包含疑似凭据实际值")
    return value


def _read_asset_text(field, *, label: str) -> str:
    raw = _read_field_bytes(field, label=label)
    filename = Path(str(getattr(field, "name", "resource.txt"))).name
    limits = IngestionLimits(max_file_bytes=5 * 1024 * 1024).validate()
    try:
        parsed = parse_input_file(InputFile(filename=filename, data=raw), limits)
    except IngestionError:
        parsed = None
    if parsed is not None:
        content = "\n\n".join(
            str(value).strip()
            for _, value in parsed.requirements
            if str(value).strip()
        )
        if content:
            if contains_secret_value(content):
                raise ValueError(f"{label}包含疑似凭据实际值")
            return content
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            content = raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if content:
            if contains_secret_value(content):
                raise ValueError(f"{label}包含疑似凭据实际值")
            return content
    raise ValueError(f"{label}无法提取可供模型读取的文字")


def _structured_if(field, predicate) -> dict[str, Any] | None:
    if not field:
        return None
    try:
        value = _read_structured(field, label="测试资源文件")
    except ValueError:
        return None
    return value if predicate(value) else None


def _resource_source_hash(profile) -> str:
    digest = hashlib.sha256()
    for name in (
        "ui_agent_asset_file",
        "api_openapi_file",
        "database_query_file",
        "performance_profile_file",
    ):
        field = getattr(profile, name, None)
        digest.update(name.encode("ascii"))
        if field:
            digest.update(_read_field_bytes(field, label="测试资源文件"))
    for name in (
        "ui_agent_asset_text",
        "api_asset_text",
        "api_base_url",
        "database_asset_text",
        "database_connection_ref",
        "performance_asset_text",
    ):
        digest.update(name.encode("ascii"))
        digest.update(str(getattr(profile, name, "") or "").strip().encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _resolve_api(profile):
    document = _read_structured(profile.api_openapi_file, label="OpenAPI 文件")
    if not str(document.get("openapi") or document.get("swagger") or "").strip():
        raise ValueError("OpenAPI 文件缺少 openapi/swagger 版本")
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError("OpenAPI paths 必须是对象")
    operations: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    performance_targets: dict[str, dict[str, str]] = {}
    for path, path_item in paths.items():
        if not isinstance(path_item, Mapping):
            continue
        shared_parameters = path_item.get("parameters")
        if shared_parameters is None:
            shared_parameters = []
        if not isinstance(shared_parameters, list):
            raise ValueError(f"OpenAPI path {path} 的 parameters 必须是数组")
        for method, raw in path_item.items():
            if str(method).lower() not in _HTTP_METHODS or not isinstance(raw, Mapping):
                continue
            identity = str(raw.get("operationId") or f"{method}-{path}")
            operation_ref = _safe_ref(identity, prefix="api.operation")
            binding_ref = _safe_ref(identity, prefix="api.binding")
            input_refs: dict[str, str] = {}
            operation_parameters = raw.get("parameters")
            if operation_parameters is None:
                operation_parameters = []
            if not isinstance(operation_parameters, list):
                raise ValueError(
                    f"OpenAPI operation {identity} 的 parameters 必须是数组"
                )
            for parameter in [*shared_parameters, *operation_parameters]:
                if not isinstance(parameter, Mapping) or "$ref" in parameter:
                    continue
                location = str(parameter.get("in") or "")
                name = str(parameter.get("name") or "").strip()
                if location in {"path", "query"} and name:
                    input_refs[f"{location}.{name}"] = _safe_ref(
                        name,
                        prefix=f"runtime.api.{identity}",
                    )
            if raw.get("requestBody") is not None:
                input_refs["body"] = _safe_ref(identity, prefix="runtime.api.body")
            allowed = [binding_ref] if input_refs else []
            operations.append(
                {
                    "operation_ref": operation_ref,
                    "description": str(raw.get("summary") or raw.get("description") or identity),
                    "base_url_ref": "runtime.api.base",
                    "method": str(method).upper(),
                    "path": str(path),
                    "state_effect": (
                        "read_only"
                        if str(method).lower() in {"get", "head", "options"}
                        else "changes_state"
                    ),
                    "allowed_binding_refs": allowed,
                    "observables": [
                        {
                            "observable_ref": _safe_ref(identity, prefix="api.status"),
                            "description": "HTTP 响应状态码",
                            "kind": "status",
                        },
                        {
                            "observable_ref": _safe_ref(identity, prefix="api.body"),
                            "description": "HTTP 响应正文包含期望内容",
                            "kind": "body_contains",
                        },
                    ],
                }
            )
            performance_targets[identity] = {
                "method": str(method).upper(),
                "url": str(profile.api_base_url).rstrip("/")
                + "/"
                + str(path).lstrip("/"),
            }
            if input_refs:
                bindings.append(
                    {
                        "binding_ref": binding_ref,
                        "description": f"{identity} 的 OpenAPI 输入",
                        "executor_kind": "http_api",
                        "operation_ref": operation_ref,
                        "input_refs": input_refs,
                    }
                )
    if not operations:
        raise ValueError("OpenAPI 文件中没有可执行 operation")
    return (
        operations,
        bindings,
        {"base_urls": {"runtime.api.base": str(profile.api_base_url)}},
        performance_targets,
    )


def _resolve_database(profile):
    document = _read_structured(
        profile.database_query_file,
        label="数据库访问策略",
    )
    if document.get("schema_version") != "database-access-policy.v1":
        raise ValueError(
            "数据库资源 schema_version 必须是 database-access-policy.v1"
        )
    unknown = set(document) - {"schema_version", "database_schema"}
    if unknown:
        raise ValueError(
            "数据库访问策略只能包含 database_schema；历史 SQL 及用途应保存到业务知识库"
        )
    raw_schema = document.get("database_schema")
    if not isinstance(raw_schema, Mapping):
        raise ValueError("数据库访问策略必须包含 database_schema 对象")
    raw_tables = raw_schema.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise ValueError("database_schema.tables 必须是非空数组")
    database_schema = {
        "connection_profile_ref": str(profile.database_connection_ref),
        "dialect": str(raw_schema.get("dialect") or "generic"),
        "tables": raw_tables,
        "allowed_parameter_refs": list(
            raw_schema.get("allowed_parameter_refs") or []
        ),
    }
    runtime_values = {
        "database_schemas": {
            str(profile.database_connection_ref): database_schema
        }
    }
    return (
        [],
        [],
        runtime_values,
        database_schema,
    )


def _absolute_http_url(value: Any, *, label: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or "{" in url
        or "}" in url
    ):
        raise ValueError(f"{label}必须是无凭据、无路径变量的绝对 HTTP URL")
    return url


def _resolve_performance(profile, api_targets: Mapping[str, Mapping[str, str]]):
    document = _read_structured(profile.performance_profile_file, label="性能配置文件")
    if document.get("schema_version") != "performance-profile-set.v1":
        raise ValueError("性能配置 schema_version 必须是 performance-profile-set.v1")
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("性能配置文件没有 profiles")
    runtime_profiles: dict[str, dict[str, Any]] = {}
    for item in profiles:
        if not isinstance(item, Mapping) or not item.get("profile_ref"):
            continue
        profile_ref = str(item["profile_ref"])
        runtime_value = item.get("runtime")
        if runtime_value is None:
            runtime_value = {}
        if not isinstance(runtime_value, Mapping):
            raise ValueError(f"性能配置 {profile_ref}.runtime 必须是对象")
        driver_ref = str(item.get("driver_ref") or "")
        if driver_ref != "driver.http":
            runtime_profiles[profile_ref] = dict(runtime_value)
            continue
        allowed = {"target_operation_id", "target_url"}
        unknown = set(runtime_value) - allowed
        if unknown:
            raise ValueError(
                f"性能配置 {profile_ref}.runtime 包含未知字段: "
                + ", ".join(sorted(str(value) for value in unknown))
            )
        selected = [name for name in allowed if runtime_value.get(name)]
        if len(selected) != 1:
            raise ValueError(
                f"性能配置 {profile_ref} 必须且只能填写 target_operation_id 或 target_url"
            )
        if selected[0] == "target_url":
            target_url = _absolute_http_url(
                runtime_value["target_url"],
                label=f"性能配置 {profile_ref}.target_url",
            )
        else:
            operation_id = str(runtime_value["target_operation_id"]).strip()
            target = api_targets.get(operation_id)
            if target is None:
                raise ValueError(
                    f"性能配置 {profile_ref} 引用了不存在的 OpenAPI operationId: {operation_id}"
                )
            if str(target.get("method")) != "GET":
                raise ValueError(
                    f"内置 HTTP 性能驱动只支持 GET operation: {operation_id}"
                )
            target_url = _absolute_http_url(
                target.get("url"),
                label=f"OpenAPI operation {operation_id}",
            )
        runtime_profiles[profile_ref] = {"inputs": {"url": target_url}}
    runtime = {"performance_profiles": runtime_profiles}
    catalog_profiles = [
        {key: value for key, value in item.items() if key != "runtime"}
        for item in profiles
        if isinstance(item, Mapping)
    ]
    return catalog_profiles, runtime


def _compile_normalized_resources(
    profile,
    draft: NormalizedResourceDraft,
    api_targets: Mapping[str, Mapping[str, str]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, str]],
]:
    """Assign stable refs and runtime bindings to the model's semantic draft."""

    agent_ui_profiles: list[dict[str, Any]] = []
    for index, item in enumerate(draft.agent_ui_profiles, start=1):
        identity = f"{urlsplit(item.url).netloc}-{index}"
        agent_ui_profiles.append(
            {
                "profile_ref": _safe_ref(identity, prefix="ui.agent.profile"),
                "start_url": item.url,
                "max_steps": item.max_steps,
                "operations": [
                    {
                        "operation_ref": _safe_ref(
                            f"{identity}-{feature_index}",
                            prefix="ui.agent.operation",
                        ),
                        "description": feature,
                        "state_effect": "unknown",
                    }
                    for feature_index, feature in enumerate(item.features, start=1)
                ],
                "observables": [
                    {
                        "observable_ref": _safe_ref(
                            identity,
                            prefix="ui.agent.observable",
                        ),
                        "description": "由网页 Agent 根据已审批 Check 判断页面结果",
                    }
                ],
            }
        )

    operations: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    normalized_targets = dict(api_targets)
    for index, item in enumerate(draft.api_operations, start=1):
        identity = item.name or f"operation-{index}"
        operation_ref = _safe_ref(identity, prefix="api.operation")
        binding_ref = _safe_ref(identity, prefix="api.binding")
        input_refs: dict[str, str] = {}
        for parameter in item.parameters:
            slot = "body" if parameter.location == "body" else f"{parameter.location}.{parameter.name}"
            input_refs[slot] = _safe_ref(
                parameter.name,
                prefix=f"runtime.api.{identity}",
            )
        operations.append(
            {
                "operation_ref": operation_ref,
                "description": item.description,
                "base_url_ref": "runtime.api.base",
                "method": item.method,
                "path": item.path,
                "state_effect": (
                    "read_only"
                    if item.method in {"GET", "HEAD", "OPTIONS"}
                    else "changes_state"
                ),
                "allowed_binding_refs": [binding_ref] if input_refs else [],
                "observables": [
                    {
                        "observable_ref": _safe_ref(identity, prefix="api.status"),
                        "description": "HTTP 响应状态码",
                        "kind": "status",
                    },
                    {
                        "observable_ref": _safe_ref(identity, prefix="api.body"),
                        "description": "HTTP 响应正文包含期望内容",
                        "kind": "body_contains",
                    },
                ],
            }
        )
        if input_refs:
            bindings.append(
                {
                    "binding_ref": binding_ref,
                    "description": f"{item.description} 的运行输入",
                    "executor_kind": "http_api",
                    "operation_ref": operation_ref,
                    "input_refs": input_refs,
                }
            )
        normalized_targets[identity] = {
            "method": item.method,
            "url": str(profile.api_base_url).rstrip("/") + "/" + item.path.lstrip("/"),
        }

    database_schema = None
    if draft.database_schema is not None:
        database_schema = {
            "connection_profile_ref": str(profile.database_connection_ref),
            "dialect": draft.database_schema.dialect,
            "tables": [item.model_dump(mode="json") for item in draft.database_schema.tables],
            "allowed_parameter_refs": [
                _safe_ref(name, prefix="runtime.db")
                for name in draft.database_schema.allowed_parameter_names
            ],
        }

    performance_profiles: list[dict[str, Any]] = []
    runtime_profiles: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(draft.performance_profiles, start=1):
        identity = item.name or f"profile-{index}"
        profile_ref = _safe_ref(identity, prefix="perf")
        if item.target_api_operation:
            target = normalized_targets.get(item.target_api_operation)
            if target is None:
                raise ValueError(
                    "性能资料引用了不存在的接口操作: " + item.target_api_operation
                )
            if str(target.get("method")) != "GET":
                raise ValueError("内置 HTTP 性能驱动只支持 GET 接口")
            target_url = _absolute_http_url(
                target.get("url"),
                label=f"性能配置 {identity}",
            )
        else:
            target_url = _absolute_http_url(
                item.target_url,
                label=f"性能配置 {identity}",
            )
        performance_profiles.append(
            {
                "profile_ref": profile_ref,
                "description": item.description,
                "driver_ref": "driver.http",
                "state_effect": "read_only",
                "max_duration_seconds": item.max_duration_seconds,
                "max_virtual_users": item.max_virtual_users,
                "observables": [
                    {
                        "observable_ref": _safe_ref(
                            f"{identity}.{metric.metric}.{metric.percentile or 'value'}",
                            prefix="observable.perf",
                        ),
                        "description": metric.description,
                        "metric": metric.metric,
                        "unit": metric.unit,
                        "percentile": metric.percentile,
                    }
                    for metric in item.metrics
                ],
            }
        )
        runtime_profiles[profile_ref] = {"inputs": {"url": target_url}}

    runtime: dict[str, Any] = {}
    if operations:
        runtime["base_urls"] = {"runtime.api.base": str(profile.api_base_url)}
    if database_schema is not None:
        runtime["database_schemas"] = {
            str(profile.database_connection_ref): database_schema
        }
    if performance_profiles:
        runtime["performance_profiles"] = runtime_profiles
    return (
        agent_ui_profiles,
        operations,
        bindings,
        database_schema,
        performance_profiles,
        runtime,
        normalized_targets,
    )


def _loose_resource_sources(profile) -> tuple[dict[str, str], dict[str, bool]]:
    sources: dict[str, str] = {}
    strict = {"api": False, "database": False, "performance": False}
    ui_agent_parts: list[str] = []
    ui_agent_text = str(getattr(profile, "ui_agent_asset_text", "") or "").strip()
    ui_agent_file = getattr(profile, "ui_agent_asset_file", None)
    if ui_agent_text:
        if contains_secret_value(ui_agent_text):
            raise ValueError("网页 Agent 测试资源说明包含疑似凭据实际值")
        ui_agent_parts.append(ui_agent_text)
    if ui_agent_file:
        ui_agent_parts.append(_read_asset_text(ui_agent_file, label="网页 Agent 测试资源文件"))
    if ui_agent_parts:
        sources["ui_agent"] = "\n\n".join(ui_agent_parts)
    definitions = (
        (
            "api",
            "api_openapi_file",
            "api_asset_text",
            lambda value: bool(value.get("openapi") or value.get("swagger")),
        ),
        (
            "database",
            "database_query_file",
            "database_asset_text",
            lambda value: value.get("schema_version") == "database-access-policy.v1",
        ),
        (
            "performance",
            "performance_profile_file",
            "performance_asset_text",
            lambda value: value.get("schema_version") == "performance-profile-set.v1",
        ),
    )
    for channel, file_name, text_name, predicate in definitions:
        field = getattr(profile, file_name, None)
        text = str(getattr(profile, text_name, "") or "").strip()
        canonical = _structured_if(field, predicate) if field else None
        if canonical is not None:
            strict[channel] = True
            continue
        parts = []
        if text:
            if contains_secret_value(text):
                raise ValueError(f"{channel} 测试资源说明包含疑似凭据实际值")
            parts.append(text)
        if field:
            parts.append(_read_asset_text(field, label=f"{channel} 测试资源文件"))
        if parts:
            sources[channel] = "\n\n".join(parts)
    return sources, strict


def _normalized_draft(profile, sources: dict[str, str], gateway) -> NormalizedResourceDraft:
    source_hash = _resource_source_hash(profile)
    cached = dict(getattr(profile, "normalized_resource_data", {}) or {})
    cached_hash = str(
        getattr(profile, "normalized_resource_source_hash", "") or ""
    )
    if cached and cached_hash == source_hash:
        return NormalizedResourceDraft.model_validate(cached)
    if gateway is None:
        raise ValueError("测试资源资料尚未由模型整理，请重新生成执行计划")
    draft = normalize_resource_sources(gateway, sources)
    if getattr(profile, "pk", None):
        profile.__class__.objects.filter(pk=profile.pk).update(
            normalized_resource_data=draft.model_dump(mode="json"),
            normalized_resource_source_hash=source_hash,
        )
        profile.normalized_resource_data = draft.model_dump(mode="json")
        profile.normalized_resource_source_hash = source_hash
    return draft


def _merge_runtime(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping):
            merged = dict(target.get(key) or {})
            merged.update(value)
            target[key] = merged
        else:
            target[key] = value


def validate_non_ui_resource_files(profile) -> None:
    """Validate uploaded executable definitions before a resource row is saved."""

    content: dict[str, Any] = {
        "schema_version": "planning-catalog.v4",
        "catalog_id": "catalog.resource.form-validation",
        "system_id": str(profile.system_id or "form-validation"),
        "environment": str(profile.environment or "test"),
        "available_executors": [],
        "http_operations": [],
        "database_operations": [],
        "database_schema": None,
        "tcp_port_probes": [],
        "performance_profiles": [],
        "agent_ui_profiles": [],
        "data_bindings": [],
        "cleanup_actions": [],
    }
    api_targets: dict[str, dict[str, str]] = {}
    if profile.api_openapi_file:
        operations, bindings, _, api_targets = _resolve_api(profile)
        content["available_executors"].append("http_api")
        content["http_operations"].extend(operations)
        content["data_bindings"].extend(bindings)
    if profile.database_query_file:
        operations, bindings, _, database_schema = _resolve_database(profile)
        content["available_executors"].append("database")
        content["database_operations"].extend(operations)
        content["database_schema"] = database_schema
        content["data_bindings"].extend(bindings)
    if profile.performance_profile_file:
        profiles, _ = _resolve_performance(profile, api_targets)
        content["available_executors"].append("performance")
        content["performance_profiles"].extend(profiles)
    if content["available_executors"]:
        PlanningCatalogSnapshot.build(**content)


def validate_resource_source_inputs(profile) -> None:
    """Accept readable loose material while validating recognized formal formats."""

    _, strict = _loose_resource_sources(profile)
    content: dict[str, Any] = {
        "schema_version": "planning-catalog.v4",
        "catalog_id": "catalog.resource.form-validation",
        "system_id": str(profile.system_id or "form-validation"),
        "environment": str(profile.environment or "test"),
        "available_executors": [],
        "http_operations": [],
        "database_operations": [],
        "database_schema": None,
        "tcp_port_probes": [],
        "performance_profiles": [],
        "agent_ui_profiles": [],
        "data_bindings": [],
        "cleanup_actions": [],
    }
    api_targets: dict[str, dict[str, str]] = {}
    if strict["api"]:
        operations, bindings, _, api_targets = _resolve_api(profile)
        content["available_executors"].append("http_api")
        content["http_operations"].extend(operations)
        content["data_bindings"].extend(bindings)
    if strict["database"]:
        operations, bindings, _, database_schema = _resolve_database(profile)
        content["available_executors"].append("database")
        content["database_operations"].extend(operations)
        content["database_schema"] = database_schema
        content["data_bindings"].extend(bindings)
    if strict["performance"]:
        profiles, _ = _resolve_performance(profile, api_targets)
        content["available_executors"].append("performance")
        content["performance_profiles"].extend(profiles)
    if content["available_executors"]:
        PlanningCatalogSnapshot.build(**content)


def resolve_test_resources(
    profile,
    design_bundle: ApprovedTestDesignBundle,
    *,
    resource_model_gateway: Any | None = None,
) -> ResolvedTestResources:
    """Create the exact resource snapshot consumed by layer two."""

    design_bundle = ApprovedTestDesignBundle.model_validate(
        design_bundle.model_dump(mode="json")
    )
    content: dict[str, Any] = {
        "schema_version": "planning-catalog.v4",
        "catalog_id": _safe_ref(profile.profile_id, prefix="catalog.resource"),
        "system_id": profile.system_id,
        "environment": profile.environment,
        "available_executors": [],
        "http_operations": [],
        "database_operations": [],
        "database_schema": None,
        "tcp_port_probes": [],
        "performance_profiles": [],
        "agent_ui_profiles": [],
        "data_bindings": [],
        "cleanup_actions": [],
    }
    runtime: dict[str, Any] = {}
    api_performance_targets: dict[str, dict[str, str]] = {}
    loose_sources, strict_sources = _loose_resource_sources(profile)
    if strict_sources["api"]:
        operations, bindings, values, api_performance_targets = _resolve_api(profile)
        content["available_executors"].append("http_api")
        content["http_operations"].extend(operations)
        content["data_bindings"].extend(bindings)
        _merge_runtime(runtime, values)
    if strict_sources["database"]:
        operations, bindings, values, database_schema = _resolve_database(profile)
        content["available_executors"].append("database")
        content["database_operations"].extend(operations)
        content["database_schema"] = database_schema
        content["data_bindings"].extend(bindings)
        _merge_runtime(runtime, values)
    if loose_sources:
        model_sources = dict(loose_sources)
        if api_performance_targets:
            model_sources["context_api_operations"] = json.dumps(
                sorted(api_performance_targets),
                ensure_ascii=False,
            )
        draft = _normalized_draft(profile, model_sources, resource_model_gateway)
        (
            agent_ui_profiles,
            operations,
            bindings,
            database_schema,
            profiles,
            values,
            api_performance_targets,
        ) = _compile_normalized_resources(
            profile,
            draft,
            api_performance_targets,
        )
        if "api" in loose_sources:
            content["available_executors"].append("http_api")
            content["http_operations"].extend(operations)
            content["data_bindings"].extend(bindings)
        if "database" in loose_sources:
            content["available_executors"].append("database")
            content["database_schema"] = database_schema
        if "performance" in loose_sources:
            content["available_executors"].append("performance")
            content["performance_profiles"].extend(profiles)
        if "ui_agent" in loose_sources:
            content["available_executors"].append("stagehand_agent")
            content["agent_ui_profiles"].extend(agent_ui_profiles)
        _merge_runtime(runtime, values)
    if strict_sources["performance"]:
        profiles, values = _resolve_performance(profile, api_performance_targets)
        content["available_executors"].append("performance")
        content["performance_profiles"].extend(profiles)
        _merge_runtime(runtime, values)
    if profile.port_host:
        content["available_executors"].append("tcp_port")
        content["tcp_port_probes"].append(
            {
                "probe_ref": _safe_ref(profile.profile_id, prefix="port.probe"),
                "description": f"连接 {profile.name} 登记的 TCP 端口",
                "host_ref": "runtime.port.host",
                "port": profile.port_number,
                "timeout_seconds": 5.0,
                "state_effect": "read_only",
                "observables": [
                    {
                        "observable_ref": _safe_ref(profile.profile_id, prefix="port.state"),
                        "description": "TCP 端口连接状态",
                        "kind": "state",
                    },
                    {
                        "observable_ref": _safe_ref(profile.profile_id, prefix="port.latency"),
                        "description": "TCP 连接延迟",
                        "kind": "connect_latency_ms",
                    },
                ],
            }
        )
        _merge_runtime(runtime, {"network_hosts": {"runtime.port.host": profile.port_host}})

    return ResolvedTestResources(
        catalog=PlanningCatalogSnapshot.build(**content),
        runtime_config=runtime,
        runtime_config_hash=runtime_config_content_hash(runtime),
    )


__all__ = [
    "ResolvedTestResources",
    "resolve_test_resources",
    "runtime_config_content_hash",
    "validate_non_ui_resource_files",
    "validate_resource_source_inputs",
]
