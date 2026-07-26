"""HTTP typed plan 到 JSON artifact 的无推断投影。"""

from __future__ import annotations

from pprint import pformat
import re

from apps.test_platform.intent.contracts import ApprovedTestDesignBundle

from ..catalogs import PlanningCatalogSnapshot
from ..contracts import HttpExecution, PlanFlow, PlanStage, TestPlanDraft
from .json_common import write_json_bundle


def _pytest_source(payload: dict) -> str:
    """Render a reviewable, runnable pytest projection of the HTTP plan."""

    cases = pformat(payload["requests"], width=100, sort_dicts=False)
    return f'''"""Generated from the approved TestConductor HTTP execution plan.

Runtime values stay outside the artifact:
- TEST_API_BASE_URL: target base URL
- TEST_API_RUNTIME_JSON: JSON object keyed by the runtime refs shown in CASES
"""

import json
import os
import re

import httpx
import pytest


CASES = {cases}
_REF = re.compile(r"\\{{([^{{}}]+)\\}}")


def _runtime():
    return json.loads(os.environ.get("TEST_API_RUNTIME_JSON", "{{}}"))


def _resolve_text(value, runtime):
    if not isinstance(value, str):
        return value
    match = _REF.fullmatch(value)
    if match:
        return runtime[match.group(1)]
    return _REF.sub(lambda item: str(runtime[item.group(1)]), value)


def _json_path(value, path):
    current = value
    for token in [item for item in re.split(r"[.\\[\\]]", path[1:]) if item]:
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def _compare(actual, operator, expected):
    if operator == "equals":
        return actual == expected
    if operator == "contains":
        return expected in actual
    if operator == "exists":
        return actual is not None
    if operator == "not_exists":
        return actual is None
    if operator == "gte":
        return actual >= expected
    if operator == "gt":
        return actual > expected
    if operator == "lte":
        return actual <= expected
    if operator == "lt":
        return actual < expected
    raise AssertionError(f"Unsupported assertion operator: {{operator}}")


def _assert_response(response, assertion):
    kind = assertion["kind"]
    operator = assertion["operator"]
    expected = assertion.get("expected")
    if kind == "status":
        actual = response.status_code
    elif kind == "header":
        actual = response.headers.get(assertion["name"])
    elif kind == "json":
        try:
            actual = _json_path(response.json(), assertion["path"])
        except (KeyError, IndexError, TypeError):
            actual = None
    elif kind in {{"body_contains", "text_contains"}}:
        actual = response.text
    else:
        raise AssertionError(f"Unsupported assertion kind: {{kind}}")
    assert _compare(actual, operator, expected), (
        f"{{assertion['expected_result_id']}}: {{actual!r}} {{operator}} {{expected!r}}"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["request_id"])
def test_api_case(case):
    runtime = _runtime()
    base_url = os.environ["TEST_API_BASE_URL"].rstrip("/")
    path = _resolve_text(case["path"], runtime)
    query = {{name: _resolve_text(value, runtime) for name, value in case.get("query", {{}}).items()}}
    headers = runtime.get(case.get("headers_ref")) if case.get("headers_ref") else None
    body = runtime.get(case.get("body_ref")) if case.get("body_ref") else None
    response = httpx.request(
        case["method"],
        base_url + path,
        params=query,
        headers=headers,
        json=body,
        timeout=float(os.environ.get("TEST_API_TIMEOUT", "30")),
        follow_redirects=False,
    )
    for assertion in case["assertions"]:
        _assert_response(response, assertion)
'''


class HttpApiCompiler:
    artifact_schema_version = "http-execution-plan.v4"

    def compile(
        self,
        bundle: ApprovedTestDesignBundle,
        plan: TestPlanDraft,
        flow: PlanFlow,
        stage: PlanStage,
        catalog: PlanningCatalogSnapshot,
        output_root,
    ):
        execution = stage.execution
        if not isinstance(execution, HttpExecution):
            raise ValueError("HttpApiCompiler 只接受 HttpExecution")
        requests = []
        for step in execution.requests:
            path = step.path
            unresolved_path_parameters = set(
                re.findall(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}", step.path)
            )
            body_ref = None
            headers_ref = None
            query: dict[str, str] = {}
            used_slots: set[str] = set()
            for binding in step.data_bindings:
                for slot, variable_ref in binding.input_refs.items():
                    if slot in used_slots:
                        raise ValueError(f"HTTP input slot 重复绑定: {slot}")
                    used_slots.add(slot)
                    if slot in {"body", "body_ref"}:
                        body_ref = variable_ref
                    elif slot in {"headers", "headers_ref"}:
                        headers_ref = variable_ref
                    elif slot.startswith("query."):
                        name = slot.removeprefix("query.")
                        if not name:
                            raise ValueError("HTTP query input slot 缺少参数名")
                        query[name] = "{" + variable_ref + "}"
                    elif slot.startswith("path."):
                        name = slot.removeprefix("path.")
                        placeholder = "{" + name + "}"
                        if name not in unresolved_path_parameters:
                            raise ValueError(f"HTTP path 不包含登记参数: {name}")
                        path = path.replace(placeholder, "{" + variable_ref + "}")
                        unresolved_path_parameters.discard(name)
                    else:
                        raise ValueError(f"HTTP 不支持的 catalog input slot: {slot}")
            if unresolved_path_parameters:
                raise ValueError("HTTP path 仍有未绑定的 catalog 参数")
            assertions = [
                {
                    "assertion_id": f"ASSERT-{index:04d}",
                    "expected_result_id": assertion.expected_result_id,
                    "after_operation_id": assertion.after_operation_id,
                    "kind": assertion.kind,
                    "path": assertion.path,
                    "name": assertion.name,
                    "operator": assertion.operator,
                    "expected": assertion.expected,
                    "unit": assertion.unit,
                }
                for index, assertion in enumerate(step.assertions, start=1)
            ]
            requests.append(
                {
                    "request_id": step.request_id,
                    "source": step.source.model_dump(mode="json"),
                    "operation_ref": step.operation_ref,
                    "method": step.method,
                    "path": path,
                    "body_ref": body_ref,
                    "headers_ref": headers_ref,
                    "query": query,
                    "assertions": assertions,
                }
            )
        payload = {
            "schema_version": self.artifact_schema_version,
            "executor_kind": stage.executor_kind.value,
            "flow_id": flow.flow_id,
            "stage_id": stage.stage_id,
            "design_id": plan.design_id,
            "design_version": plan.design_version,
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "base_url_ref": execution.base_url_ref,
            "requests": requests,
        }
        return write_json_bundle(
            bundle,
            plan,
            flow,
            stage,
            catalog,
            output_root,
            artifact_schema_version=self.artifact_schema_version,
            payload=payload,
            supporting_files={
                "pytest_source": ("test_api_generated.py", _pytest_source(payload))
            },
        )


__all__ = ["HttpApiCompiler"]
