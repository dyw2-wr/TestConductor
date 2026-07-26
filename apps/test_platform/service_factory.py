"""Django composition root for the v4 workflow.

Only process configuration is read here. API keys and runtime values are never
stored in workflow rows, planning artifacts, or reports.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4
from collections.abc import Mapping
from typing import Any, Callable, Type

from django.conf import settings
from django.utils.module_loading import import_string
import httpx
from openai import OpenAI
from pydantic import ValidationError

from .intent.builder import DefaultDesignBuilder
from .input_contracts import validate_runtime_input
from .intent.model_gateway import ExistingLLMModelGateway
from .intent.prompt_builder import DefaultDesignPromptBuilder
from .intent.service import TestDesignPipeline
from .intent.knowledge import (
    ApprovedKnowledgeSourceStore,
    InMemoryApprovedKnowledgeResolver,
    MilvusApprovedKnowledgeResolver,
)
from .retrieval import ControlledRetriever
from .retrieval.milvus import (
    MilvusConfig,
    MilvusHybridBackend,
    build_embedding_provider,
)
from .planning.planner import DefaultPlanPromptBuilder, PlanDraftGenerator
from .runners import ProcedureRunner, ExecutionCoordinator, RunnerRegistry
from .runners.contracts import RuntimeContext
from .runners.performance_http import HttpPerformanceDriver
from .run_history import get_default_run_history_recorder
from .workflow import IntentToExecutionWorkflow


def _setting(name: str, default: Any) -> Any:
    """Read Django configuration without breaking standalone contract tests."""

    if not settings.configured:
        return default
    return getattr(settings, name, default)


def _release_model_slot(slot: Path, token: str) -> None:
    """Release only the directory owned by this caller.

    The token check prevents a timed-out owner from deleting a slot that has
    already been reclaimed by another worker.
    """

    try:
        if (slot / "owner").read_text(encoding="utf-8") != token:
            return
        quarantine = slot.with_name(f"{slot.name}.release-{token}")
        slot.rename(quarantine)
    except (FileNotFoundError, OSError):
        return
    shutil.rmtree(quarantine, ignore_errors=True)


@contextmanager
def model_call_slot():
    """Bound concurrent model calls across all local worker processes."""

    limit = int(_setting("TEST_PLATFORM_LLM_MAX_CONCURRENT_CALLS", 2))
    queue_timeout = float(_setting("TEST_PLATFORM_LLM_QUEUE_TIMEOUT_SECONDS", 15))
    call_timeout = float(_setting("TEST_PLATFORM_LLM_TIMEOUT_SECONDS", 120))
    if limit < 1 or limit > 128:
        raise RuntimeError("TEST_PLATFORM_LLM_MAX_CONCURRENT_CALLS 必须在 1-128 之间")
    if queue_timeout < 0 or queue_timeout > 3600:
        raise RuntimeError("TEST_PLATFORM_LLM_QUEUE_TIMEOUT_SECONDS 必须在 0-3600 之间")

    artifact_root = _setting("TEST_PLATFORM_ARTIFACT_ROOT", Path.cwd() / "run_artifacts")
    root = Path(artifact_root).resolve() / ".model-call-slots"
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + queue_timeout
    stale_after = max(300.0, call_timeout * 3)
    token = uuid4().hex
    acquired: Path | None = None
    while acquired is None:
        now = time.time()
        for index in range(limit):
            slot = root / f"slot-{index + 1}"
            try:
                slot.mkdir()
                try:
                    (slot / "owner").write_text(token, encoding="utf-8")
                except OSError:
                    shutil.rmtree(slot, ignore_errors=True)
                    raise
                acquired = slot
                break
            except FileExistsError:
                try:
                    if now - slot.stat().st_mtime <= stale_after:
                        continue
                    quarantine = root / f"stale-{index + 1}-{uuid4().hex}"
                    slot.rename(quarantine)
                    shutil.rmtree(quarantine, ignore_errors=True)
                except (FileNotFoundError, OSError):
                    continue
        if acquired is not None:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("模型服务繁忙，等待并发名额超时，请稍后重试")
        time.sleep(0.1)
    try:
        yield
    finally:
        _release_model_slot(acquired, token)


class OpenAICompatibleModelGateway:
    """Shared strict-JSON gateway for both v4 model boundaries."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        timeout: float,
        progress_callback: Callable[[str, str, int], None] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.model = model
        self.progress_callback = progress_callback
        self.client: OpenAI | None = None

    def _notify_progress(self, phase: str, message: str, percent: int) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(phase, message, percent)
        except Exception:
            # Progress reporting must never turn a successful model call into a
            # failed generation job.
            return

    def generate(self, messages, output_schema: Type[Any]) -> Any:
        if not self.api_key:
            raise RuntimeError("未配置 TEST_PLATFORM_LLM_API_KEY")
        if not self.model:
            raise RuntimeError("未配置 TEST_PLATFORM_LLM_MODEL")
        if self.client is None:
            connect_timeout = float(_setting("TEST_PLATFORM_LLM_CONNECT_TIMEOUT_SECONDS", 10))
            max_retries = int(_setting("TEST_PLATFORM_LLM_MAX_RETRIES", 0))
            if not 0 < self.timeout <= 3600:
                raise RuntimeError("TEST_PLATFORM_LLM_TIMEOUT_SECONDS 必须在 0-3600 之间")
            if not 0 < connect_timeout <= self.timeout:
                raise RuntimeError("模型连接超时必须大于 0 且不超过模型总超时")
            if max_retries < 0 or max_retries > 5:
                raise RuntimeError("TEST_PLATFORM_LLM_MAX_RETRIES 必须在 0-5 之间")
            kwargs: dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": httpx.Timeout(self.timeout, connect=connect_timeout),
                "max_retries": max_retries,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = OpenAI(**kwargs)
        request = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.__name__,
                    "strict": True,
                    "schema": output_schema.model_json_schema(),
                },
            },
        }
        self._notify_progress("waiting_model", "正在等待模型调用名额", 35)
        with model_call_slot():
            self._notify_progress("calling_model", "正在调用模型", 45)
            try:
                response = self.client.chat.completions.create(**request)
            except Exception as exc:  # provider/network specific
                detail = str(exc).lower()
                if getattr(exc, "status_code", None) == 400 and (
                    "json_schema" in detail or "response_format" in detail
                ):
                    request["response_format"] = {"type": "json_object"}
                    try:
                        response = self.client.chat.completions.create(**request)
                    except Exception as fallback_exc:
                        raise RuntimeError(f"模型调用失败: {fallback_exc}") from fallback_exc
                else:
                    raise RuntimeError(f"模型调用失败: {exc}") from exc
        self._notify_progress("validating_model", "模型已返回，正在校验结果结构", 75)
        content = response.choices[0].message.content if response.choices else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型没有返回 JSON 文本")
        payload = ExistingLLMModelGateway._parse_json(content)
        try:
            return output_schema.model_validate(payload)
        except (AttributeError, ValidationError) as exc:
            raise ValueError(f"模型输出不符合严格候选 schema: {exc}") from exc


