"""Build and execute a repeatable, multi-subject acceptance matrix.

This command deliberately uses deterministic fixture gateways at both model
boundaries.  The generated candidates still pass through the production design
builder, validation, human-review contracts, plan compiler, artifact hash gates,
runners, report writer, and Django run-history recorder.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socketserver
import sqlite3
from threading import Thread
from typing import Any, Iterator, Mapping
from uuid import uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings
from django.utils import timezone

from apps.test_platform.approval_service import (
    persist_execution_plan_review,
    persist_test_plan_approval,
)
from apps.test_platform.execution_service import execute_execution_plan_artifact
from apps.test_platform.intent.builder import DefaultDesignBuilder
from apps.test_platform.intent.contracts import ReviewDecision, TestDesignRequest
from apps.test_platform.intent.prompt_builder import DefaultDesignPromptBuilder
from apps.test_platform.intent.service import TestDesignPipeline
from apps.test_platform.models import (
    ApprovedKnowledgeEntry,
    ExecutionPlanArtifact,
    TestExecutionRun,
    TestPlanArtifact,
    TestResourceProfile,
    TestWorkflow,
)
from apps.test_platform.planning.compiler import TestPlanCompiler
from apps.test_platform.planning.contracts import PlanReviewDecision
from apps.test_platform.planning.planner import (
    DefaultPlanPromptBuilder,
    PlanDraftGenerator,
)
from apps.test_platform.planning.resources import resolve_test_resources
from apps.test_platform.service_factory import get_knowledge_resolver
from apps.test_platform.workflow import IntentToExecutionWorkflow


BUSINESS_MODELS = (
    ApprovedKnowledgeEntry,
    TestResourceProfile,
    TestWorkflow,
    TestPlanArtifact,
    ExecutionPlanArtifact,
    TestExecutionRun,
)


def _json(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


class _FixtureGateway:
    """A deterministic model boundary which still validates the output schema."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)

    def generate(self, messages, output_schema):
        if not messages:
            raise ValueError("fixture gateway received an empty prompt")
        return output_schema.model_validate(self.payload)


class _MatrixHttpHandler(BaseHTTPRequestHandler):
    server_version = "TestConductorMatrix/1.0"

    def do_GET(self) -> None:  # noqa: N802 - standard-library handler API
        routes = {
            "/health": (
                200,
                {
                    "status": "ready",
                    "service": "commerce-gateway",
                    "region": "local",
                },
            ),
            "/orders/summary": (
                200,
                {"orders": 3, "paid": 2, "pending": 1},
            ),
            "/analytics/pulse": (
                200,
                {"metric": "availability", "value": 99.95},
            ),
            "/fail": (
                500,
                {"status": "failed", "reason": "intentional matrix fixture"},
            ),
        }
        status, payload = routes.get(
            self.path,
            (404, {"error": "not found"}),
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _MatrixTcpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.sendall(b"test_conductor-matrix-ready\n")


class _MatrixTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass(frozen=True)
class _LocalServices:
    api_base_url: str
    tcp_host: str
    tcp_port: int


@contextmanager
def _local_services() -> Iterator[_LocalServices]:
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), _MatrixHttpHandler)
    tcp_server = _MatrixTcpServer(("127.0.0.1", 0), _MatrixTcpHandler)
    http_thread = Thread(target=http_server.serve_forever, daemon=True)
    tcp_thread = Thread(target=tcp_server.serve_forever, daemon=True)
    http_thread.start()
    tcp_thread.start()
    try:
        yield _LocalServices(
            api_base_url=f"http://127.0.0.1:{http_server.server_address[1]}",
            tcp_host="127.0.0.1",
            tcp_port=int(tcp_server.server_address[1]),
        )
    finally:
        http_server.shutdown()
        tcp_server.shutdown()
        http_server.server_close()
        tcp_server.server_close()
        http_thread.join(timeout=2)
        tcp_thread.join(timeout=2)


@dataclass(frozen=True)
class _DatabaseFixture:
    key: str
    path: Path
    connection_ref: str
    system_id: str
    table: str
    columns: tuple[tuple[str, str, str], ...]
    sql: str
    check_column: str
    expected: Any

    def policy(self) -> dict[str, Any]:
        return {
            "schema_version": "database-access-policy.v1",
            "database_schema": {
                "dialect": "sqlite",
                "tables": [
                    {
                        "name": self.table,
                        "description": f"{self.system_id} matrix fixture",
                        "columns": [
                            {
                                "name": name,
                                "data_type": data_type,
                                "description": description,
                            }
                            for name, data_type, description in self.columns
                        ],
                    }
                ],
                "allowed_parameter_refs": [],
            },
        }


def _business_counts() -> dict[str, int]:
    return {model.__name__: model.objects.count() for model in BUSINESS_MODELS}


