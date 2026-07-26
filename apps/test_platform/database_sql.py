"""Shared safety checks for AI-authored read-only database queries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


_WRITE_SQL_RE = re.compile(
    r"\b(?:insert|update|delete|drop|alter|create|replace|truncate|attach|detach|"
    r"pragma|vacuum|reindex|grant|revoke|merge|call|execute|copy|into)\b|"
    r"\bfor\s+update\b",
    re.IGNORECASE,
)
_TABLE_RE = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.$]*)(?!\s*\()",
    re.IGNORECASE,
)
_CTE_RE = re.compile(
    r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(",
    re.IGNORECASE,
)
_PARAM_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_QUALIFIED_COLUMN_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b"
)
_TABLE_ALIAS_RE = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.$]*)"
    r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)
_SQL_WORDS = {
    "all", "and", "as", "asc", "between", "by", "case", "cast", "collate",
    "cross", "current_date", "current_time", "current_timestamp", "desc",
    "distinct", "else", "end", "escape", "except", "exists", "false", "fetch",
    "filter", "first", "following", "for", "from", "full", "group", "having",
    "in", "inner", "intersect", "into", "is", "join", "last", "left", "like",
    "limit", "natural", "not", "null", "nulls", "offset", "on", "or", "order",
    "outer", "over", "partition", "preceding", "recursive", "right", "rows",
    "select", "then", "true", "union", "using", "when", "where", "window",
    "with",
}


def validate_read_only_sql(
    sql: str,
    *,
    allowed_tables: Iterable[str] | None = None,
    allowed_columns: Mapping[str, Iterable[str]] | None = None,
    allowed_parameter_refs: Iterable[str] | None = None,
    parameters_refs: dict[str, str] | None = None,
) -> str:
    """Return normalized SQL or raise ``ValueError`` for unsafe drafts.

    The check is intentionally conservative. It accepts one SELECT/CTE statement,
    rejects comments and write/administrative keywords, binds values separately,
    and optionally limits table names and runtime parameter references.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL 不能为空")
    if len(sql.encode("utf-8")) > 20_000:
        raise ValueError("SQL 不能超过 20,000 字节")
    if "--" in sql or "/*" in sql or "*/" in sql:
        raise ValueError("AI SQL 不允许包含注释")
    normalized = sql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if ";" in normalized:
        raise ValueError("AI SQL 只能包含一条语句")
    if not re.match(r"^(?:select|with)\b", normalized, re.IGNORECASE):
        raise ValueError("AI SQL 只允许 SELECT 或 WITH 查询")
    if _WRITE_SQL_RE.search(normalized):
        raise ValueError("AI SQL 包含写入或数据库管理关键字")

    table_allowlist = {
        str(item).strip().lower()
        for item in (allowed_tables or [])
        if str(item).strip()
    }
    if table_allowlist:
        cte_names = {item.lower() for item in _CTE_RE.findall(normalized)}
        referenced_tables = {
            item.lower()
            for item in _TABLE_RE.findall(normalized)
            if item.lower() not in cte_names
        }
        unknown = sorted(referenced_tables - table_allowlist)
        if unknown:
            raise ValueError("AI SQL 引用了未登记数据表: " + "、".join(unknown))
        if not referenced_tables:
            raise ValueError("AI SQL 必须查询已登记的数据表")

    if allowed_columns is not None:
        column_allowlist = {
            str(table).strip().lower(): {
                str(column).strip().lower()
                for column in columns
                if str(column).strip()
            }
            for table, columns in allowed_columns.items()
            if str(table).strip()
        }
        table_aliases: dict[str, str] = {}
        for table, alias in _TABLE_ALIAS_RE.findall(normalized):
            table_name = table.lower()
            table_aliases[table_name] = table_name
            if alias and alias.lower() not in _SQL_WORDS:
                table_aliases[alias.lower()] = table_name
        unknown_qualified = []
        for qualifier, column in _QUALIFIED_COLUMN_RE.findall(normalized):
            table_name = table_aliases.get(qualifier.lower(), qualifier.lower())
            if (
                table_name in column_allowlist
                and column.lower() not in column_allowlist[table_name]
            ):
                unknown_qualified.append(f"{qualifier}.{column}")
        if unknown_qualified:
            raise ValueError(
                "AI SQL 引用了未登记字段: "
                + "、".join(sorted(set(unknown_qualified)))
            )

        # Also cover ordinary unqualified columns. Remove string literals and
        # known structural identifiers, then compare remaining names with the
        # union of fields published by the resource.
        without_literals = re.sub(r"'(?:''|[^'])*'", " ", normalized)
        identifiers = {item.lower() for item in _IDENTIFIER_RE.findall(without_literals)}
        function_names = {
            item.lower()
            for item in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                without_literals,
            )
        }
        aliases = {
            item.lower()
            for item in re.findall(
                r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)\b",
                without_literals,
                re.IGNORECASE,
            )
        }
        structural = (
            _SQL_WORDS
            | set(column_allowlist)
            | set(table_aliases)
            | set(table_aliases.values())
            | {item.lower() for item in _CTE_RE.findall(normalized)}
            | set(_PARAM_RE.findall(normalized))
            | function_names
            | aliases
        )
        allowed_column_names = {
            column
            for columns in column_allowlist.values()
            for column in columns
        }
        unknown_columns = sorted(
            identifiers - structural - allowed_column_names
        )
        if unknown_columns:
            raise ValueError(
                "AI SQL 引用了未登记字段: " + "、".join(unknown_columns)
            )

    declared = dict(parameters_refs or {})
    sql_parameters = set(_PARAM_RE.findall(normalized))
    # Catalog SQL is checked structurally before its parameters are resolved by
    # the legacy query registry. AI SQL always supplies parameters_refs and is
    # therefore held to the stricter exact-name check.
    if parameters_refs is not None and sql_parameters != set(declared):
        raise ValueError(
            "SQL 参数与 parameters_refs 必须精确一致；"
            f"缺少={sorted(sql_parameters - set(declared))}，"
            f"多余={sorted(set(declared) - sql_parameters)}"
        )
    if allowed_parameter_refs is not None:
        allowed_refs = {
            str(item).strip()
            for item in allowed_parameter_refs
            if str(item).strip()
        }
        unknown_refs = sorted(set(declared.values()) - allowed_refs)
        if unknown_refs:
            raise ValueError(
                "AI SQL 参数引用未在数据库资源中登记: " + "、".join(unknown_refs)
            )
    return normalized


def sql_parameter_names(sql: str) -> set[str]:
    return set(_PARAM_RE.findall(str(sql or "")))


__all__ = ["sql_parameter_names", "validate_read_only_sql"]