def get_model_gateway(
    purpose: str | None = None,
    *,
    progress_callback: Callable[[str, str, int], None] | None = None,
) -> OpenAICompatibleModelGateway:
    model_setting = {
        "design": "TEST_PLATFORM_DESIGN_LLM_MODEL",
        "planning": "TEST_PLATFORM_PLANNING_LLM_MODEL",
    }.get(purpose)
    model = (
        str(getattr(settings, model_setting, "") or "").strip()
        if model_setting
        else ""
    ) or str(getattr(settings, "TEST_PLATFORM_LLM_MODEL", "") or "").strip()
    return OpenAICompatibleModelGateway(
        api_key=str(getattr(settings, "TEST_PLATFORM_LLM_API_KEY", "")),
        base_url=str(getattr(settings, "TEST_PLATFORM_LLM_BASE_URL", "")) or None,
        model=model,
        timeout=float(getattr(settings, "TEST_PLATFORM_LLM_TIMEOUT_SECONDS", 120)),
        progress_callback=progress_callback,
    )


def get_knowledge_resolver():
    catalog_path = str(
        _setting("TEST_PLATFORM_APPROVED_KNOWLEDGE_CATALOG", "") or ""
    ).strip()
    sources = None
    if catalog_path:
        path = Path(catalog_path)
        if not path.is_absolute():
            path = Path(_setting("BASE_DIR", Path.cwd())) / path
        sources = ApprovedKnowledgeSourceStore.from_json(path)

    from .intent.contracts import ApprovedKnowledge
    from .models import ApprovedKnowledgeEntry

    managed_documents = [
        ApprovedKnowledge(
            scope_id=entry.scope_id,
            knowledge_id=entry.knowledge_id,
            version=entry.version,
            approval_id=entry.approval_id,
            approved_at=entry.approved_at.isoformat(),
            content=entry.content,
            content_hash=entry.content_hash,
        )
        for entry in ApprovedKnowledgeEntry.objects.filter(
            status=ApprovedKnowledgeEntry.Status.APPROVED
        )
    ]
    if managed_documents:
        external_documents = (
            [
                sources.approved_document(item.metadata.source_ref)
                for item in sources.sources()
            ]
            if sources is not None
            else []
        )
        merged = {item.scope_id: item for item in external_documents}
        merged.update({item.scope_id: item for item in managed_documents})
        return InMemoryApprovedKnowledgeResolver(list(merged.values()))
    if sources is None:
        return InMemoryApprovedKnowledgeResolver()
    if not bool(_setting("TEST_PLATFORM_MILVUS_ENABLED", False)):
        return InMemoryApprovedKnowledgeResolver(
            [
                sources.approved_document(item.metadata.source_ref)
                for item in sources.sources()
            ]
        )
    config = MilvusConfig(
        uri=str(_setting("TEST_PLATFORM_MILVUS_URI", "http://localhost:19530")),
        token=str(_setting("TEST_PLATFORM_MILVUS_TOKEN", "")),
        database=str(_setting("TEST_PLATFORM_MILVUS_DATABASE", "default")),
        collection=str(
            _setting(
                "TEST_PLATFORM_MILVUS_COLLECTION",
                "test_conductor_knowledge_v1",
            )
        ),
        dense_weight=float(_setting("TEST_PLATFORM_MILVUS_DENSE_WEIGHT", 0.65)),
        sparse_weight=float(_setting("TEST_PLATFORM_MILVUS_SPARSE_WEIGHT", 0.35)),
    )
    retriever = ControlledRetriever(
        backend=MilvusHybridBackend(config),
        embeddings=build_embedding_provider(
            str(_setting("TEST_PLATFORM_EMBEDDING_PROVIDER", "hashing")),
            device=str(_setting("TEST_PLATFORM_EMBEDDING_DEVICE", "cpu")),
        ),
        sources=sources,
    )
    return MilvusApprovedKnowledgeResolver(retriever=retriever, sources=sources)


