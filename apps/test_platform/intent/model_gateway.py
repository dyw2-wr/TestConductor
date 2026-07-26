"""模型调用边界；未知字段必须在 schema 边界失败。"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, Type

from pydantic import ValidationError

from .contracts import ModelMessage


class DesignModelGateway(Protocol):
    def generate(self, messages: list[ModelMessage], output_schema: Type[Any]) -> Any: ...


class ExistingLLMModelGateway:
    _fenced_json = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

    def __init__(self, llm_service: Any):
        self.llm_service = llm_service

    def generate(self, messages: list[ModelMessage], output_schema: Type[Any]) -> Any:
        chat_messages = [
            {"role": message.role, "content": message.content}
            for message in messages
        ]
        try:
            response = self.llm_service.invoke(chat_messages)
        except Exception as exc:  # pragma: no cover - provider specific
            raise RuntimeError(f"模型调用失败: {exc}") from exc
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型没有返回可解析文本")
        payload = self._parse_json(content)
        try:
            return output_schema.model_validate(payload)
        except (AttributeError, ValidationError) as exc:
            raise ValueError(f"模型输出不符合严格候选 schema: {exc}") from exc

    @classmethod
    def _parse_json(cls, content: str) -> dict[str, Any]:
        fenced = cls._fenced_json.search(content)
        candidate = fenced.group(1).strip() if fenced else content.strip()
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            if start < 0:
                raise ValueError("模型输出中没有 JSON 对象")
            try:
                value, _ = json.JSONDecoder().raw_decode(candidate[start:])
            except json.JSONDecodeError as exc:
                raise ValueError(f"模型输出不是有效 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("模型输出必须是 JSON 对象")
        return value