def _file_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def _write_database_fixtures(root: Path) -> list[_DatabaseFixture]:
    root.mkdir(parents=True, exist_ok=True)
    fixtures = [
        _DatabaseFixture(
            key="identity",
            path=root / "identity.sqlite3",
            connection_ref="matrix.db.identity",
            system_id="identity-center",
            table="user_accounts",
            columns=(
                ("username", "text", "登录名"),
                ("status", "text", "账号状态"),
                ("failed_attempts", "integer", "连续失败次数"),
            ),
            sql="SELECT status FROM user_accounts WHERE username = 'alice' LIMIT 1",
            check_column="status",
            expected="active",
        ),
        _DatabaseFixture(
            key="commerce",
            path=root / "commerce.sqlite3",
            connection_ref="matrix.db.commerce",
            system_id="commerce-hub",
            table="inventory",
            columns=(
                ("sku", "text", "商品编号"),
                ("stock", "integer", "现有库存"),
                ("reserved", "integer", "预留库存"),
            ),
            sql="SELECT stock FROM inventory WHERE sku = 'SKU-BOOK-01' LIMIT 1",
            check_column="stock",
            expected=42,
        ),
        _DatabaseFixture(
            key="analytics",
            path=root / "analytics.sqlite3",
            connection_ref="matrix.db.analytics",
            system_id="analytics-lake",
            table="daily_metrics",
            columns=(
                ("metric_name", "text", "指标名称"),
                ("metric_value", "real", "指标值"),
                ("recorded_on", "text", "统计日期"),
            ),
            sql=(
                "SELECT metric_value FROM daily_metrics "
                "WHERE metric_name = 'availability' LIMIT 1"
            ),
            check_column="metric_value",
            expected=99.95,
        ),
    ]
    setup = {
        "identity": (
            "CREATE TABLE IF NOT EXISTS user_accounts "
            "(username TEXT PRIMARY KEY, status TEXT NOT NULL, failed_attempts INTEGER NOT NULL)",
            (
                "INSERT OR REPLACE INTO user_accounts(username, status, failed_attempts) "
                "VALUES (?, ?, ?)"
            ),
            [("alice", "active", 1), ("bob", "suspended", 5)],
        ),
        "commerce": (
            "CREATE TABLE IF NOT EXISTS inventory "
            "(sku TEXT PRIMARY KEY, stock INTEGER NOT NULL, reserved INTEGER NOT NULL)",
            "INSERT OR REPLACE INTO inventory(sku, stock, reserved) VALUES (?, ?, ?)",
            [("SKU-BOOK-01", 42, 3), ("SKU-CABLE-02", 0, 0)],
        ),
        "analytics": (
            "CREATE TABLE IF NOT EXISTS daily_metrics "
            "(metric_name TEXT PRIMARY KEY, metric_value REAL NOT NULL, recorded_on TEXT NOT NULL)",
            (
                "INSERT OR REPLACE INTO daily_metrics"
                "(metric_name, metric_value, recorded_on) VALUES (?, ?, ?)"
            ),
            [
                ("availability", 99.95, "2026-07-26"),
                ("conversion_rate", 3.7, "2026-07-26"),
            ],
        ),
    }
    for fixture in fixtures:
        ddl, insert, rows = setup[fixture.key]
        connection = sqlite3.connect(fixture.path)
        try:
            connection.execute(ddl)
            connection.executemany(insert, rows)
            connection.commit()
        finally:
            connection.close()
    return fixtures


def _save_json_field(instance, field_name: str, filename: str, payload: Any) -> None:
    field = getattr(instance, field_name)
    field.save(
        filename,
        ContentFile(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
        ),
        save=False,
    )


def _save_text_field(instance, field_name: str, filename: str, value: str) -> None:
    getattr(instance, field_name).save(
        filename,
        ContentFile(value.encode("utf-8")),
        save=False,
    )


def _create_knowledge(
    *,
    fixture: _DatabaseFixture,
    run_tag: str,
) -> ApprovedKnowledgeEntry:
    entry = ApprovedKnowledgeEntry(
        system_id=fixture.system_id,
        title=f"{fixture.system_id} 只读验收规则 {run_tag}",
        content=(
            "该资源只允许执行经过审批的只读查询。验收 SQL："
            f"{fixture.sql}。不得执行 INSERT、UPDATE、DELETE 或多语句。"
        ),
    )
    _save_text_field(
        entry,
        "source_file",
        f"{fixture.key}-{run_tag}.md",
        f"# {fixture.system_id} 只读规则\n\n{entry.content}\n",
    )
    entry.full_clean()
    entry.save()
    entry.approve()
    return entry


def _create_database_profile(
    *,
    fixture: _DatabaseFixture,
    run_tag: str,
) -> TestResourceProfile:
    profile = TestResourceProfile(
        name=f"{fixture.system_id} SQLite 资源 {run_tag}",
        system_id=fixture.system_id,
        environment="matrix-local",
        database_connection_ref=fixture.connection_ref,
    )
    _save_json_field(
        profile,
        "database_asset_file",
        f"{fixture.key}-database-policy-{run_tag}.json",
        fixture.policy(),
    )
    profile.full_clean()
    profile.save()
    return profile


def _create_composite_profile(
    *,
    fixture: _DatabaseFixture,
    services: _LocalServices,
    run_tag: str,
) -> TestResourceProfile:
    operation_id = "matrixHealth"
    profile = TestResourceProfile(
        name=f"电商网关复合资源 {run_tag}",
        system_id=fixture.system_id,
        environment="matrix-local",
        api_base_url=services.api_base_url,
        database_connection_ref=fixture.connection_ref,
        port_host=services.tcp_host,
        port_number=services.tcp_port,
    )
    _save_json_field(
        profile,
        "api_openapi_file",
        f"commerce-openapi-{run_tag}.json",
        {
            "openapi": "3.0.3",
            "info": {"title": "Matrix commerce API", "version": "1.0.0"},
            "paths": {
                "/health": {
                    "get": {
                        "operationId": operation_id,
                        "summary": "读取电商网关健康状态",
                        "responses": {"200": {"description": "服务正常"}},
                    }
                },
                "/fail": {
                    "get": {
                        "operationId": "matrixFail",
                        "summary": "固定返回 500 的失败链路入口",
                        "responses": {
                            "500": {"description": "矩阵预期失败响应"}
                        },
                    }
                },
            },
        },
    )
    _save_json_field(
        profile,
        "database_asset_file",
        f"commerce-composite-database-policy-{run_tag}.json",
        fixture.policy(),
    )
    _save_json_field(
        profile,
        "performance_profile_file",
        f"commerce-performance-{run_tag}.json",
        {
            "schema_version": "performance-profile-set.v1",
            "profiles": [
                {
                    "profile_ref": "perf.matrix.commerce-smoke",
                    "description": "本地电商健康接口轻量压力检查",
                    "driver_ref": "driver.http",
                    "state_effect": "read_only",
                    "max_duration_seconds": 5,
                    "max_virtual_users": 10,
                    "observables": [
                        {
                            "observable_ref": "observable.perf.matrix-p95",
                            "description": "健康接口响应延迟 p95",
                            "metric": "latency_ms",
                            "unit": "ms",
                            "percentile": "p95",
                        }
                    ],
                    "runtime": {"target_operation_id": operation_id},
                }
            ],
        },
    )
    profile.full_clean()
    profile.save()
    return profile