def get_workflow(
    *,
    progress_callback: Callable[[str, str, int], None] | None = None,
) -> IntentToExecutionWorkflow:
    design_gateway = get_model_gateway(
        "design",
        progress_callback=progress_callback,
    )
    planning_gateway = get_model_gateway(
        "planning",
        progress_callback=progress_callback,
    )
    pipeline = TestDesignPipeline(
        DefaultDesignBuilder(DefaultDesignPromptBuilder(), design_gateway),
        knowledge_resolver=get_knowledge_resolver(),
    )
    planner = PlanDraftGenerator(DefaultPlanPromptBuilder(), planning_gateway)
    coordinator = ExecutionCoordinator(
        RunnerRegistry(procedure=ProcedureRunner()),
        run_history_recorder=get_default_run_history_recorder(),
    )
    return IntentToExecutionWorkflow(pipeline, planner, coordinator=coordinator)


_RUNTIME_MAPPING_FIELDS = frozenset(
    {
        "variables",
        "base_urls",
        "network_hosts",
        "query_catalog",
        "database_schemas",
        "database_connections",
        "performance_profiles",
        "data_guarantees",
    }
)
_RUNTIME_SCALAR_FIELDS = frozenset(
    {
        "procedure_asset_database",
        "procedure_library_id",
        "procedure_library_hash",
        "ui_browser_headless",
        "performance_mode",
        "max_response_bytes",
        "max_performance_duration_seconds",
        "max_virtual_users",
    }
)
_RUNTIME_ALLOWED_FIELDS = _RUNTIME_MAPPING_FIELDS | _RUNTIME_SCALAR_FIELDS


