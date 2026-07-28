"""Normalize human-authored resource notes into a strict planning draft."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.test_platform.intent.contracts import ModelMessage, contains_secret_value


class _StrictDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApiParameterDraft(_StrictDraft):
    name: str
    location: Literal["path", "query", "body"]


class ApiOperationDraft(_StrictDraft):
    name: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str
    description: str
    parameters: list[ApiParameterDraft] = Field(default_factory=list)


class DatabaseColumnDraft(_StrictDraft):
    name: str
    data_type: str = "text"
    description: str = ""


class DatabaseTableDraft(_StrictDraft):
    name: str
    description: str = ""
    columns: list[DatabaseColumnDraft] = Field(min_length=1)


class DatabaseSchemaDraft(_StrictDraft):
    dialect: str = "generic"
    tables: list[DatabaseTableDraft] = Field(min_length=1)
    allowed_parameter_names: list[str] = Field(default_factory=list)


class PerformanceMetricDraft(_StrictDraft):
    metric: str
    description: str
    unit: str | None = None
    percentile: str | None = None


class PerformanceProfileDraft(_StrictDraft):
    name: str
    description: str
    max_duration_seconds: float = Field(gt=0, le=86_400)
    max_virtual_users: int = Field(gt=0, le=1_000_000)
    target_api_operation: str | None = None
    target_url: str | None = None
    metrics: list[PerformanceMetricDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def require_one_target(self) -> "PerformanceProfileDraft":
        if bool(self.target_api_operation) == bool(self.target_url):
            raise ValueError(
                "performance profile requires exactly one target_api_operation or target_url"
            )
        return self


class AgentUiProfileDraft(_StrictDraft):
    url: str
    features: list[str] = Field(min_length=1)
    max_steps: int = Field(ge=1, le=200)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("网页 Agent URL 必须是绝对 HTTP(S) 地址")
        return value


class NormalizedResourceDraft(_StrictDraft):
    agent_ui_profiles: list[AgentUiProfileDraft] = Field(default_factory=list)
    api_operations: list[ApiOperationDraft] = Field(default_factory=list)
    database_schema: DatabaseSchemaDraft | None = None
    performance_profiles: list[PerformanceProfileDraft] = Field(default_factory=list)


_SYSTEM_PROMPT = """你负责把测试人员提供的宽松资源资料整理成机器可校验的资源草稿。

规则：
1. 只提取资料明确支持的 UI 网站、接口、表字段和性能目标，不补造业务能力、凭据或连接信息。
2. UI Agent 资料只整理绝对 HTTP(S) URL、粗粒度功能和明确给出的最大步数；缺少最大步数时不要自行补造。
3. API path 必须是以 / 开头的相对路径；name 在本次输出中唯一。
4. 数据库只描述数据库/Schema 和允许访问的表字段，不输出或保存 SQL。若类型不明确可使用 text。
5. 性能资料使用 driver.http。没有明确安全上限时采用保守默认值：60 秒、10 个虚拟用户。
6. 性能目标优先引用本次输出或上下文中已有 API operation name；否则使用资料中的绝对 HTTP URL。
7. 指标名称使用 latency_ms、error_rate、throughput_rps 等稳定英文标识。
8. 不输出解释、Markdown 或 schema 之外的字段。
"""


def normalize_resource_sources(
    gateway: Any,
    sources: dict[str, str],
) -> NormalizedResourceDraft:
    """Use the planning model for loose source material, then validate its draft."""

    normalized_sources = {
        str(name): str(value or "").strip()
        for name, value in sources.items()
        if str(value or "").strip()
    }
    if not normalized_sources:
        return NormalizedResourceDraft()
    if contains_secret_value(normalized_sources):
        raise ValueError("测试资源资料包含疑似凭据实际值")
    encoded = json.dumps(normalized_sources, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > 500_000:
        raise ValueError("需要模型整理的测试资源资料不能超过 500 KiB")
    draft = gateway.generate(
        [
            ModelMessage(role="system", content=_SYSTEM_PROMPT),
            ModelMessage(
                role="user",
                content=(
                    "请整理以下测试资源资料。只为资料中存在的分类返回内容；"
                    "未提供的分类保持空值。\n" + encoded
                ),
            ),
        ],
        NormalizedResourceDraft,
    )
    draft = NormalizedResourceDraft.model_validate(draft)
    if "ui_agent" in normalized_sources and not draft.agent_ui_profiles:
        raise ValueError("模型没有从网页 Agent 资料中整理出 URL、功能和最大步数")
    if "ui_agent" not in normalized_sources and draft.agent_ui_profiles:
        raise ValueError("模型为未提供的网页 Agent 资料生成了额外内容")
    if "api" in normalized_sources and not draft.api_operations:
        raise ValueError("模型没有从接口资料中整理出可调用接口")
    if "api" not in normalized_sources and draft.api_operations:
        raise ValueError("模型为未提供的接口资料生成了额外内容")
    if "database" in normalized_sources and draft.database_schema is None:
        raise ValueError("模型没有从数据库资料中整理出表结构")
    if "database" not in normalized_sources and draft.database_schema is not None:
        raise ValueError("模型为未提供的数据库资料生成了额外内容")
    if "performance" in normalized_sources and not draft.performance_profiles:
        raise ValueError("模型没有从性能资料中整理出性能配置")
    if "performance" not in normalized_sources and draft.performance_profiles:
        raise ValueError("模型为未提供的性能资料生成了额外内容")
    return draft


__all__ = [
    "AgentUiProfileDraft",
    "NormalizedResourceDraft",
    "normalize_resource_sources",
]