def _scenario(
    *,
    title: str,
    requirement_id: str,
    channel: str,
    expected_text: str,
    expected: Any,
    operator: str,
    technique: str = "positive",
    data_requirements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "techniques": [technique],
        "requirement_ids": [requirement_id],
        "operations": [
            {"text": f"执行 {title}", "channel_hint": channel}
        ],
        "expected_results": [
            {
                "text": expected_text,
                "after_operation_index": 1,
                "channel_hint": channel,
                "operator": operator,
                "expected": expected,
                **({"unit": "ms"} if channel == "performance" else {}),
            }
        ],
        "data_requirements": data_requirements or [],
        "state_impact": {
            "impact": "read_only",
            "rationale": {"text": "验收只读取已登记资源，不改变目标状态"},
        },
    }


def _design_candidate(
    title: str,
    scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "title": title,
        "objective": {
            "text": f"确定性验证 {title}",
            "derivation_note": "直接拆解自矩阵验收需求",
        },
        "in_scope": [{"text": "仅验证命令创建的本地受控资源"}],
        "out_of_scope": [{"text": "不访问生产环境，不执行写操作"}],
        "scenarios": scenarios,
        "open_questions": [],
    }


def _composite_scenario(requirement_id: str) -> dict[str, Any]:
    """One real four-stage business flow, not four unrelated batch entries."""

    return {
        "title": "电商网关四通道线性验收",
        "techniques": ["positive"],
        "requirement_ids": [requirement_id],
        "operations": [
            {"text": "请求电商健康接口", "channel_hint": "api"},
            {"text": "读取同一网关的 SKU 库存", "channel_hint": "database"},
            {"text": "对健康接口执行轻量压力检查", "channel_hint": "performance"},
            {"text": "连接网关登记的 TCP 端口", "channel_hint": "port"},
        ],
        "expected_results": [
            {
                "text": "HTTP 状态应为 200",
                "after_operation_index": 1,
                "channel_hint": "api",
                "operator": "equals",
                "expected": 200,
            },
            {
                "text": "SKU-BOOK-01 库存应为 42",
                "after_operation_index": 2,
                "channel_hint": "database",
                "operator": "equals",
                "expected": 42,
            },
            {
                "text": "p95 延迟应不超过 1000 ms",
                "after_operation_index": 3,
                "channel_hint": "performance",
                "operator": "lte",
                "expected": 1000,
                "unit": "ms",
            },
            {
                "text": "端口状态应为 open",
                "after_operation_index": 4,
                "channel_hint": "port",
                "operator": "equals",
                "expected": "open",
            },
        ],
        "data_requirements": [],
        "state_impact": {
            "impact": "read_only",
            "rationale": {
                "text": "四个阶段只读取受控资源，不改变目标状态"
            },
        },
    }


def _failure_chain_scenario(requirement_id: str) -> dict[str, Any]:
    return {
        "title": "API 失败后阻断数据库阶段",
        "techniques": ["positive"],
        "requirement_ids": [requirement_id],
        "operations": [
            {"text": "请求固定失败接口", "channel_hint": "api"},
            {
                "text": "仅当前序通过时读取库存",
                "channel_hint": "database",
            },
        ],
        "expected_results": [
            {
                "text": "接口应返回 200（故意与 fixture 500 冲突）",
                "after_operation_index": 1,
                "channel_hint": "api",
                "operator": "equals",
                "expected": 200,
            },
            {
                "text": "后续库存应为 42",
                "after_operation_index": 2,
                "channel_hint": "database",
                "operator": "equals",
                "expected": 42,
            },
        ],
        "data_requirements": [],
        "state_impact": {
            "impact": "read_only",
            "rationale": {
                "text": "失败链只读取 HTTP 与 SQLite，后序应由协调器阻断"
            },
        },
    }


def _database_plan_candidate(
    *,
    design_id: str,
    fixture: _DatabaseFixture,
    knowledge_scope_id: str,
    expected: Any | None = None,
) -> dict[str, Any]:
    expected_value = fixture.expected if expected is None else expected
    return {
        "flows": [
            {
                "scenario_id": f"{design_id}-SCN-0001",
                "stages": [
                    {
                        "executor_kind": "database",
                        "database_queries": [
                            {
                                "operation_id": f"{design_id}-OP-0001",
                                "expected_result_id": f"{design_id}-EXP-0001",
                                "sql": fixture.sql,
                                "execution_policy": "read_only",
                                "parameters_refs": {},
                                "check_kind": "column",
                                "check_column": fixture.check_column,
                                "operator": "equals",
                                "expected": expected_value,
                                "knowledge_scope_id": knowledge_scope_id,
                            }
                        ],
                    }
                ],
            }
        ],
        "open_questions": [],
    }


