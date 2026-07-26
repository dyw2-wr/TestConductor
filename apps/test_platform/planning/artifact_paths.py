"""Stable category-first paths for generated executor files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any


EXECUTOR_ARTIFACT_CATEGORIES = {
    "procedure_playwright": "ui",
    "http_api": "api",
    "database": "database",
    "performance": "performance",
    "tcp_port": "port",
}


def artifact_category(executor_kind: Any) -> str:
    value = getattr(executor_kind, "value", executor_kind)
    category = EXECUTOR_ARTIFACT_CATEGORIES.get(str(value or ""))
    if category is None:
        raise ValueError(f"未识别的执行器类型: {value}")
    return category


def generated_files_path(executor_kind: Any) -> PurePosixPath:
    return PurePosixPath("generated-files", artifact_category(executor_kind))


def generated_files_root(root: str | Path, executor_kind: Any) -> Path:
    return Path(root).joinpath(*generated_files_path(executor_kind).parts)


__all__ = [
    "EXECUTOR_ARTIFACT_CATEGORIES",
    "artifact_category",
    "generated_files_path",
    "generated_files_root",
]
