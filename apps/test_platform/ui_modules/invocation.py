"""Small, deterministic validation for calls to published UI modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .catalog import UiModuleDefinition


@dataclass(frozen=True)
class UiModuleInvocation:
    procedure_id: str
    version: int
    parameters: Mapping[str, str]

    @property
    def ref(self) -> str:
        return f"{self.procedure_id}@v{self.version}"


def validate_invocation(module: UiModuleDefinition, invocation: UiModuleInvocation) -> None:
    if invocation.procedure_id != module.procedure_id or invocation.version != module.version:
        raise ValueError("UI 模块调用引用与已发布模块不一致")
    expected = {str(item["name"]) for item in module.input_parameters}
    supplied = {str(key) for key in invocation.parameters}
    missing = sorted(expected - supplied)
    unknown = sorted(supplied - {str(item["name"]) for item in module.parameters})
    if missing:
        raise ValueError("UI 模块缺少参数: " + ", ".join(missing))
    if unknown:
        raise ValueError("UI 模块包含未知参数: " + ", ".join(unknown))


__all__ = ["UiModuleInvocation", "validate_invocation"]
