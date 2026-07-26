"""最小 HTTP API 执行器。

输入是第二层生成的 ``http-execution-plan.v4`` JSON。请求发送通过可注入 transport，
所以单元测试不需要网络；生产环境可以注入经过网关/代理配置的 ``httpx`` transport。
这里只支持显式、结构化断言，不执行模型生成的 Python 表达式或 ``eval``。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4

from .base import (
    ArtifactWorkspace,
    artifact_stage_identity,
    load_json_payload,
    prepare_artifact,
    write_evidence,
)
from .contracts import RunResult, RunStatus, RunnerError, RuntimeContext, StepResult, finish_result


class HttpTransport(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass
class _ResponseView:
    status_code: int
    headers: Mapping[str, Any]
    body: Any
    text: str


@dataclass(frozen=True)
class _PreparedRequest:
    step_id: str
    source_kind: str
    method: str
    url: str
    headers: Mapping[str, Any]
    body: Any
    query: Mapping[str, Any]
    assertions: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _PreparedHttpPlan:
    workspace: ArtifactWorkspace
    timeout_seconds: float | None
    stop_on_failure: bool
    requests: tuple[_PreparedRequest, ...]


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_JSON_PATH_RE = re.compile(r"^\$(?:\.[A-Za-z0-9_-]+|\[\d+\])*$")
_STATUS_OPERATORS = {"equals", "gte", "lte", "gt", "lt"}
_HEADER_OPERATORS = {"equals", "contains", "exists", "not_exists"}
_JSON_OPERATORS = _HEADER_OPERATORS | {"gte", "lte", "gt", "lt"}
_TEXT_OPERATORS = {"equals", "contains"}


def _resolve_path(value: Any, variables: Mapping[str, Any]) -> str:
    if not isinstance(value, str):
        raise RunnerError("REQUEST_URL_INVALID", "request path 必须是字符串")

    def replace(match: re.Match[str]) -> str:
        raw = _lookup(variables, match.group(1))
        if isinstance(raw, (Mapping, list, tuple)):
            raise RunnerError("REQUEST_URL_INVALID", "path 变量必须是标量")
        text = str(raw)
        if text in {".", ".."} or any(ord(char) < 32 for char in text):
            raise RunnerError("REQUEST_URL_INVALID", "path 变量包含不安全值")
        return quote(text, safe="")

    return _PLACEHOLDER_RE.sub(replace, value)


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
    exact = _PLACEHOLDER_RE.fullmatch(value.strip())
    if exact:
        return _lookup(variables, exact.group(1))

    def replace(match: re.Match[str]) -> str:
        resolved = _lookup(variables, match.group(1))
        if isinstance(resolved, (Mapping, list, tuple)):
            raise RunnerError("REQUEST_TEMPLATE_INVALID", f"对象变量不能嵌入字符串: {match.group(1)}")
        return str(resolved)

    return _PLACEHOLDER_RE.sub(replace, value)


def _response_view(response: Any, max_response_bytes: int) -> _ResponseView:
    if isinstance(response, Mapping):
        status = response.get("status_code", response.get("status", 0))
        headers = response.get("headers", {})
        body = response.get("json", response.get("body"))
        raw_text = response.get("text")
        text = str(raw_text if raw_text is not None else body if body is not None else "")
        if len(text.encode("utf-8", errors="ignore")) > max_response_bytes:
            raise RunnerError(
                "RESPONSE_TOO_LARGE",
                f"响应超过 {max_response_bytes} 字节",
            )
        return _ResponseView(int(status), headers, body, text)
    status = getattr(response, "status_code", getattr(response, "status", 0))
    headers = getattr(response, "headers", {}) or {}
    content_length = next(
        (
            value
            for key, value in headers.items()
            if str(key).lower() == "content-length"
        ),
        None,
    )
    try:
        if content_length is not None and int(content_length) > max_response_bytes:
            raise RunnerError(
                "RESPONSE_TOO_LARGE",
                f"响应超过 {max_response_bytes} 字节",
            )
    except (TypeError, ValueError):
        pass
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)) and len(content) > max_response_bytes:
        raise RunnerError(
            "RESPONSE_TOO_LARGE",
            f"响应超过 {max_response_bytes} 字节",
        )
    try:
        body = response.json()
    except Exception:
        body = None
    text = getattr(response, "text", None)
    if text is None:
        text = json.dumps(body, ensure_ascii=False, default=str) if body is not None else ""
    return _ResponseView(int(status), headers, body, str(text))


def _json_path(value: Any, path: str) -> tuple[bool, Any]:
    """支持 ``$.a.b`` 和数组下标的只读 JSONPath 子集。"""

    if not isinstance(path, str) or not path.startswith("$"):
        return False, None
    current = value
    tokens = [token for token in re.split(r"[.\[\]]", path[1:]) if token != ""]
    for token in tokens:
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, current


def _assertion(assertion: Any, response: _ResponseView) -> tuple[bool, str]:
    if not isinstance(assertion, Mapping):
        return False, "断言必须是对象"
    kind = str(assertion.get("kind") or "").lower()
    operator = str(assertion.get("operator") or "").lower()
    expected = assertion.get("expected")
    if kind == "status":
        actual: Any = response.status_code
    elif kind == "header":
        name = str(assertion.get("name") or "").lower()
        actual = next((v for k, v in response.headers.items() if str(k).lower() == name), None)
    elif kind == "json":
        found, actual = _json_path(response.body, str(assertion.get("path") or ""))
        if operator == "exists":
            return found, f"json path exists={found}"
        if operator == "not_exists":
            return (not found), f"json path absent={not found}"
        if not found:
            return False, f"json path not found: {assertion.get('path')}"
    elif kind in {"body_contains", "text_contains"}:
        actual = response.text
    else:
        return False, f"不支持的断言类型: {kind or '<empty>'}"

    if operator == "equals":
        passed = actual == expected
    elif operator == "contains":
        try:
            passed = expected in actual
        except TypeError:
            passed = False
    elif operator == "exists":
        passed = actual is not None
    elif operator == "not_exists":
        passed = actual is None
    elif operator == "gte":
        try:
            passed = actual >= expected
        except TypeError:
            passed = False
    elif operator == "gt":
        try:
            passed = actual > expected
        except TypeError:
            passed = False
    elif operator == "lte":
        try:
            passed = actual <= expected
        except TypeError:
            passed = False
    elif operator == "lt":
        try:
            passed = actual < expected
        except TypeError:
            passed = False
    else:
        return False, f"不支持的断言操作符: {operator}"
    return bool(passed), f"{kind} {operator}: {'passed' if passed else 'failed'}"


def _validated_assertion(
    assertion: Any,
    step_id: str,
    index: int,
    context: RuntimeContext,
) -> Mapping[str, Any]:
    """Validate one structured assertion without inspecting a live response."""

    if not isinstance(assertion, Mapping):
        raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.assertions[{index}] 必须是对象")
    expected_result_id = str(assertion.get("expected_result_id") or "").strip()
    if not expected_result_id:
        raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.assertions[{index}] 缺少 expected_result_id")

    kind = str(assertion.get("kind") or "").strip().lower()
    operator = str(assertion.get("operator") or "").strip().lower()
    if kind == "status":
        allowed_operators = _STATUS_OPERATORS
    elif kind == "header":
        name = str(assertion.get("name") or "").strip()
        if not name or "\r" in name or "\n" in name:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"{step_id}.assertions[{index}] header 断言缺少合法 name",
            )
        allowed_operators = _HEADER_OPERATORS
    elif kind == "json":
        path = assertion.get("path")
        if not isinstance(path, str) or not _JSON_PATH_RE.fullmatch(path):
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"{step_id}.assertions[{index}] 使用了不受支持的 JSON path",
            )
        allowed_operators = _JSON_OPERATORS
    elif kind in {"body_contains", "text_contains"}:
        allowed_operators = _TEXT_OPERATORS
    else:
        raise RunnerError(
            "ARTIFACT_SCHEMA_INVALID",
            f"{step_id}.assertions[{index}] 使用了不支持的 kind: {kind or '<empty>'}",
        )
    if operator not in allowed_operators:
        raise RunnerError(
            "ARTIFACT_SCHEMA_INVALID",
            f"{step_id}.assertions[{index}] 的 operator 不适用于 {kind}: {operator or '<empty>'}",
        )

    normalized = dict(assertion)
    normalized["expected_result_id"] = expected_result_id
    normalized["operator"] = operator
    if "expected" in normalized:
        normalized["expected"] = _resolve(normalized["expected"], context.variables)
    return normalized


class HttpxTransport:
    """默认 transport；只在真正执行时导入 httpx。"""

    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout
        self._client: Any = None

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._client.request(method, url, **kwargs)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class HttpRunner:
    executor_kind = "http_api"
    payload_schema = "http-execution-plan.v4"

    def __init__(self, transport: HttpTransport | Any | None = None):
        self._owns_transport = transport is None
        self.transport = transport or HttpxTransport()

    def preflight(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> None:
        """Read and validate the complete HTTP plan without causing side effects."""

        self._prepare_plan(artifact_dir, artifact_bundle, context)

    def _prepare_plan(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> _PreparedHttpPlan:
        workspace = prepare_artifact(artifact_dir, artifact_bundle, expected_executor=self.executor_kind)
        payload_path, payload = load_json_payload(workspace, "payload")
        if payload.get("schema_version") != self.payload_schema:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{payload_path.name} 不是 {self.payload_schema}")
        allowed_payload_keys = {
            "schema_version",
            "executor_kind",
            "flow_id",
            "stage_id",
            "design_id",
            "design_version",
            "plan_id",
            "plan_version",
            "base_url_ref",
            "requests",
        }
        if set(payload) != allowed_payload_keys:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "HTTP payload 字段必须与 v4 adapter 精确一致",
            )

        if "timeout_seconds" in payload or "stop_on_failure" in payload:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "HTTP v4 artifact 不允许携带运行时 timeout/stop_on_failure 参数",
            )
        base_url_ref = str(payload.get("base_url_ref") or "").strip()
        if not base_url_ref:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "HTTP 计划缺少 base_url_ref")
        base_url_value = context.base_urls.get(base_url_ref)
        if not isinstance(base_url_value, str) or not base_url_value.strip():
            raise RunnerError("RUNTIME_RESOURCE_MISSING", f"未注入 base URL: {base_url_ref}")
        base_url = _resolve(base_url_value, context.variables)
        if not isinstance(base_url, str):
            raise RunnerError("REQUEST_URL_INVALID", "注册的 base URL 必须解析为字符串")
        parsed_base = urlparse(base_url)
        if (
            parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.username
            or parsed_base.password
        ):
            raise RunnerError("REQUEST_URL_INVALID", "注册的 base URL 必须是无用户信息的 HTTP(S) 地址")

        requests = payload.get("requests")
        if not isinstance(requests, list) or not requests:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", "HTTP 计划必须包含非空 requests")
        prepared_requests = tuple(
            self._prepare_request(index, spec, base_url, context)
            for index, spec in enumerate(requests)
        )
        return _PreparedHttpPlan(
            workspace=workspace,
            timeout_seconds=None,
            stop_on_failure=True,
            requests=prepared_requests,
        )

    @staticmethod
    def _prepare_request(
        index: int,
        spec: Any,
        base_url: str,
        context: RuntimeContext,
    ) -> _PreparedRequest:
        if not isinstance(spec, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"requests[{index}] 必须是对象")
        allowed_request_keys = {
            "request_id",
            "source",
            "operation_ref",
            "method",
            "path",
            "body_ref",
            "headers_ref",
            "query",
            "assertions",
        }
        if not set(spec).issubset(allowed_request_keys):
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"requests[{index}] 包含 adapter 未声明字段",
            )
        request_id = spec.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"requests[{index}].request_id 必须是非空字符串",
            )
        step_id = request_id.strip()
        source = spec.get("source")
        if not isinstance(source, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.source 必须是对象")
        source_kind = source.get("source_kind")
        source_id = source.get("source_id")
        if source_kind not in {"operation", "expected_result", "required_state"} or not isinstance(
            source_id, str
        ) or not source_id.strip():
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.source 身份无效")
        method_value = spec.get("method")
        if not isinstance(method_value, str) or not method_value.strip():
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.method 必须显式提供")
        method = method_value.strip().upper()
        if method not in _ALLOWED_METHODS:
            raise RunnerError("REQUEST_METHOD_INVALID", f"不支持的 HTTP method: {method}")

        path = _resolve_path(spec.get("path", ""), context.variables)
        if not isinstance(path, str) or not path.strip():
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id} 缺少 path")
        parsed_path = urlparse(path)
        if (
            not path.startswith("/")
            or "://" in path
            or "\\" in path
            or parsed_path.query
            or parsed_path.fragment
            or any(part == ".." for part in path.split("/"))
        ):
            raise RunnerError("REQUEST_URL_INVALID", "request path 必须是 base URL 下的相对安全路径")
        url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        parsed_url = urlparse(url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or parsed_url.netloc != urlparse(base_url).netloc
            or parsed_url.username
            or parsed_url.password
        ):
            raise RunnerError("REQUEST_URL_INVALID", "request path 不能改变已登记 base URL 的主机")

        if any(key in spec for key in ("headers", "body", "json")):
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"{step_id} 不允许 inline headers/body/json，只能使用 runtime refs",
            )
        headers_value = {}
        headers_ref = spec.get("headers_ref")
        if headers_ref not in (None, ""):
            if not isinstance(headers_ref, str) or not headers_ref.strip():
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.headers_ref 必须是非空字符串")
            headers_value = _lookup(context.variables, headers_ref.strip())
        headers = _resolve(headers_value, context.variables)
        if headers is None:
            headers = {}
        if not isinstance(headers, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.headers/headers_ref 必须解析为对象")
        for name, value in headers.items():
            if not str(name).strip() or "\r" in str(name) or "\n" in str(name):
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id} 包含非法 header name")
            if "\r" in str(value) or "\n" in str(value):
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id} 包含非法 header value")

        body_value = None
        body_ref = spec.get("body_ref")
        if body_ref not in (None, ""):
            if not isinstance(body_ref, str) or not body_ref.strip():
                raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.body_ref 必须是非空字符串")
            body_value = _lookup(context.variables, body_ref.strip())
        body = _resolve(body_value, context.variables)

        query_value = spec.get("query", {})
        query = _resolve({} if query_value is None else query_value, context.variables)
        if not isinstance(query, Mapping):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.query 必须是对象")

        assertions = spec.get("assertions")
        if not isinstance(assertions, list):
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"{step_id}.assertions 必须是数组")
        prepared_assertions = tuple(
            _validated_assertion(assertion, step_id, assertion_index, context)
            for assertion_index, assertion in enumerate(assertions)
        )
        return _PreparedRequest(
            step_id=step_id,
            source_kind=source_kind,
            method=method,
            url=url,
            headers=dict(headers),
            body=body,
            query=dict(query),
            assertions=prepared_assertions,
        )

    def run(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> RunResult:
        run_id = f"run-{uuid4().hex}"
        flow_id, stage_id = artifact_stage_identity(artifact_bundle)
        result = RunResult.new(
            run_id=run_id,
            executor_kind=self.executor_kind,
            flow_id=flow_id,
            stage_id=stage_id,
        )
        external_action_started = [False]
        try:
            prepared = self._prepare_plan(artifact_dir, artifact_bundle, context)
            # Transport configuration happens only after every request and assertion has
            # passed the read-only preflight. ``preflight()`` itself never touches it.
            if prepared.timeout_seconds is not None and isinstance(self.transport, HttpxTransport):
                self.transport.timeout = prepared.timeout_seconds
            for index, spec in enumerate(prepared.requests):
                self._run_request(result, index, spec, context, external_action_started)
                if prepared.stop_on_failure and result.steps and result.steps[-1].status != RunStatus.PASSED:
                    break
            if result.steps:
                if any(step.status == RunStatus.ERROR for step in result.steps):
                    result.status = RunStatus.ERROR
                elif all(step.status == RunStatus.PASSED for step in result.steps):
                    result.status = RunStatus.PASSED
                else:
                    result.status = RunStatus.FAILED
        except RunnerError as exc:
            result.errors.append(f"{exc.code}: {exc.message}")
            # After the request boundary, a malformed response or quota failure is an
            # Once transport starts, failures are execution errors rather than preflight blocks.
            result.status = RunStatus.ERROR if external_action_started[0] else (
                RunStatus.BLOCKED if exc.code in {
                    "RUNTIME_RESOURCE_MISSING", "ARTIFACT_SCHEMA_INVALID", "REQUEST_TEMPLATE_INVALID",
                    "REQUEST_URL_INVALID", "REQUEST_METHOD_INVALID",
                    "ARTIFACT_DIR_MISSING", "ARTIFACT_PATH_INVALID", "MANIFEST_MISSING", "MANIFEST_INVALID",
                    "MANIFEST_IDENTITY_MISMATCH", "MANIFEST_REF_MISSING", "ARTIFACT_REFS_MISSING",
                    "ARTIFACT_MISSING", "ARTIFACT_HASH_INVALID", "ARTIFACT_HASH_MISMATCH", "EXECUTOR_MISMATCH",
                } else RunStatus.ERROR
            )
        except Exception as exc:  # pragma: no cover - transport-specific failures
            result.errors.append(f"HTTP_RUN_ERROR: {exc}")
            result.status = RunStatus.ERROR
        finally:
            result.external_action_started = external_action_started[0]
            # 注入的 transport 可能被 coordinator 复用于多个 stage，由调用方负责其
            # 生命周期；只有默认 transport 是 runner 自己创建、自己关闭的资源。
            if self._owns_transport:
                close = getattr(self.transport, "close", None)
                if callable(close):
                    close()
            finish_result(result, result.status)
        return result

    def _run_request(
        self,
        result: RunResult,
        index: int,
        spec: _PreparedRequest,
        context: RuntimeContext,
        external_action_started: list[bool],
    ) -> None:
        kwargs: dict[str, Any] = {}
        if spec.headers:
            kwargs["headers"] = spec.headers
        if spec.body is not None:
            kwargs["json"] = spec.body
        if spec.query:
            kwargs["params"] = spec.query
        started = perf_counter()
        try:
            external_action_started[0] = True
            raw_response = self.transport.request(spec.method, spec.url, **kwargs)
        except RunnerError:
            raise
        except Exception as exc:
            result.steps.append(
                StepResult(
                    step_id=spec.step_id,
                    status=RunStatus.ERROR,
                    message=f"请求失败: {exc}",
                    duration_ms=(perf_counter() - started) * 1000,
                )
            )
            return
        response = _response_view(raw_response, context.max_response_bytes)
        assertion_results: list[dict[str, Any]] = []
        passed = not (
            spec.source_kind == "required_state"
            and not 200 <= response.status_code < 300
        )
        if spec.source_kind == "required_state":
            assertion_results.append(
                {
                    "passed": 200 <= response.status_code < 300,
                    "expected_result_id": None,
                    "message": "setup HTTP status 必须为 2xx",
                }
            )
        for assertion in spec.assertions:
            expected_result_id = assertion.get("expected_result_id")
            ok, message = _assertion(assertion, response)
            assertion_results.append({"passed": ok, "expected_result_id": expected_result_id, "message": message})
            passed = passed and ok
        evidence_name = write_evidence(
            context,
            f"{result.run_id}-{index + 1}-{spec.step_id}",
            {
                "request": {"method": spec.method, "url": spec.url},
                "response": {
                    "status_code": response.status_code,
                    "content_type": next(
                        (v for k, v in response.headers.items() if str(k).lower() == "content-type"),
                        None,
                    ),
                },
                "assertions": assertion_results,
            },
        )
        if evidence_name:
            result.evidence.append(evidence_name)
        result.steps.append(
            StepResult(
                step_id=spec.step_id,
                status=RunStatus.PASSED if passed else RunStatus.FAILED,
                message="all assertions passed" if passed else "one or more assertions failed",
                duration_ms=(perf_counter() - started) * 1000,
                details={"status_code": response.status_code, "assertions": assertion_results},
                evidence=[evidence_name] if evidence_name else [],
            )
        )
