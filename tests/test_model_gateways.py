from contextlib import nullcontext
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict

from apps.test_platform.intent.contracts import ModelMessage
from apps.test_platform.intent.model_gateway import ExistingLLMModelGateway
from apps.test_platform.service_factory import (
    OpenAICompatibleModelGateway,
    get_model_gateway,
    model_call_slot,
)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class ModelGatewayTests(unittest.TestCase):
    def test_task_specific_models_fall_back_to_the_global_model(self):
        configured = SimpleNamespace(
            configured=True,
            TEST_PLATFORM_LLM_API_KEY="key",
            TEST_PLATFORM_LLM_BASE_URL="",
            TEST_PLATFORM_LLM_MODEL="global-model",
            TEST_PLATFORM_DESIGN_LLM_MODEL="fast-design-model",
            TEST_PLATFORM_PLANNING_LLM_MODEL="",
            TEST_PLATFORM_LLM_TIMEOUT_SECONDS=10,
        )
        with patch("apps.test_platform.service_factory.settings", configured):
            design = get_model_gateway("design")
            planning = get_model_gateway("planning")

        self.assertEqual(design.model, "fast-design-model")
        self.assertEqual(planning.model, "global-model")

    def test_gateway_reports_real_model_wait_call_and_validation_phases(self):
        updates = []
        gateway = OpenAICompatibleModelGateway(
            api_key="key",
            base_url=None,
            model="model",
            timeout=1,
            progress_callback=lambda phase, message, percent: updates.append(
                (phase, message, percent)
            ),
        )
        gateway.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content='{"value": 1}')
                            )
                        ]
                    )
                )
            )
        )

        with patch(
            "apps.test_platform.service_factory.model_call_slot",
            return_value=nullcontext(),
        ):
            result = gateway.generate([], _Output)

        self.assertEqual(result.value, 1)
        self.assertEqual(
            [item[0] for item in updates],
            ["waiting_model", "calling_model", "validating_model"],
        )

    def test_model_call_gate_fails_fast_when_local_capacity_is_full(self):
        configured = SimpleNamespace(
            configured=True,
            TEST_PLATFORM_ARTIFACT_ROOT=None,
            TEST_PLATFORM_LLM_MAX_CONCURRENT_CALLS=1,
            TEST_PLATFORM_LLM_QUEUE_TIMEOUT_SECONDS=0,
            TEST_PLATFORM_LLM_TIMEOUT_SECONDS=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            configured.TEST_PLATFORM_ARTIFACT_ROOT = Path(directory)
            with patch("apps.test_platform.service_factory.settings", configured):
                with model_call_slot():
                    with self.assertRaisesRegex(RuntimeError, "模型服务繁忙"):
                        with model_call_slot():
                            self.fail("a second model call unexpectedly acquired capacity")

    def test_openai_gateway_disables_hidden_retries_and_bounds_connect_timeout(self):
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"value": 1}'))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: completion)
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            configured = SimpleNamespace(
                configured=True,
                TEST_PLATFORM_ARTIFACT_ROOT=Path(directory),
                TEST_PLATFORM_LLM_CONNECT_TIMEOUT_SECONDS=2,
                TEST_PLATFORM_LLM_MAX_RETRIES=0,
                TEST_PLATFORM_LLM_MAX_CONCURRENT_CALLS=2,
                TEST_PLATFORM_LLM_QUEUE_TIMEOUT_SECONDS=0,
                TEST_PLATFORM_LLM_TIMEOUT_SECONDS=5,
            )
            with patch(
                "apps.test_platform.service_factory.settings", configured
            ), patch(
                "apps.test_platform.service_factory.OpenAI", return_value=client
            ) as factory:
                gateway = OpenAICompatibleModelGateway(
                    api_key="key", base_url=None, model="model", timeout=5
                )
                result = gateway.generate([], _Output)

        self.assertEqual(result.value, 1)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(factory.call_args.kwargs["timeout"].connect, 2)

    def test_openai_gateway_falls_back_only_for_unsupported_schema_format(self):
        calls = []

        class UnsupportedSchema(RuntimeError):
            status_code = 400

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise UnsupportedSchema("response_format json_schema unsupported")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"value": 2}'))]
                )

        gateway = OpenAICompatibleModelGateway(
            api_key="key", base_url=None, model="model", timeout=1
        )
        gateway.client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )

        result = gateway.generate([ModelMessage(role="user", content="generate")], _Output)

        self.assertEqual(result.value, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["response_format"]["type"], "json_schema")
        self.assertEqual(calls[1]["response_format"], {"type": "json_object"})

    def test_openai_gateway_does_not_retry_unrelated_provider_errors(self):
        calls = []

        class ProviderFailure(RuntimeError):
            status_code = 500

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                raise ProviderFailure("upstream unavailable")

        gateway = OpenAICompatibleModelGateway(
            api_key="key", base_url=None, model="model", timeout=1
        )
        gateway.client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with self.assertRaisesRegex(RuntimeError, "模型调用失败"):
            gateway.generate([], _Output)
        self.assertEqual(len(calls), 1)

    def test_openai_gateway_rejects_empty_and_schema_invalid_responses(self):
        for content, message in (("", "没有返回 JSON"), ('{"other": 1}', "严格候选 schema")):
            with self.subTest(content=content):
                gateway = OpenAICompatibleModelGateway(
                    api_key="key", base_url=None, model="model", timeout=1
                )
                gateway.client = SimpleNamespace(
                    chat=SimpleNamespace(
                        completions=SimpleNamespace(
                            create=lambda **kwargs: SimpleNamespace(
                                choices=[
                                    SimpleNamespace(
                                        message=SimpleNamespace(content=content)
                                    )
                                ]
                            )
                        )
                    )
                )
                with self.assertRaisesRegex(ValueError, message):
                    gateway.generate([], _Output)

    def test_existing_gateway_accepts_fenced_or_wrapped_json_and_rejects_arrays(self):
        self.assertEqual(
            ExistingLLMModelGateway._parse_json('```json\n{"value": 3}\n```'),
            {"value": 3},
        )
        self.assertEqual(
            ExistingLLMModelGateway._parse_json('commentary before {"value": 4} after'),
            {"value": 4},
        )
        with self.assertRaisesRegex(ValueError, "必须是 JSON 对象"):
            ExistingLLMModelGateway._parse_json("[1, 2]")

    def test_existing_gateway_maps_roles_and_validates_list_content(self):
        captured = []

        class Service:
            def invoke(self, messages):
                captured.extend(messages)
                return SimpleNamespace(content=[{"text": '{"value": '}, {"text": "5}"}])

        result = ExistingLLMModelGateway(Service()).generate(
            [
                ModelMessage(role="system", content="rules"),
                ModelMessage(role="user", content="request"),
            ],
            _Output,
        )

        self.assertEqual(result.value, 5)
        self.assertEqual([item["content"] for item in captured], ["rules", "request"])
        self.assertEqual([item["role"] for item in captured], ["system", "user"])


if __name__ == "__main__":
    unittest.main()
