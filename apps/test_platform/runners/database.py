"""Database executor for exact SQL approved in an execution plan.

Current plans always carry the complete SQL.  The runner revalidates the
statement against the injected schema boundary before opening the connection.
Historical v4/v5 read-only artifacts remain runnable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from time import perf_counter
from typing import Any, Mapping
from uuid import uuid4

from apps.test_platform.database_sql import validate_database_sql, validate_read_only_sql

from .base import (
    ArtifactWorkspace,
    artifact_stage_identity,
    load_json_payload,
    prepare_artifact,
    write_evidence,
)
from .contracts import (
    DatabaseConnection,
    RunResult,
    RunStatus,
    RunnerError,
    RuntimeContext,
    StepResult,
    finish_result,
)


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
_COMPARISON_OPERATORS = {
    "equals", "not_equals", "contains", "exists", "not_exists",
    "gt", "gte", "lt", "lte",
}
_ROW_COUNT_OPERATORS = {
    "equals", "not_equals", "gt", "gte", "lt", "lte",
}
_EXISTS_OPERATORS = {"equals", "not_equals", "exists", "not_exists"}
_COLUMN_SPECIAL_OPERATORS = {"not_null", "null"}


@dataclass(frozen=True)
class _ValidatedQuery:
    query_id: str
    sql: str
    parameters: Any
    assertions: list[Mapping[str, Any]]
    execution_policy: str = "read_only"


@dataclass(frozen=True)
class _ValidatedPlan:
    workspace: ArtifactWorkspace
    connection_value: Any
    max_rows: int
    queries: list[_ValidatedQuery]
    contains_writes: bool = False


def _lookup(mapping: Mapping[str, Any], name: str) -> Any:
    current: Any = mapping
    for part in name.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise RunnerError("RUNTIME_RESOURCE_MISSING", f"未注入变量: {name}")
        current = current[part]
    return current


def _resolve(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _resolve(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    match = _PLACEHOLDER_RE.fullmatch(value.strip())
    if match:
        return _lookup(variables, match.group(1))

    def replace(item: re.Match[str]) -> str:
        resolved = _lookup(variables, item.group(1))
        if isinstance(resolved, (Mapping, list, tuple)):
            raise RunnerError("QUERY_TEMPLATE_INVALID", f"对象变量不能嵌入字符串: {item.group(1)}")
        return str(resolved)

    return _PLACEHOLDER_RE.sub(replace, value)


def _safe_select(sql: str) -> str:
    try:
        return validate_read_only_sql(sql)
    except ValueError as exc:
        raise RunnerError("QUERY_NOT_READ_ONLY", str(exc)) from exc


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    operator = operator.lower()
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "contains":
        try:
            return expected in actual
        except TypeError:
            return False
    if operator == "exists":
        return actual is not None
    if operator == "not_exists":
        return actual is None
    if operator == "gt":
        return actual > expected
    if operator == "gte":
        return actual >= expected
    if operator == "lt":
        return actual < expected
    if operator == "lte":
        return actual <= expected
    raise RunnerError("ASSERTION_INVALID", f"不支持的数据库断言操作符: {operator}")


def _validate_connection_value(value: Any, *, requires_write: bool = False) -> None:
    """Accept SQLite files or an explicitly permissioned DB-API connection."""

    if value is None:
        raise RunnerError("RUNTIME_RESOURCE_MISSING", "未注入数据库连接")
    if isinstance(value, (str, Path)):
        path = Path(value)
        if not path.is_file():
            raise RunnerError("RUNTIME_RESOURCE_MISSING", f"SQLite 文件不存在: {path}")
        return
    if isinstance(value, DatabaseConnection):
        if requires_write and value.access_mode != "read_write":
            raise RunnerError(
                "RUNTIME_RESOURCE_INVALID",
                "数据库写计划需要 access_mode=read_write 的运行时连接",
            )
        return
    raise RunnerError(
        "RUNTIME_RESOURCE_INVALID",
        "database runner 只接受 SQLite 文件路径或 DatabaseConnection",
    )


def _connection_for(value: Any, *, requires_write: bool = False) -> tuple[Any, bool]:
    _validate_connection_value(value, requires_write=requires_write)
    if isinstance(value, (str, Path)):
        path = Path(value)
        mode = "rw" if requires_write else "ro"
        conn = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode={mode}",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        return conn, True
    if isinstance(value, DatabaseConnection):
        return value.connection, value.close_when_done
    raise AssertionError("连接类型应已由 _validate_connection_value 校验")


class DatabaseRunner:
    executor_kind = "database"
    payload_schemas = {
        "database-execution-plan.v4",
        "database-execution-plan.v5",
        "database-execution-plan.v6",
    }

    def preflight(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> None:
        """完成数据库 stage 的全部静态门禁，不创建连接。"""

        self._validate_plan(artifact_dir, artifact_bundle, context)

    def run(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> RunResult:
        flow_id, stage_id = artifact_stage_identity(artifact_bundle)
        result = RunResult.new(
            run_id=f"run-{uuid4().hex}",
            executor_kind=self.executor_kind,
            flow_id=flow_id,
            stage_id=stage_id,
        )
        external_action_started = [False]
        try:
            validated = self._validate_plan(artifact_dir, artifact_bundle, context)
            connection, owned = _connection_for(
                validated.connection_value,
                requires_write=validated.contains_writes,
            )
            try:
                for index, query in enumerate(validated.queries):
                    self._run_query(
                        result,
                        index,
                        query,
                        connection,
                        context,
                        validated.max_rows,
                        external_action_started,
                    )
                if validated.contains_writes:
                    connection.commit()
            except Exception:
                if validated.contains_writes:
                    rollback = getattr(connection, "rollback", None)
                    if callable(rollback):
                        rollback()
                raise
            finally:
                if owned:
                    connection.close()
            if result.steps:
                result.status = (
                    RunStatus.PASSED
                    if all(step.status == RunStatus.PASSED for step in result.steps)
                    else RunStatus.FAILED
                )
        except RunnerError as exc:
            result.errors.append(f"{exc.code}: {exc.message}")
            result.status = RunStatus.BLOCKED if exc.code in {
                "RUNTIME_RESOURCE_MISSING", "RUNTIME_RESOURCE_INVALID", "ARTIFACT_SCHEMA_INVALID",
                "QUERY_NOT_READ_ONLY", "QUERY_INVALID", "QUERY_TEMPLATE_INVALID",
                "ARTIFACT_DIR_MISSING", "ARTIFACT_PATH_INVALID", "MANIFEST_MISSING", "MANIFEST_INVALID",
                "MANIFEST_IDENTITY_MISMATCH", "MANIFEST_REF_MISSING", "ARTIFACT_REFS_MISSING",
                "ARTIFACT_MISSING", "ARTIFACT_HASH_INVALID", "ARTIFACT_HASH_MISMATCH", "EXECUTOR_MISMATCH",
                "ARTIFACT_PAYLOAD_MISSING", "ARTIFACT_PAYLOAD_INVALID", "ASSERTION_INVALID",
            } else RunStatus.ERROR
        except Exception as exc:  # pragma: no cover - driver-specific failures
            result.errors.append(f"DATABASE_RUN_ERROR: {exc}")
            result.status = RunStatus.ERROR
        finally:
            result.external_action_started = external_action_started[0]
            finish_result(result, result.status)
        return result

    def _validate_plan(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> _ValidatedPlan:
        workspace = prepare_artifact(artifact_dir, artifact_bundle, expected_executor=self.executor_kind)
        payload_path, payload = load_json_payload(workspace, "payload")
        schema_version = payload.get("schema_version")
        if schema_version not in self.payload_schemas:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"{payload_path.name} 不是受支持的数据库执行计划",
            )
        common_payload_keys = {
            "schema_version",
            "executor_kind",
            "flow_id",
            "stage_id",
            "design_id",
            "design_version",
            "plan_id",
            "plan_version",
            "connection_profile_ref",
        }
        if schema_version == "database-execution-plan.v6":
            allowed_payload_keys = common_payload_keys | {
                "contains_writes",
                "warnings",
                "statements",
            }
        else:
            allowed_payload_keys = common_payload_keys | {"read_only", "queries"}
        if set(payload) != allowed_payload_keys:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "database payload 字段与对应版本不一致",
            )
        if (
            schema_version != "database-execution-plan.v6"
            and payload.get("read_only") is not True
        ):
            raise RunnerError("QUERY_NOT_READ_ONLY", "database 计划必须声明 read_only=true")
        contains_writes = (
            payload.get("contains_writes")
            if schema_version == "database-execution-plan.v6"
            else False
        )
        if type(contains_writes) is not bool:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "database v6 contains_writes 必须是 bool",
            )
        if schema_version == "database-execution-plan.v6":
            warnings = payload.get("warnings")
            if not isinstance(warnings, list) or any(
                not isinstance(item, str) or not item.strip() for item in warnings
            ):
                raise RunnerError(
                    "ARTIFACT_SCHEMA_INVALID",
                    "database v6 warnings 必须是字符串数组",
                )
            if contains_writes and not warnings:
                raise RunnerError(
                    "ARTIFACT_SCHEMA_INVALID",
                    "数据库写计划必须携带高风险警告",
                )

        if "connection_ref" in payload:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "database v4 只接受 connection_profile_ref",
            )
        connection_ref_value = payload.get("connection_profile_ref")
        if not isinstance(connection_ref_value, str) or not connection_ref_value.strip():
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "database 计划缺少 connection_profile_ref")
        connection_ref = connection_ref_value.strip()
        if connection_ref not in context.database_connections:
            raise RunnerError("RUNTIME_RESOURCE_MISSING", f"未注入数据库连接: {connection_ref}")
        connection_value = context.database_connections[connection_ref]
        _validate_connection_value(
            connection_value,
            requires_write=contains_writes,
        )

        if "max_rows" in payload:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "database v4 的 max_rows 由 runner 策略拥有，artifact 不能覆盖",
            )
        max_rows = 1000

        query_specs = payload.get(
            "statements" if schema_version == "database-execution-plan.v6" else "queries"
        )
        if not isinstance(query_specs, list) or not query_specs:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "数据库计划必须包含非空 SQL")
        traceability = workspace.manifest.get("traceability")
        allowed_expected_results: set[str] | None = None
        if isinstance(traceability, Mapping):
            expected_trace = traceability.get("expected_results")
            if isinstance(expected_trace, list):
                allowed_expected_results = {
                    str(item) for item in expected_trace if str(item).strip()
                }
        queries = [
            self._validate_query(
                index,
                spec,
                context,
                allowed_expected_results,
                connection_ref=connection_ref,
                schema_version=schema_version,
            )
            for index, spec in enumerate(query_specs)
        ]
        actual_contains_writes = any(
            item.execution_policy == "write" for item in queries
        )
        if actual_contains_writes != contains_writes:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "contains_writes 与 SQL execution_policy 不一致",
            )
        return _ValidatedPlan(
            workspace=workspace,
            connection_value=connection_value,
            max_rows=max_rows,
            queries=queries,
            contains_writes=contains_writes,
        )

    def _validate_query(
        self,
        index: int,
        spec: Any,
        context: RuntimeContext,
        allowed_expected_results: set[str] | None,
        *,
        connection_ref: str,
        schema_version: str,
    ) -> _ValidatedQuery:
        if not isinstance(spec, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"queries[{index}] 必须是对象")
        legacy_query_keys = {
            "query_id",
            "source",
            "operation_ref",
            "query_ref",
            "parameters_refs",
            "assertions",
            # Kept in the DTO allowlist only so _validate_query can return the
            # dedicated QUERY_NOT_READ_ONLY error instead of silently ignoring it.
            "sql",
            "query",
            "statement",
            "parameters",
            "sql_origin",
            "knowledge_scope_id",
        }
        current_statement_keys = {
            "statement_id",
            "source",
            "operation_ref",
            "execution_policy",
            "risk_level",
            "parameters_refs",
            "assertions",
            "sql",
            "sql_origin",
            "knowledge_scope_id",
        }
        allowed_query_keys = (
            current_statement_keys
            if schema_version == "database-execution-plan.v6"
            else legacy_query_keys
        )
        if not set(spec).issubset(allowed_query_keys):
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"queries[{index}] 包含 adapter 未声明字段",
            )
        query_id_value = spec.get(
            "statement_id"
            if schema_version == "database-execution-plan.v6"
            else "query_id"
        )
        if not isinstance(query_id_value, str) or not query_id_value.strip():
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"SQL[{index}] 缺少有效 ID",
            )
        query_id = query_id_value.strip()
        query_ref_value = spec.get("query_ref")
        sql_value = spec.get("sql")
        execution_policy = str(spec.get("execution_policy") or "read_only")
        if schema_version == "database-execution-plan.v6":
            expected_risk = "high" if execution_policy == "write" else "normal"
            if spec.get("risk_level") != expected_risk:
                raise RunnerError(
                    "ARTIFACT_SCHEMA_INVALID",
                    f"{query_id}.risk_level 与 execution_policy 不一致",
                )
        if sql_value is not None:
            if (
                schema_version not in {
                    "database-execution-plan.v5",
                    "database-execution-plan.v6",
                }
                or query_ref_value is not None
                or any(key in spec for key in ("query", "statement", "parameters"))
                or spec.get("sql_origin") not in {
                    "knowledge_reused",
                    "ai_generated",
                }
            ):
                raise RunnerError(
                    "QUERY_NOT_READ_ONLY",
                    "AI SQL artifact 必须使用 v5、声明来源且不能混用 query_ref/literal parameters",
                )
            schema = context.database_schemas.get(connection_ref)
            if not isinstance(schema, Mapping):
                raise RunnerError(
                    "RUNTIME_RESOURCE_MISSING",
                    f"未找到数据库结构约束: {connection_ref}",
                )
            tables = [
                str(item.get("name") or "")
                for item in schema.get("tables") or []
                if isinstance(item, Mapping)
            ]
            allowed_parameter_refs = schema.get("allowed_parameter_refs") or []
            allowed_columns = {
                str(item.get("name") or ""): [
                    str(column.get("name") or "")
                    for column in item.get("columns") or []
                    if isinstance(column, Mapping)
                ]
                for item in schema.get("tables") or []
                if isinstance(item, Mapping)
            }
            try:
                sql = validate_database_sql(
                    str(sql_value),
                    execution_policy=execution_policy,
                    allowed_tables=tables,
                    allowed_columns=allowed_columns,
                    allowed_parameter_refs=allowed_parameter_refs,
                    parameters_refs=dict(spec.get("parameters_refs") or {}),
                )
            except ValueError as exc:
                raise RunnerError("QUERY_INVALID", str(exc)) from exc
        else:
            if schema_version == "database-execution-plan.v6":
                raise RunnerError(
                    "QUERY_INVALID",
                    "database v6 必须包含本次审批的完整 SQL",
                )
            if (
                not isinstance(query_ref_value, str)
                or not query_ref_value.strip()
                or any(key in spec for key in ("query", "statement", "parameters"))
            ):
                raise RunnerError(
                    "QUERY_NOT_READ_ONLY",
                    "database artifact 必须引用已登记 query_ref 或携带已审批 AI SQL",
                )
            query_ref = query_ref_value.strip()
            if query_ref not in context.query_catalog:
                raise RunnerError(
                    "RUNTIME_RESOURCE_MISSING",
                    f"未找到 query_ref: {query_ref}",
                )
            catalog_entry = context.query_catalog[query_ref]
            if isinstance(catalog_entry, Mapping):
                if catalog_entry.get("read_only") is not True:
                    raise RunnerError(
                        "QUERY_NOT_READ_ONLY",
                        f"query_ref 未明确登记为只读: {query_ref}",
                    )
                sql_value = catalog_entry.get("sql")
            else:
                sql_value = catalog_entry
            if not isinstance(sql_value, str) or not sql_value.strip():
                raise RunnerError(
                    "QUERY_INVALID",
                    f"query_ref 没有有效 SQL: {query_ref}",
                )
            sql = _safe_select(_resolve(sql_value, context.variables))

        parameters_refs = spec.get("parameters_refs")
        if parameters_refs is None:
            parameters: Any = {}
        elif isinstance(parameters_refs, Mapping):
            parameters = {}
            for parameter, variable_ref in parameters_refs.items():
                if (
                    not isinstance(parameter, str)
                    or not parameter.strip()
                    or not isinstance(variable_ref, str)
                    or not variable_ref.strip()
                ):
                    raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{query_id}.parameters_refs 必须映射有效变量名")
                parameters[parameter.strip()] = _lookup(context.variables, variable_ref.strip())
        else:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"{query_id}.parameters_refs 必须是参数名到 runtime variable ref 的对象",
            )
        parameters = _resolve(parameters, context.variables)

        assertion_specs = spec.get("assertions")
        if not isinstance(assertion_specs, list):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{query_id}.assertions 必须是数组")
        assertions = [
            self._validate_assertion(
                query_id,
                assertion_index,
                assertion,
                context,
                allowed_expected_results,
            )
            for assertion_index, assertion in enumerate(assertion_specs)
        ]
        return _ValidatedQuery(
            query_id=query_id,
            sql=sql,
            parameters=parameters,
            assertions=assertions,
            execution_policy=execution_policy,
        )

    @staticmethod
    def _validate_assertion(
        query_id: str,
        index: int,
        assertion: Any,
        context: RuntimeContext,
        allowed_expected_results: set[str] | None,
    ) -> Mapping[str, Any]:
        if not isinstance(assertion, Mapping):
            raise RunnerError("ASSERTION_INVALID", f"{query_id}.assertions[{index}] 必须是对象")
        expected_result_id = str(assertion.get("expected_result_id") or "").strip()
        if not expected_result_id:
            raise RunnerError(
                "ASSERTION_INVALID",
                f"{query_id}.assertions[{index}] 缺少 expected_result_id",
            )
        if (
            allowed_expected_results is not None
            and expected_result_id not in allowed_expected_results
        ):
            raise RunnerError(
                "ASSERTION_INVALID",
                f"{query_id}.assertions[{index}] 引用了 manifest 未声明的 expected_result_id: {expected_result_id}",
            )
        kind = str(assertion.get("kind") or "").strip().lower()
        operator = str(assertion.get("operator") or "").strip().lower()
        if kind in {"row_count", "affected_rows"}:
            allowed_operators = _ROW_COUNT_OPERATORS
        elif kind == "exists":
            allowed_operators = _EXISTS_OPERATORS
        elif kind == "column":
            if not str(assertion.get("column") or "").strip():
                raise RunnerError("ASSERTION_INVALID", f"{query_id}.assertions[{index}] 列断言缺少 column")
            allowed_operators = _COMPARISON_OPERATORS | _COLUMN_SPECIAL_OPERATORS
        else:
            raise RunnerError("ASSERTION_INVALID", f"{query_id}.assertions[{index}] 不支持 kind: {kind}")
        if operator not in allowed_operators:
            raise RunnerError(
                "ASSERTION_INVALID",
                f"{query_id}.assertions[{index}] 的 operator 不受支持: {operator}",
            )
        normalized = dict(assertion)
        normalized["expected_result_id"] = expected_result_id
        normalized["kind"] = kind
        normalized["operator"] = operator
        if "expected" in normalized:
            normalized["expected"] = _resolve(normalized["expected"], context.variables)
        return normalized

    def _run_query(
        self,
        result: RunResult,
        index: int,
        query: _ValidatedQuery,
        connection: Any,
        context: RuntimeContext,
        max_rows: int,
        external_action_started: list[bool],
    ) -> None:
        query_id = query.query_id
        started = perf_counter()
        cursor = connection.cursor()
        try:
            external_action_started[0] = True
            cursor.execute(query.sql, query.parameters)
            affected_rows = max(int(cursor.rowcount or 0), 0)
            columns = [str(item[0]) for item in (cursor.description or [])]
            rows = cursor.fetchmany(max_rows + 1) if cursor.description else []
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        normalized_rows = [dict(zip(columns, row)) for row in rows]
        assertion_results: list[dict[str, Any]] = []
        passed = True
        for assertion in query.assertions:
            expected_result_id = assertion.get("expected_result_id")
            ok, message = self._assert_query(
                assertion,
                normalized_rows,
                columns,
                affected_rows,
            )
            assertion_results.append(
                {
                    "passed": ok,
                    "expected_result_id": expected_result_id,
                    "message": message,
                }
            )
            passed = passed and ok
        if truncated:
            passed = False
            result.errors.append(f"QUERY_RESULT_TRUNCATED: {query_id} 超过 max_rows")
        evidence_name = write_evidence(
            context,
            f"{result.run_id}-{index + 1}-{query_id}",
            {
                "query_id": query_id,
                "row_count": len(normalized_rows),
                "affected_rows": affected_rows,
                "truncated": truncated,
                "columns": columns,
                "assertions": assertion_results,
            },
        )
        if evidence_name:
            result.evidence.append(evidence_name)
        result.steps.append(
            StepResult(
                step_id=query_id,
                status=RunStatus.PASSED if passed else RunStatus.FAILED,
                message="all assertions passed" if passed else "one or more assertions failed",
                duration_ms=(perf_counter() - started) * 1000,
                details={
                    "row_count": len(normalized_rows),
                    "affected_rows": affected_rows,
                    "truncated": truncated,
                    "assertions": assertion_results,
                },
                evidence=[evidence_name] if evidence_name else [],
            )
        )

    @staticmethod
    def _assert_query(
        assertion: Any,
        rows: list[dict[str, Any]],
        columns: list[str],
        affected_rows: int,
    ) -> tuple[bool, str]:
        if not isinstance(assertion, Mapping):
            return False, "数据库断言必须是对象"
        kind = str(assertion.get("kind") or "").lower()
        operator = str(assertion.get("operator") or "")
        expected = assertion.get("expected")
        if kind == "row_count":
            actual = len(rows)
            try:
                ok = _compare(actual, operator, expected)
            except (TypeError, ValueError):
                ok = False
            return ok, f"row_count {operator}: {'passed' if ok else 'failed'}"
        if kind == "affected_rows":
            try:
                ok = _compare(affected_rows, operator, expected)
            except (TypeError, ValueError):
                ok = False
            return (
                ok,
                f"affected_rows {operator} expected={expected!r} "
                f"actual={affected_rows!r}: {'passed' if ok else 'failed'}",
            )
        if kind == "exists":
            actual = bool(rows)
            if operator in {"equals", "not_equals"} and not isinstance(expected, bool):
                return False, "exists equals/not_equals 的 expected 必须是 bool"
            if operator == "equals":
                expected_value = expected
                ok = actual == expected_value
            elif operator == "not_equals":
                expected_value = expected
                ok = actual != expected_value
            elif operator == "exists":
                ok = actual
            elif operator == "not_exists":
                ok = not actual
            else:
                ok = False
            return ok, f"rows exists expected={expected!r} actual={actual!r}: {'passed' if ok else 'failed'}"
        column = str(assertion.get("column", ""))
        if not column:
            return False, "列断言缺少 column"
        if column not in columns:
            return False, f"查询结果不存在列: {column}"
        if not rows:
            return False, "没有可供列断言的行"
        values = [row[column] for row in rows]
        if operator == "not_null":
            ok = all(value is not None for value in values)
        elif operator == "null":
            ok = all(value is None for value in values)
        else:
            try:
                ok = all(_compare(value, operator, expected) for value in values)
            except (TypeError, RunnerError):
                ok = False
        return ok, f"column {column} {operator}: {'passed' if ok else 'failed'}"
