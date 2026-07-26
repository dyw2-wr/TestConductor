"""The small, explicit input contract used at each execution boundary.

The platform has three different input moments and they must not be mixed:

* requirement input is interpreted by the first-layer model;
* resource definitions are parsed before second-layer planning;
* non-secret runtime values are frozen while the execution plan is generated.

This module validates the frozen input boundary. Secrets are still supplied by
the process/factory, never by a stored plan or report.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeInputBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = "test-runtime-input.v1"
    variables: dict[str, Any] = Field(default_factory=dict)
    performance_mode: str | None = None

    @model_validator(mode="after")
    def validate_bundle(self) -> "RuntimeInputBundle":
        if self.schema_version != "test-runtime-input.v1":
            raise ValueError("runtime input schema_version 必须是 test-runtime-input.v1")
        for name in self.variables:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("runtime variables 的名称不能为空")
        if self.performance_mode is not None and self.performance_mode not in {"dry_run", "live"}:
            raise ValueError("performance_mode 必须是 dry_run 或 live")
        return self


def validate_runtime_input(raw: Any) -> RuntimeInputBundle:
    """Validate non-secret input stored with an execution-plan revision."""

    return RuntimeInputBundle.model_validate(raw or {})


__all__ = ["RuntimeInputBundle", "validate_runtime_input"]