def _composite_plan_candidate(
    *,
    design_id: str,
    profile: TestResourceProfile,
    fixture: _DatabaseFixture,
    knowledge_scope_id: str,
) -> dict[str, Any]:
    port_probe = f"port.probe.{profile.profile_id}"
    return {
        "flows": [
            {
                "scenario_id": f"{design_id}-SCN-0001",
                "stages": [
                    {
                        "executor_kind": "http_api",
                        "operations": [
                            {
                                "operation_id": f"{design_id}-OP-0001",
                                "catalog_ref": "api.operation.matrixHealth",
                            }
                        ],
                        "expected_results": [
                            {
                                "expected_result_id": f"{design_id}-EXP-0001",
                                "catalog_ref": "api.operation.matrixHealth",
                                "observable_ref": "api.status.matrixHealth",
                            }
                        ],
                    },
                    {
                        "executor_kind": "database",
                        "database_queries": [
                            {
                                "operation_id": f"{design_id}-OP-0002",
                                "expected_result_id": f"{design_id}-EXP-0002",
                                "sql": fixture.sql,
                                "execution_policy": "read_only",
                                "parameters_refs": {},
                                "check_kind": "column",
                                "check_column": fixture.check_column,
                                "operator": "equals",
                                "expected": fixture.expected,
                                "knowledge_scope_id": knowledge_scope_id,
                            }
                        ],
                    },
                    {
                        "executor_kind": "performance",
                        "performance_stages": [
                            {"duration_seconds": 0.2, "virtual_users": 2}
                        ],
                        "operations": [
                            {
                                "operation_id": f"{design_id}-OP-0003",
                                "catalog_ref": "perf.matrix.commerce-smoke",
                            }
                        ],
                        "expected_results": [
                            {
                                "expected_result_id": f"{design_id}-EXP-0003",
                                "catalog_ref": "perf.matrix.commerce-smoke",
                                "observable_ref": "observable.perf.matrix-p95",
                            }
                        ],
                    },
                    {
                        "executor_kind": "tcp_port",
                        "operations": [
                            {
                                "operation_id": f"{design_id}-OP-0004",
                                "catalog_ref": port_probe,
                            }
                        ],
                        "expected_results": [
                            {
                                "expected_result_id": f"{design_id}-EXP-0004",
                                "catalog_ref": port_probe,
                                "observable_ref": (
                                    f"port.state.{profile.profile_id}"
                                ),
                            }
                        ],
                    },
                ],
            },
        ],
        "open_questions": [],
    }


def _failure_chain_plan_candidate(
    *,
    design_id: str,
    fixture: _DatabaseFixture,
    knowledge_scope_id: str,
) -> dict[str, Any]:
    return {
        "flows": [
            {
                "scenario_id": f"{design_id}-SCN-0001",
                "stages": [
                    {
                        "executor_kind": "http_api",
                        "operations": [
                            {
                                "operation_id": f"{design_id}-OP-0001",
                                "catalog_ref": "api.operation.matrixFail",
                            }
                        ],
                        "expected_results": [
                            {
                                "expected_result_id": f"{design_id}-EXP-0001",
                                "catalog_ref": "api.operation.matrixFail",
                                "observable_ref": "api.status.matrixFail",
                            }
                        ],
                    },
                    {
                        "executor_kind": "database",
                        "database_queries": [
                            {
                                "operation_id": f"{design_id}-OP-0002",
                                "expected_result_id": f"{design_id}-EXP-0002",
                                "sql": fixture.sql,
                                "execution_policy": "read_only",
                                "parameters_refs": {},
                                "check_kind": "column",
                                "check_column": fixture.check_column,
                                "operator": "equals",
                                "expected": fixture.expected,
                                "knowledge_scope_id": knowledge_scope_id,
                            }
                        ],
                    },
                ],
            }
        ],
        "open_questions": [],
    }


def _single_catalog_plan_candidate(
    *,
    design_id: str,
    executor_kind: str,
    catalog_ref: str,
    observable_ref: str,
) -> dict[str, Any]:
    generated_fields = (
        {
            "performance_stages": [
                {"duration_seconds": 0.2, "virtual_users": 2}
            ]
        }
        if executor_kind == "performance"
        else {}
    )
    return {
        "flows": [
            {
                "scenario_id": f"{design_id}-SCN-0001",
                "stages": [
                    {
                        "executor_kind": executor_kind,
                        **generated_fields,
                        "operations": [
                            {
                                "operation_id": f"{design_id}-OP-0001",
                                "catalog_ref": catalog_ref,
                            }
                        ],
                        "expected_results": [
                            {
                                "expected_result_id": f"{design_id}-EXP-0001",
                                "catalog_ref": catalog_ref,
                                "observable_ref": observable_ref,
                            }
                        ],
                    }
                ],
            }
        ],
        "open_questions": [],
    }


def _verify_failure_chain(case: "_CaseResult") -> dict[str, Any]:
    run = TestExecutionRun.objects.get(run_id=case.run_id)
    root = (
        Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
        / run.storage_root_ref
    )
    report_path = root / run.report_paths.get("json", "")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    flows = payload.get("flows") or []
    stages = (
        flows[0].get("stages") or []
        if len(flows) == 1 and isinstance(flows[0], Mapping)
        else []
    )
    first = stages[0] if len(stages) >= 1 else {}
    second = stages[1] if len(stages) >= 2 else {}
    verified = (
        payload.get("status") == "failed"
        and first.get("executor_kind") == "http_api"
        and first.get("status") == "failed"
        and first.get("external_action_started") is True
        and second.get("executor_kind") == "database"
        and second.get("status") == "blocked"
        and second.get("external_action_started") is False
        and (second.get("metadata") or {}).get("not_executed") is True
        and any(
            str(error).startswith("UPSTREAM_STAGE_NOT_PASSED:")
            for error in second.get("errors") or []
        )
    )
    return {
        "verified": verified,
        "run_id": case.run_id,
        "report_path": str(report_path),
        "first_stage": {
            "executor_kind": first.get("executor_kind"),
            "status": first.get("status"),
            "external_action_started": first.get(
                "external_action_started"
            ),
        },
        "blocked_stage": {
            "executor_kind": second.get("executor_kind"),
            "status": second.get("status"),
            "external_action_started": second.get(
                "external_action_started"
            ),
            "metadata": second.get("metadata"),
            "errors": second.get("errors"),
        },
    }


