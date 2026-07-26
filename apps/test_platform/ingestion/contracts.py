"""Contracts for deterministic requirement-file ingestion.

The ingestion layer sits before ``TestDesignRequest``.  It accepts frontend text
or uploaded bytes and returns ordinary ``RequirementInput`` objects.  It does
not add source fragments to the v4 design contract, call a model, or write to a
knowledge database.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Mapping

from apps.test_platform.intent.contracts import (
    DesignSelections,
    TargetSelection,
    TestDesignRequest,
)


INGESTION_SCHEMA_VERSION = "requirement-ingestion.v1"


def _safe_filename(value: str) -> str:
    text = str(value or "").replace("\\", "/")
    if not text or "\x00" in text:
        raise ValueError("文件名不能为空或包含 NUL")
    name = PurePath(text).name
    if name in {"", ".", ".."}:
        raise ValueError("文件名无效")
    if len(name) > 255:
        raise ValueError("文件名不能超过 255 个字符")
    return name


@dataclass(frozen=True)
class InputFile:
    """An uploaded file held in memory; arbitrary filesystem paths are not accepted."""

    filename: str
    data: bytes
    content_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "filename", _safe_filename(self.filename))
        if not isinstance(self.data, (bytes, bytearray, memoryview)):
            raise TypeError("InputFile.data 必须是 bytes")
        object.__setattr__(self, "data", bytes(self.data))
        if self.content_type is not None and not isinstance(self.content_type, str):
            raise TypeError("InputFile.content_type 必须是字符串或 None")

    @classmethod
    def from_path(cls, path: str) -> "InputFile":
        """Convenience for trusted CLI callers; web callers should pass uploaded bytes."""

        from pathlib import Path

        source = Path(path)
        return cls(filename=source.name, data=source.read_bytes())


@dataclass(frozen=True)
class IngestionLimits:
    """Hard limits shared by all adapters.

    Limits deliberately match the first-layer contract: ingestion never silently
    truncates or summarizes content to fit the model.
    """

    max_file_bytes: int = 20 * 1024 * 1024
    max_total_file_bytes: int = 50 * 1024 * 1024
    max_files: int = 20
    max_requirements: int = 20
    max_requirement_bytes: int = 256 * 1024
    max_total_requirement_bytes: int = 256 * 1024
    max_archive_entries: int = 2000
    max_archive_uncompressed_bytes: int = 100 * 1024 * 1024
    max_archive_entry_bytes: int = 25 * 1024 * 1024
    max_archive_ratio: int = 100
    max_pdf_pages: int = 200
    max_ocr_pages: int = 50
    max_ocr_pixels: int = 100_000_000
    max_table_cells: int = 100_000
    max_json_nodes: int = 100_000
    max_json_depth: int = 100

    def validate(self) -> "IngestionLimits":
        invalid = {
            name: value
            for name, value in self.__dict__.items()
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0
        }
        if invalid:
            raise ValueError(f"摄取限制必须为正整数: {sorted(invalid)}")
        if self.max_requirements > 20:
            raise ValueError("max_requirements 不能超过 TestDesignRequest 的 20 条限制")
        if self.max_requirement_bytes > 256 * 1024:
            raise ValueError("max_requirement_bytes 不能超过第一层 256 KiB 限制")
        if self.max_total_requirement_bytes > 256 * 1024:
            raise ValueError("max_total_requirement_bytes 不能超过第一层 256 KiB 限制")
        if self.max_file_bytes > self.max_total_file_bytes:
            raise ValueError("max_file_bytes 不能大于 max_total_file_bytes")
        if self.max_ocr_pages > self.max_pdf_pages:
            raise ValueError("max_ocr_pages 不能大于 max_pdf_pages")
        return self


@dataclass(frozen=True)
class IngestionWarning:
    code: str
    message: str
    source_name: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "source_name": self.source_name,
        }


@dataclass(frozen=True)
class SourcePreview:
    source_name: str
    source_type: str
    requirement_count: int
    warnings: tuple[IngestionWarning, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_type": self.source_type,
            "requirement_count": self.requirement_count,
            "warnings": [item.as_dict() for item in self.warnings],
        }


@dataclass(frozen=True)
class AdapterResult:
    source_type: str
    requirements: tuple[tuple[str | None, str], ...]
    warnings: tuple[IngestionWarning, ...] = ()


@dataclass(frozen=True)
class IngestionResult:
    """Prepared request plus transient preview information for the frontend."""

    request: TestDesignRequest
    warnings: tuple[IngestionWarning, ...] = ()
    sources: tuple[SourcePreview, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INGESTION_SCHEMA_VERSION,
            "request": self.request.model_dump(mode="json"),
            "warnings": [item.as_dict() for item in self.warnings],
            "sources": [item.as_dict() for item in self.sources],
        }


class IngestionError(ValueError):
    """A user-correctable ingestion failure with a stable machine code."""

    def __init__(self, code: str, message: str, source_name: str | None = None):
        self.code = code
        self.message = message
        self.source_name = source_name
        prefix = f"[{code}]"
        if source_name:
            prefix += f" {source_name}"
        super().__init__(f"{prefix}: {message}")


def coerce_target(value: TargetSelection | Mapping[str, Any]) -> TargetSelection:
    return value if isinstance(value, TargetSelection) else TargetSelection.model_validate(value)


def coerce_selections(value: DesignSelections | Mapping[str, Any]) -> DesignSelections:
    return value if isinstance(value, DesignSelections) else DesignSelections.model_validate(value)


__all__ = [
    "AdapterResult",
    "IngestionError",
    "IngestionLimits",
    "IngestionResult",
    "IngestionWarning",
    "INGESTION_SCHEMA_VERSION",
    "InputFile",
    "SourcePreview",
    "coerce_selections",
    "coerce_target",
]
