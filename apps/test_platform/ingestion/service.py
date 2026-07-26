"""Public, database-free requirement ingestion service.

The service is the single bridge for both a frontend textarea and uploaded
files.  It prepares a v4 ``TestDesignRequest`` but deliberately does not call
the LLM or approve anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from apps.test_platform.intent.contracts import RequirementInput, TestDesignRequest

from .adapters import parse_input_file, supported_extensions
from .contracts import (
    IngestionError,
    IngestionLimits,
    IngestionResult,
    IngestionWarning,
    InputFile,
    SourcePreview,
    coerce_selections,
    coerce_target,
)


class RequirementIngestor:
    """Prepare raw text/files for the existing first-layer contract."""

    def __init__(self, limits: IngestionLimits | None = None):
        self.limits = (limits or IngestionLimits()).validate()

    def prepare(
        self,
        *,
        frontend_text: str | None = None,
        files: Sequence[InputFile] | None = None,
        target: Any,
        selections: Any,
        request_id: str | None = None,
    ) -> IngestionResult:
        """Merge frontend text first, then files in caller-provided order.

        Empty textarea values are ignored.  A non-empty string is passed to the
        v4 contract byte-for-byte, including leading/trailing whitespace and
        line endings; ``strip`` is used only to decide whether it is empty.
        """

        try:
            target_value = coerce_target(target)
            selections_value = coerce_selections(selections)
        except (ValidationError, TypeError, ValueError) as exc:
            raise IngestionError(
                "REQUEST_INVALID",
                "target 或 selections 不符合第一层输入契约",
            ) from exc
        warnings: list[IngestionWarning] = []
        previews: list[SourcePreview] = []
        raw_requirements: list[tuple[str | None, str]] = []

        if frontend_text is not None:
            if not isinstance(frontend_text, str):
                raise IngestionError(
                    "FRONTEND_TEXT_INVALID",
                    "frontend_text 必须是字符串",
                    "frontend_text",
                )
            if frontend_text == "":
                warnings.append(
                    IngestionWarning(
                        "EMPTY_TEXT_IGNORED",
                        "前端文本为空，已忽略",
                        "frontend_text",
                    )
                )
            elif not frontend_text.strip():
                warnings.append(
                    IngestionWarning(
                        "EMPTY_TEXT_IGNORED",
                        "前端文本只有空白字符，已忽略",
                        "frontend_text",
                    )
                )
            else:
                if "\x00" in frontend_text:
                    raise IngestionError(
                        "NUL_BYTE_REJECTED",
                        "前端文本包含 NUL 字符",
                        "frontend_text",
                    )
                try:
                    frontend_text_bytes = frontend_text.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise IngestionError(
                        "INVALID_UNICODE",
                        "前端文本包含无效 Unicode 字符",
                        "frontend_text",
                    ) from exc
                if len(frontend_text_bytes) > self.limits.max_requirement_bytes:
                    raise IngestionError(
                        "REQUIREMENT_TOO_LARGE",
                        f"前端文本超过 {self.limits.max_requirement_bytes} 字节限制",
                        "frontend_text",
                    )
                raw_requirements.append((None, frontend_text))
                previews.append(SourcePreview("frontend_text", "frontend_text", 1))

        file_values = tuple(files or ())
        if len(file_values) > self.limits.max_files:
            raise IngestionError(
                "TOO_MANY_FILES",
                f"一次最多接收 {self.limits.max_files} 个文件；请分批提交",
            )
        total_file_bytes = 0
        for value in file_values:
            source = self._coerce_file(value)
            total_file_bytes += len(source.data)
            if total_file_bytes > self.limits.max_total_file_bytes:
                raise IngestionError(
                    "TOTAL_FILE_TOO_LARGE",
                    f"本次文件总大小超过 {self.limits.max_total_file_bytes} 字节限制",
                    source.filename,
                )
            result = parse_input_file(source, self.limits)
            if len(raw_requirements) + len(result.requirements) > self.limits.max_requirements:
                raise IngestionError(
                    "TOO_MANY_REQUIREMENTS",
                    f"本次提取出的需求超过 {self.limits.max_requirements} 条；"
                    "请在前端选择范围或分批提交",
                    source.filename,
                )
            source_warnings = tuple(result.warnings)
            warnings.extend(source_warnings)
            previews.append(
                SourcePreview(
                    source_name=source.filename,
                    source_type=result.source_type,
                    requirement_count=len(result.requirements),
                    warnings=source_warnings,
                )
            )
            raw_requirements.extend(result.requirements)

        if not raw_requirements:
            raise IngestionError(
                "NO_INPUT",
                "请提供一段非空前端文本或至少一个有效文件",
            )
        if len(raw_requirements) > self.limits.max_requirements:
            raise IngestionError(
                "TOO_MANY_REQUIREMENTS",
                f"本次提取出 {len(raw_requirements)} 条需求，最多允许 {self.limits.max_requirements} 条；"
                "请在前端选择范围或分批提交",
            )

        requirements: list[RequirementInput] = []
        total_requirement_bytes = 0
        for index, (requirement_id, content) in enumerate(raw_requirements, start=1):
            if not isinstance(content, str) or not content.strip():
                raise IngestionError(
                    "EMPTY_EXTRACTED_TEXT",
                    "提取结果包含空需求",
                    f"requirement-{index}",
                )
            if "\x00" in content:
                raise IngestionError(
                    "NUL_BYTE_REJECTED",
                    "提取结果包含 NUL 字符",
                    f"requirement-{index}",
                )
            if any(
                (ord(character) < 32 and character not in "\t\r\n")
                or ord(character) == 127
                for character in content
            ):
                raise IngestionError(
                    "CONTROL_CHARACTER_REJECTED",
                    "提取结果包含不可见控制字符",
                    f"requirement-{index}",
                )
            try:
                content_bytes = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise IngestionError(
                    "INVALID_UNICODE",
                    "提取结果包含无效 Unicode 字符",
                    f"requirement-{index}",
                ) from exc
            if len(content_bytes) > self.limits.max_requirement_bytes:
                raise IngestionError(
                    "REQUIREMENT_TOO_LARGE",
                    f"提取出的第 {index} 条需求超过 {self.limits.max_requirement_bytes} 字节限制",
                    f"requirement-{index}",
                )
            total_requirement_bytes += len(content_bytes)
            if total_requirement_bytes > self.limits.max_total_requirement_bytes:
                raise IngestionError(
                    "TOTAL_REQUIREMENT_TOO_LARGE",
                    "本次提取出的需求正文总量超过第一层 256 KiB 限制；请拆分或选择范围",
                    f"requirement-{index}",
                )
            requirements.append(
                RequirementInput(requirement_id=requirement_id, content=content)
            )

        try:
            request = TestDesignRequest(
                request_id=request_id,
                requirements=requirements,
                target=target_value,
                selections=selections_value,
            )
        except ValidationError as exc:
            raise IngestionError(
                "REQUEST_INVALID",
                "target、selections 或需求 ID 不符合第一层契约",
            ) from exc
        return IngestionResult(
            request=request,
            warnings=tuple(warnings),
            sources=tuple(previews),
        )

    @staticmethod
    def _coerce_file(value: InputFile | Mapping[str, Any]) -> InputFile:
        if isinstance(value, InputFile):
            return value
        if isinstance(value, Mapping):
            try:
                return InputFile(
                    filename=str(value["filename"]),
                    data=value["data"],
                    content_type=value.get("content_type"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IngestionError(
                    "FILE_INPUT_INVALID",
                    "文件输入必须包含 filename 和 data；不接受服务器路径",
                ) from exc
        raise IngestionError(
            "FILE_INPUT_INVALID",
            "files 只接受 InputFile 或 filename/data 对象；不接受服务器路径",
        )


def prepare_request(
    *,
    frontend_text: str | None = None,
    files: Sequence[InputFile] | None = None,
    target: Any,
    selections: Any,
    request_id: str | None = None,
    limits: IngestionLimits | None = None,
) -> IngestionResult:
    """Convenience function for a future HTTP/API entry point."""

    return RequirementIngestor(limits).prepare(
        frontend_text=frontend_text,
        files=files,
        target=target,
        selections=selections,
        request_id=request_id,
    )


__all__ = ["RequirementIngestor", "prepare_request", "supported_extensions"]