def _verify_database_expected_failure(
    case: "_CaseResult",
    fixture: _DatabaseFixture,
) -> dict[str, Any]:
    run = TestExecutionRun.objects.get(run_id=case.run_id)
    root = (
        Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
        / run.storage_root_ref
    )
    report_path = root / run.report_paths.get("json", "")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    payload_paths = list(root.rglob("execution.json"))
    payload = (
        json.loads(payload_paths[0].read_text(encoding="utf-8"))
        if len(payload_paths) == 1
        else {}
    )
    database_steps = payload.get("statements") or payload.get("queries") or [{}]
    assertion = (((database_steps[0]).get("assertions") or [{}])[0])
    stages = (
        ((report.get("flows") or [{}])[0]).get("stages") or []
    )
    step_assertions = (
        (((stages[0].get("steps") or [{}])[0]).get("details") or {}).get(
            "assertions"
        )
        or []
        if stages
        else []
    )
    connection = sqlite3.connect(
        f"file:{fixture.path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    try:
        row = connection.execute(fixture.sql).fetchone()
    finally:
        connection.close()
    observed = row[0] if row else None
    approved_expected = assertion.get("expected")
    verified = (
        report.get("status") == "failed"
        and len(stages) == 1
        and stages[0].get("executor_kind") == "database"
        and stages[0].get("status") == "failed"
        and approved_expected == "locked"
        and observed == "active"
        and any(item.get("passed") is False for item in step_assertions)
    )
    return {
        "verified": verified,
        "run_id": case.run_id,
        "report_path": str(report_path),
        "executor_kind": (
            stages[0].get("executor_kind") if stages else None
        ),
        "stage_status": stages[0].get("status") if stages else None,
        "approved_expected": approved_expected,
        "observed_fixture_value": observed,
        "assertions": step_assertions,
    }


@dataclass(frozen=True)
class _CaseResult:
    case_id: str
    case_kind: str
    title: str
    channels: list[str]
    expected_status: str
    actual_status: str
    matched_expectation: bool
    workflow_id: str
    test_plan_id: str
    execution_plan_id: str
    resource_profile_id: str
    run_id: str
    report_paths: dict[str, str]
    reports_verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.__dict__.items()
        }


class _MatrixBuilder:
    def __init__(self, *, root: Path, run_tag: str):
        self.root = root
        self.run_tag = run_tag

    def execute_case(
        self,
        *,
        slug: str,
        case_kind: str,
        title: str,
        system_id: str,
        channels: list[str],
        requirement_text: str,
        knowledge_scope_ids: list[str],
        profile: TestResourceProfile,
        design_candidate: dict[str, Any],
        plan_candidate: dict[str, Any],
        expected_status: str,
        execution_variables: dict[str, Any] | None = None,
    ) -> _CaseResult:
        request_id = f"REQ-MATRIX-{self.run_tag}-{slug}".upper()
        design_id = f"design-{request_id}"
        request = TestDesignRequest.model_validate(
            {
                "schema_version": "test-design-request.v4",
                "request_id": request_id,
                "requirements": [
                    {
                        "requirement_id": f"{request_id}-ITEM",
                        "content": requirement_text,
                    }
                ],
                "target": {
                    "system_id": system_id,
                    "environment": "matrix-local",
                },
                "selections": {
                    "techniques": ["positive"],
                    "techniques_by_channel": {
                        channel: ["positive"] for channel in channels
                    },
                    "allowed_channels": channels,
                    "required_channels": channels,
                    "knowledge_scope_ids": knowledge_scope_ids,
                },
            }
        )
        workflow_row = TestWorkflow.objects.create(
            title=title,
            requirement_text=requirement_text,
            request_id=request_id,
            system_id=system_id,
            target_environment="matrix-local",
            coverage_by_category={
                channel: ["positive"] for channel in channels
            },
            allowed_channels=channels,
            knowledge_scope_ids=knowledge_scope_ids,
            resource_profile=profile,
            status=TestWorkflow.Status.DRAFT,
        )
        compiler = TestPlanCompiler()
        service = IntentToExecutionWorkflow(
            TestDesignPipeline(
                DefaultDesignBuilder(
                    DefaultDesignPromptBuilder(),
                    _FixtureGateway(design_candidate),
                ),
                knowledge_resolver=get_knowledge_resolver(),
            ),
            PlanDraftGenerator(
                DefaultPlanPromptBuilder(),
                _FixtureGateway(plan_candidate),
                compiler,
            ),
            plan_compiler=compiler,
        )
        generated = service.generate_design(request, design_id=design_id)
        design_review = service.review_design(
            generated,
            decision=ReviewDecision.APPROVED,
            comments="矩阵验收：需求、知识范围与逻辑场景已核对",
        )
        if design_review.approved_bundle is None:
            raise CommandError(f"{slug}: deterministic design was not approved")
        test_plan = TestPlanArtifact.objects.create(
            source_intent=workflow_row,
            resource_profile=profile,
            title=generated.design.title,
            test_categories=channels,
            design_id=generated.design.design_id,
            version=generated.design.version,
            content_hash=generated.validation.design_content_hash,
            generation_result={
                key: _json(getattr(generated, key))
                for key in (
                    "request",
                    "candidate",
                    "design",
                    "input_snapshot",
                    "validation",
                )
            },
            status=TestPlanArtifact.Status.REVIEW,
        )
        test_plan = persist_test_plan_approval(
            test_plan.pk,
            expected_status=TestPlanArtifact.Status.REVIEW,
            review_payload=_json(design_review.review),
            approved_bundle=_json(design_review.approved_bundle),
        )
        resources = resolve_test_resources(
            profile,
            design_review.approved_bundle,
        )
        resources.catalog.require_target(system_id, "matrix-local")
        artifact_root = self.root / "cases" / f"{slug}-{self.run_tag}"
        compilation = service.compile_plan(
            design_review.approved_bundle,
            resources.catalog,
            artifact_root,
            plan_id=f"plan-{request_id}",
        )
        if not compilation.validation.passed or not compilation.artifacts:
            raise CommandError(
                f"{slug}: compilation did not pass: "
                f"{_json(compilation.validation)}"
            )
        plan_review = service.review_plan(
            compilation,
            resources.catalog,
            decision=PlanReviewDecision.APPROVED,
            comments="矩阵验收：执行映射、生成文件和 hash 门禁已核对",
        )
        if plan_review.approved_bundle is None:
            raise CommandError(f"{slug}: deterministic plan was not approved")
        storage_root = Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
        artifact_ref = artifact_root.resolve().relative_to(storage_root).as_posix()
        execution_plan = ExecutionPlanArtifact.objects.create(
            source_test_plan=test_plan,
            resource_profile=profile,
            title=f"{title} - 执行计划",
            test_categories=channels,
            plan_id=compilation.plan.plan_id,
            version=compilation.plan.version,
            content_hash=compilation.plan.content_hash(),
            catalog_snapshot=resources.catalog.model_dump(mode="json"),
            runtime_config_hash=resources.runtime_config_hash,
            compilation_result={
                key: _json(getattr(compilation, key))
                for key in ("plan", "validation", "artifacts")
            },
            artifact_root_ref=artifact_ref,
            status=ExecutionPlanArtifact.Status.REVIEW,
        )
        execution_plan = persist_execution_plan_review(
            execution_plan.pk,
            expected_status=ExecutionPlanArtifact.Status.REVIEW,
            review_payload=_json(plan_review.review),
            approved_bundle=_json(plan_review.approved_bundle),
        )
        run_id = (
            f"RUN-{timezone.localdate():%Y%m%d}-MATRIX-"
            f"{slug.upper()}-{uuid4().hex[:8].upper()}"
        )
        TestExecutionRun.objects.create(
            run_id=run_id,
            status=TestExecutionRun.Status.QUEUED,
            report_status=TestExecutionRun.ReportStatus.PENDING,
            execution_plan=execution_plan,
            resource_profile=profile,
            started_at=timezone.now(),
            storage_root_ref=artifact_ref,
        )
        execution_plan.execution_input = {
            "schema_version": "test-runtime-input.v1",
            "variables": execution_variables or {},
            **(
                {"performance_mode": "live"}
                if "performance" in channels
                else {}
            ),
        }
        execution_plan.save(update_fields=("execution_input", "updated_at"))
        summary = execute_execution_plan_artifact(
            execution_plan.pk,
            run_id=run_id,
        )
        run = TestExecutionRun.objects.get(run_id=run_id)
        actual_status = str(getattr(summary.status, "value", summary.status))
        report_paths = dict(run.report_paths)
        reports_verified = (
            run.report_status == TestExecutionRun.ReportStatus.AVAILABLE
            and all(
                (artifact_root / report_paths.get(kind, "")).is_file()
                for kind in ("json", "html", "junit")
            )
        )
        return _CaseResult(
            case_id=slug,
            case_kind=case_kind,
            title=title,
            channels=channels,
            expected_status=expected_status,
            actual_status=actual_status,
            matched_expectation=actual_status == expected_status,
            workflow_id=workflow_row.workflow_id,
            test_plan_id=test_plan.artifact_id,
            execution_plan_id=execution_plan.artifact_id,
            resource_profile_id=profile.profile_id,
            run_id=run_id,
            report_paths=report_paths,
            reports_verified=reports_verified,
        )