def _runtime_values(runtime_config: Mapping[str, Any] | None) -> dict[str, Any]:
    if runtime_config is None:
        return {}
    if not isinstance(runtime_config, Mapping):
        raise ValueError("runtime_config 顶层必须是对象")
    unknown = set(runtime_config) - _RUNTIME_ALLOWED_FIELDS
    if unknown:
        raise ValueError(
            "runtime_config 包含禁止或未知字段: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    values = dict(runtime_config)
    for name in _RUNTIME_MAPPING_FIELDS:
        value = values.get(name, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"runtime_config.{name} 必须是对象")
        values[name] = dict(value)
    return values


def get_runtime_context(*, evidence_dir, runtime_config=None, execution_input=None) -> RuntimeContext:
    """Resolve runtime dependencies without persisting secrets in Django."""

    factory_path = str(
        getattr(settings, "TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY", "") or ""
    ).strip()
    if factory_path:
        context = import_string(factory_path)(evidence_dir=evidence_dir)
        if not isinstance(context, RuntimeContext):
            raise TypeError("TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY 必须返回 RuntimeContext")
        values = {item.name: getattr(context, item.name) for item in fields(RuntimeContext)}
    else:
        raw = str(getattr(settings, "TEST_PLATFORM_RUNTIME_CONTEXT_JSON", "") or "").strip()
        values = {}
        if raw:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("TEST_PLATFORM_RUNTIME_CONTEXT_JSON 顶层必须是对象")
            values = decoded

    resource_values = _runtime_values(runtime_config)
    for name in _RUNTIME_MAPPING_FIELDS:
        if name not in resource_values:
            continue
        merged = dict(values.get(name) or {})
        merged.update(resource_values[name])
        values[name] = merged
    for name in _RUNTIME_SCALAR_FIELDS:
        if name in resource_values:
            values[name] = resource_values[name]
    if execution_input is not None:
        submitted = validate_runtime_input(execution_input)
        merged_variables = dict(values.get("variables") or {})
        merged_variables.update(submitted.variables)
        values["variables"] = merged_variables
        if submitted.performance_mode is not None:
            values["performance_mode"] = submitted.performance_mode
    database_connections = dict(values.get("database_connections") or {})
    for ref, connection in database_connections.items():
        if isinstance(connection, str) and connection.strip():
            path = Path(connection)
            if not path.is_absolute():
                database_connections[ref] = Path(settings.BASE_DIR) / path
    values["database_connections"] = database_connections
    performance_drivers = dict(values.get("performance_drivers") or {})
    performance_drivers.setdefault("driver.http", HttpPerformanceDriver())
    values["performance_drivers"] = performance_drivers
    values["evidence_dir"] = evidence_dir
    return RuntimeContext(**values)


__all__ = [
    "OpenAICompatibleModelGateway",
    "get_model_gateway",
    "get_knowledge_resolver",
    "get_runtime_context",
    "get_workflow",
]