class Command(BaseCommand):
    help = (
        "Create three SQLite subjects and execute standalone, composite, "
        "and failure-chain acceptance cases"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--append",
            action="store_true",
            help="Allow creation when test_platform business tables are non-empty.",
        )

    def handle(self, *args, **options):
        preexisting_counts = _business_counts()
        started_from_empty = not any(preexisting_counts.values())
        if not started_from_empty and not options["append"]:
            populated = ", ".join(
                f"{name}={count}"
                for name, count in preexisting_counts.items()
                if count
            )
            raise CommandError(
                "test_platform 业务数据非空，默认拒绝追加；"
                f"如确认保留现有数据请使用 --append（{populated}）"
            )
        artifact_storage = Path(
            settings.TEST_PLATFORM_ARTIFACT_ROOT
        ).resolve()
        media_storage = Path(settings.MEDIA_ROOT).resolve()
        preexisting_storage_file_counts = {
            "uploads": _file_count(media_storage),
            "artifacts": _file_count(artifact_storage),
        }
        root = (
            artifact_storage / "comprehensive-validation"
        )
        root.mkdir(parents=True, exist_ok=True)
        run_tag = (
            datetime.now(datetime_timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid4().hex[:6]
        )
        # Every append run owns immutable database inputs.  Reusing fixed files
        # would silently change the resource behind an older approved plan.
        databases = _write_database_fixtures(root / "runtime" / run_tag)
        database_by_key = {item.key: item for item in databases}
        knowledge = {
            item.key: _create_knowledge(
                fixture=item,
                run_tag=run_tag,
            )
            for item in databases
        }
        database_profiles = {
            item.key: _create_database_profile(
                fixture=item,
                run_tag=run_tag,
            )
            for item in databases
        }
        case_results: list[_CaseResult] = []
        with _local_services() as services:
            composite_profile = _create_composite_profile(
                fixture=database_by_key["commerce"],
                services=services,
                run_tag=run_tag,
            )
            runtime_context = {
                "database_connections": {
                    item.connection_ref: str(item.path.resolve())
                    for item in databases
                },
                "max_performance_duration_seconds": 5,
                "max_virtual_users": 10,
            }
            with override_settings(
                TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY="",
                TEST_PLATFORM_RUNTIME_CONTEXT_JSON=json.dumps(runtime_context),
            ):
                builder = _MatrixBuilder(
                    root=root,
                    run_tag=run_tag,
                )
                for fixture in databases:
                    standalone_request = (
                        f"REQ-MATRIX-{run_tag}-db-{fixture.key}"
                    ).upper()
                    requirement_id = standalone_request + "-ITEM"
                    title = {
                        "identity": "身份账号状态单库验收",
                        "commerce": "电商库存单库验收",
                        "analytics": "分析指标单库验收",
                    }[fixture.key]
                    case_results.append(
                        builder.execute_case(
                            slug=f"db-{fixture.key}",
                            case_kind="standalone_database",
                            title=title,
                            system_id=fixture.system_id,
                            channels=["database"],
                            requirement_text=(
                                f"在 {fixture.system_id} 中执行审批只读 SQL，"
                                f"确认 {fixture.check_column} 等于 {fixture.expected!r}。"
                            ),
                            knowledge_scope_ids=[
                                knowledge[fixture.key].scope_id
                            ],
                            profile=database_profiles[fixture.key],
                            design_candidate=_design_candidate(
                                title,
                                [
                                    _scenario(
                                        title=title,
                                        requirement_id=requirement_id,
                                        channel="database",
                                        expected_text=(
                                            f"{fixture.check_column} 应为 "
                                            f"{fixture.expected!r}"
                                        ),
                                        expected=fixture.expected,
                                        operator="equals",
                                    )
                                ],
                            ),
                            plan_candidate=_database_plan_candidate(
                                design_id=f"design-{standalone_request}",
                                fixture=fixture,
                                knowledge_scope_id=knowledge[
                                    fixture.key
                                ].scope_id,
                            ),
                            expected_status="passed",
                        )
                    )

                single_channel_cases = [
                    {
                        "slug": "api-only",
                        "title": "电商健康接口单项验收",
                        "channel": "api",
                        "requirement": "单独验证电商健康接口返回 HTTP 200。",
                        "scenario_title": "健康接口返回 200",
                        "expected_text": "HTTP 状态应为 200",
                        "expected": 200,
                        "operator": "equals",
                        "executor_kind": "http_api",
                        "catalog_ref": "api.operation.matrixHealth",
                        "observable_ref": "api.status.matrixHealth",
                    },
                    {
                        "slug": "performance-only",
                        "title": "电商健康接口性能单项验收",
                        "channel": "performance",
                        "requirement": (
                            "单独以 live 模式执行本地健康接口轻量压力检查，"
                            "确认 p95 不超过 1000 ms。"
                        ),
                        "scenario_title": "健康接口 p95 满足阈值",
                        "expected_text": "p95 延迟应不超过 1000 ms",
                        "expected": 1000,
                        "operator": "lte",
                        "executor_kind": "performance",
                        "catalog_ref": "perf.matrix.commerce-smoke",
                        "observable_ref": "observable.perf.matrix-p95",
                    },
                    {
                        "slug": "tcp-only",
                        "title": "电商网关 TCP 单项验收",
                        "channel": "port",
                        "requirement": "单独连接已登记的本地 TCP 服务端口并确认 open。",
                        "scenario_title": "登记 TCP 端口可连接",
                        "expected_text": "端口状态应为 open",
                        "expected": "open",
                        "operator": "equals",
                        "executor_kind": "tcp_port",
                        "catalog_ref": (
                            f"port.probe.{composite_profile.profile_id}"
                        ),
                        "observable_ref": (
                            f"port.state.{composite_profile.profile_id}"
                        ),
                    },
                ]
                for spec in single_channel_cases:
                    single_request = (
                        f"REQ-MATRIX-{run_tag}-{spec['slug']}"
                    ).upper()
                    single_design_id = f"design-{single_request}"
                    case_results.append(
                        builder.execute_case(
                            slug=str(spec["slug"]),
                            case_kind=f"standalone_{spec['channel']}",
                            title=str(spec["title"]),
                            system_id=database_by_key["commerce"].system_id,
                            channels=[str(spec["channel"])],
                            requirement_text=str(spec["requirement"]),
                            knowledge_scope_ids=[],
                            profile=composite_profile,
                            design_candidate=_design_candidate(
                                str(spec["title"]),
                                [
                                    _scenario(
                                        title=str(spec["scenario_title"]),
                                        requirement_id=(
                                            f"{single_request}-ITEM"
                                        ),
                                        channel=str(spec["channel"]),
                                        expected_text=str(
                                            spec["expected_text"]
                                        ),
                                        expected=spec["expected"],
                                        operator=str(spec["operator"]),
                                    )
                                ],
                            ),
                            plan_candidate=_single_catalog_plan_candidate(
                                design_id=single_design_id,
                                executor_kind=str(spec["executor_kind"]),
                                catalog_ref=str(spec["catalog_ref"]),
                                observable_ref=str(
                                    spec["observable_ref"]
                                ),
                            ),
                            expected_status="passed",
                        )
                    )

                composite_request = (
                    f"REQ-MATRIX-{run_tag}-composite-commerce"
                ).upper()
                composite_design_id = f"design-{composite_request}"
                composite_requirement_id = f"{composite_request}-ITEM"
                case_results.append(
                    builder.execute_case(
                        slug="composite-commerce",
                        case_kind="composite",
                        title="电商网关 API + DB + 性能 + TCP 复合验收",
                        system_id=database_by_key["commerce"].system_id,
                        channels=["api", "database", "performance", "port"],
                        requirement_text=(
                            "组合验证本地电商网关健康接口、库存只读数据、"
                            "轻量压力阈值和登记 TCP 端口。"
                        ),
                        knowledge_scope_ids=[knowledge["commerce"].scope_id],
                        profile=composite_profile,
                        design_candidate=_design_candidate(
                            "电商网关四通道复合验收",
                            [_composite_scenario(composite_requirement_id)],
                        ),
                        plan_candidate=_composite_plan_candidate(
                            design_id=composite_design_id,
                            profile=composite_profile,
                            fixture=database_by_key["commerce"],
                            knowledge_scope_id=knowledge["commerce"].scope_id,
                        ),
                        expected_status="passed",
                    )
                )

                failure_request = (
                    f"REQ-MATRIX-{run_tag}-expected-failure"
                ).upper()
                failure_design_id = f"design-{failure_request}"
                database_failure_case = builder.execute_case(
                        slug="expected-failure",
                        case_kind="expected_failure",
                        title="身份状态错误预期链路",
                        system_id=database_by_key["identity"].system_id,
                        channels=["database"],
                        requirement_text=(
                            "故意声明 alice 状态为 locked，用来验证真实断言失败、"
                            "失败报告和数据库运行历史。"
                        ),
                        knowledge_scope_ids=[knowledge["identity"].scope_id],
                        profile=database_profiles["identity"],
                        design_candidate=_design_candidate(
                            "身份状态预期失败验证",
                            [
                                _scenario(
                                    title="alice 状态错误预期",
                                    requirement_id=f"{failure_request}-ITEM",
                                    channel="database",
                                    expected_text="alice 状态应为 locked",
                                    expected="locked",
                                    operator="equals",
                                    technique="positive",
                                )
                            ],
                        ),
                        plan_candidate=_database_plan_candidate(
                            design_id=failure_design_id,
                            fixture=database_by_key["identity"],
                            knowledge_scope_id=knowledge["identity"].scope_id,
                            expected="locked",
                        ),
                        expected_status="failed",
                    )
                case_results.append(database_failure_case)
                database_failure_verification = (
                    _verify_database_expected_failure(
                        database_failure_case,
                        database_by_key["identity"],
                    )
                )

                chain_request = (
                    f"REQ-MATRIX-{run_tag}-failure-chain"
                ).upper()
                chain_design_id = f"design-{chain_request}"
                failure_chain_case = builder.execute_case(
                    slug="failure-chain",
                    case_kind="upstream_failure_chain",
                    title="API 失败后数据库阶段阻断验收",
                    system_id=database_by_key["commerce"].system_id,
                    channels=["api", "database"],
                    requirement_text=(
                        "请求固定返回 500 的接口；第一阶段断言失败后，"
                        "协调器必须阻断后续数据库阶段且不得启动其外部动作。"
                    ),
                    knowledge_scope_ids=[knowledge["commerce"].scope_id],
                    profile=composite_profile,
                    design_candidate=_design_candidate(
                        "API 上游失败阻断后序数据库",
                        [_failure_chain_scenario(f"{chain_request}-ITEM")],
                    ),
                    plan_candidate=_failure_chain_plan_candidate(
                        design_id=chain_design_id,
                        fixture=database_by_key["commerce"],
                        knowledge_scope_id=knowledge["commerce"].scope_id,
                    ),
                    expected_status="failed",
                )
                case_results.append(failure_chain_case)
                failure_chain_verification = _verify_failure_chain(
                    failure_chain_case
                )

        profile_ids = [
            *(item.pk for item in database_profiles.values()),
            composite_profile.pk,
        ]
        TestResourceProfile.objects.filter(pk__in=profile_ids).update(
            enabled=False,
            updated_at=timezone.now(),
        )
        retired_execution_plan_ids = [item.execution_plan_id for item in case_results]
        ExecutionPlanArtifact.objects.filter(
            artifact_id__in=retired_execution_plan_ids,
            status=ExecutionPlanArtifact.Status.APPROVED,
        ).update(
            status=ExecutionPlanArtifact.Status.SUPERSEDED,
            updated_at=timezone.now(),
        )
        matrix_rows = []
        for item in case_results:
            row = item.as_dict()
            row.update(
                {
                    "retryable": False,
                    "disabled_after_run": True,
                }
            )
            matrix_rows.append(row)
        post_counts = _business_counts()
        all_matched = all(
            item.matched_expectation and item.reports_verified
            for item in case_results
        ) and database_failure_verification["verified"] and (
            failure_chain_verification["verified"]
        )
        matrix_complete = all_matched
        summary = {
            "schema_version": "test_conductor-simulated-matrix.v1",
            "run_tag": run_tag,
            "generated_at": datetime.now(
                datetime_timezone.utc
            ).isoformat(),
            "candidate_source": "deterministic_fixture_gateway",
            "external_model_calls": 0,
            "started_from_empty": started_from_empty,
            "business_tables_started_empty": started_from_empty,
            "preexisting_business_counts": preexisting_counts,
            "preexisting_storage_file_counts": (
                preexisting_storage_file_counts
            ),
            "fixture_databases": [
                {
                    "key": item.key,
                    "path": str(item.path.resolve()),
                    "connection_ref": item.connection_ref,
                    "table": item.table,
                }
                for item in databases
            ],
            "knowledge_entries": [
                {
                    "knowledge_id": item.knowledge_id,
                    "scope_id": item.scope_id,
                    "system_id": item.system_id,
                    "status": item.status,
                    "source_file": item.source_file.name,
                }
                for item in knowledge.values()
            ],
            "resource_profiles": [
                {
                    "profile_id": item.profile_id,
                    "name": item.name,
                    "system_id": item.system_id,
                    "channels": sorted(item.configured_channels()),
                }
                for item in TestResourceProfile.objects.filter(name__contains=run_tag)
            ],
            "matrix": matrix_rows,
            "expected_failure_verification": (
                database_failure_verification
            ),
            "failure_chain_verification": failure_chain_verification,
            "disabled_profile_ids_after_run": [
                item.profile_id
                for item in TestResourceProfile.objects.filter(
                    pk__in=profile_ids
                )
            ],
            "retired_execution_plan_ids_after_run": (
                retired_execution_plan_ids
            ),
            "runtime_limitations": [
                (
                    "本命令的 API、性能和 TCP 复合资源使用命令进程内的临时本地"
                    "服务，数据库连接映射也只在命令运行期间注入。相关资源已在"
                    "命令结束前禁用，相关执行计划已标记为 superseded，不能从后台"
                    "误重试；历史和 JSON/HTML/JUnit 报告仍可查看，内部执行产物仅供后端追踪。"
                )
            ],
            "post_business_counts": post_counts,
            "all_expectations_matched": all_matched,
            "matrix_complete": matrix_complete,
        }
        summary_path = root / f"matrix-summary-{run_tag}.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            json.dumps(
                {
                    "summary_path": str(summary_path.resolve()),
                    "started_from_empty": started_from_empty,
                    "all_expectations_matched": all_matched,
                    "matrix_complete": matrix_complete,
                    "matrix": [
                        {
                            "case_id": item.case_id,
                            "channels": item.channels,
                            "expected": item.expected_status,
                            "actual": item.actual_status,
                            "reports_verified": item.reports_verified,
                        }
                        for item in case_results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not all_matched:
            raise CommandError(
                f"矩阵存在非预期结果，已保留完整报告: {summary_path}"
            )
