"""第三层 runner 的公共门禁和生命周期工具。

本模块只验证第二层交接包引用的相对文件和字节 hash，然后把 JSON payload
交给具体 runner。任何 hash、版本或路径不匹配都会在执行前返回
``blocked``，不打开目标系统、数据库或性能 driver。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .contracts import (
    CleanupResult,
    RunResult,
    RunStatus,
    RunnerError,
    RuntimeContext,
    StepResult,
    finish_result,
)


class ExecutorRunner(Protocol):
    """内部 runner 的最小接口。"""

    executor_kind: str

    def preflight(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> None: ...

    def run(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> RunResult: ...


@dataclass(frozen=True)
class ArtifactWorkspace:
    root: Path
    manifest_path: Path
    manifest: Mapping[str, Any]
    files: Mapping[str, Path]

    def file_for(self, *kinds: str) -> Path:
        for kind in kinds:
            path = self.files.get(kind)
            if path is not None:
                return path
        raise RunnerError("ARTIFACT_PAYLOAD_MISSING", f"缺少 payload 文件，期望类型: {', '.join(kinds)}")


@dataclass(frozen=True)
class PreparedFlowCleanup:
    action_ref: str
    cleanup_goal_id: str
    handler_kind: str
    evidence_required: bool
    parameters: Mapping[str, Any]
    data_bindings: tuple[tuple[str, str, str, str], ...]
    hook: Any


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|secret|token|api[_ -]?key|authorization|cookie|credential|口令|密码)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_ -]?key|authorization|bearer)\s*[:=]\s*[^,;\s]+"
)


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def artifact_stage_identity(artifact_bundle: Any) -> tuple[str, str]:
    """Return the mandatory v4 flow/stage identity."""

    flow_id = str(_value(artifact_bundle, "flow_id", "") or "").strip()
    stage_id = str(_value(artifact_bundle, "stage_id", "") or "").strip()
    if not flow_id or not stage_id:
        raise RunnerError(
            "ARTIFACT_IDENTITY_MISMATCH",
            "v4 artifact 必须包含 flow_id/stage_id",
        )
    return flow_id, stage_id


def _hash_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise RunnerError("ARTIFACT_PATH_INVALID", "产物引用路径不能为空")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise RunnerError("ARTIFACT_PATH_INVALID", f"产物引用必须是根目录内相对路径: {relative}")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise RunnerError("ARTIFACT_PATH_INVALID", f"产物路径越界: {relative}") from exc
    return resolved


def _require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RunnerError("ARTIFACT_HASH_INVALID", f"{field} 不是 sha256:<64位十六进制>")
    return value


def prepare_artifact(
    artifact_dir: str | Path,
    artifact_bundle: Any,
    *,
    expected_executor: str | None = None,
) -> ArtifactWorkspace:
    """校验交接包和 sidecar manifest，并返回受限的文件工作区。"""

    root = Path(artifact_dir).resolve()
    if not root.is_dir():
        raise RunnerError("ARTIFACT_DIR_MISSING", f"产物目录不存在: {root}")

    raw_bundle_kind = _value(artifact_bundle, "executor_kind", "")
    bundle_kind = str(getattr(raw_bundle_kind, "value", raw_bundle_kind))
    if expected_executor and bundle_kind != expected_executor:
        raise RunnerError(
            "EXECUTOR_MISMATCH",
            f"交接包执行器 {bundle_kind!r} 与 runner {expected_executor!r} 不一致",
        )
    manifest_ref = _value(artifact_bundle, "manifest_path_ref")
    manifest_path = _safe_file(root, manifest_ref)
    if not manifest_path.is_file():
        raise RunnerError("MANIFEST_MISSING", f"manifest 不存在: {manifest_ref}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("MANIFEST_INVALID", f"无法读取 manifest: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise RunnerError("MANIFEST_INVALID", "manifest 顶层必须是对象")

    # 这些字段是第三层执行前必须绑定的 v4 stage 审计身份。
    identity_fields = [
        "artifact_id",
        "plan_id",
        "plan_version",
        "design_id",
        "design_version",
        "design_content_hash",
        "design_input_content_hash",
        "catalog_id",
        "catalog_content_hash",
        "plan_content_hash",
        "executor_kind",
        "flow_id",
        "stage_id",
    ]
    for field in identity_fields:
        expected = _value(artifact_bundle, field)
        actual = manifest.get(field)
        if expected != actual:
            raise RunnerError("MANIFEST_IDENTITY_MISMATCH", f"manifest.{field} 与交接包不一致")
    if expected_executor and manifest.get("executor_kind") != expected_executor:
        raise RunnerError("EXECUTOR_MISMATCH", "manifest.executor_kind 与 runner 不一致")
    expected_schema = _value(artifact_bundle, "artifact_schema_version")
    if expected_schema and manifest.get("artifact_schema_version") != expected_schema:
        raise RunnerError("MANIFEST_IDENTITY_MISMATCH", "manifest.artifact_schema_version 与交接包不一致")

    refs = _value(artifact_bundle, "artifact_refs", [])
    if not refs:
        raise RunnerError("ARTIFACT_REFS_MISSING", "交接包没有 artifact_refs")
    files: dict[str, Path] = {}
    for ref in refs:
        kind = str(_value(ref, "kind", "")).strip()
        path_ref = _value(ref, "path_ref")
        expected_hash = _require_hash(_value(ref, "sha256"), f"artifact_refs[{kind}].sha256")
        path = _safe_file(root, path_ref)
        if not path.is_file():
            raise RunnerError("ARTIFACT_MISSING", f"产物文件不存在: {path_ref}")
        actual_hash = _hash_file(path)
        if actual_hash != expected_hash:
            raise RunnerError("ARTIFACT_HASH_MISMATCH", f"产物 hash 不匹配: {path_ref}")
        files.setdefault(kind, path)
        manifest_hashes = manifest.get("compiled_artifact_hashes")
        if isinstance(manifest_hashes, Mapping):
            declared_hash = manifest_hashes.get(path_ref)
            if declared_hash is not None and declared_hash != actual_hash:
                raise RunnerError("ARTIFACT_HASH_MISMATCH", f"manifest 中的 hash 不匹配: {path_ref}")

    manifest_hash_ref = next(
        (
            ref
            for ref in refs
            if str(_value(ref, "kind", "")) == "manifest"
            and _value(ref, "path_ref") == manifest_ref
        ),
        None,
    )
    if manifest_hash_ref is None:
        raise RunnerError("MANIFEST_REF_MISSING", "artifact_refs 必须包含 manifest_path_ref")
    return ArtifactWorkspace(root=root, manifest_path=manifest_path, manifest=manifest, files=files)


def load_json_payload(workspace: ArtifactWorkspace, *kinds: str) -> tuple[Path, Mapping[str, Any]]:
    path = workspace.file_for(*kinds)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("ARTIFACT_PAYLOAD_INVALID", f"无法读取 {path.name}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise RunnerError("ARTIFACT_PAYLOAD_INVALID", f"{path.name} 顶层必须是对象")
    # The sidecar hash proves bytes were not replaced, but it does not prove that
    # the JSON's own identity points at this stage. Keep that second binding at the
    # common read boundary so every typed runner gets the same check.
    identity_fields = (
        "schema_version",
        "executor_kind",
        "flow_id",
        "stage_id",
        "design_id",
        "design_version",
        "plan_id",
        "plan_version",
    )
    expected_schema = workspace.manifest.get("artifact_schema_version")
    for field in identity_fields:
        expected = expected_schema if field == "schema_version" else workspace.manifest.get(field)
        if field not in payload or payload.get(field) != expected:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                f"payload.{field} 与 manifest/交接身份不一致",
            )
    return path, payload


def _runtime_variable(variables: Mapping[str, Any], variable_ref: str) -> Any:
    """Resolve a reviewed variable_ref, preferring an exact runtime key."""

    if variable_ref in variables:
        return variables[variable_ref]
    current: Any = variables
    for part in variable_ref.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise RunnerError(
                "RUNTIME_RESOURCE_MISSING",
                f"未注入 cleanup variable_ref: {variable_ref}",
            )
        current = current[part]
    return current


def prepare_flow_cleanup(cleanup: Any, context: RuntimeContext) -> PreparedFlowCleanup | None:
    """Resolve a v4 flow cleanup to one hook and explicit runtime parameters."""

    if cleanup is None:
        return None
    action_ref = str(_value(cleanup, "action_ref", "") or "").strip()
    cleanup_goal_id = str(_value(cleanup, "cleanup_goal_id", "") or "").strip()
    handler_kind = str(_value(cleanup, "handler_kind", "") or "").strip()
    if not action_ref or not cleanup_goal_id or not handler_kind:
        raise RunnerError(
            "ARTIFACT_SCHEMA_INVALID",
            "flow cleanup 必须包含 action_ref/cleanup_goal_id/handler_kind",
        )
    if _value(cleanup, "always_run") is not True:
        raise RunnerError("ARTIFACT_SCHEMA_INVALID", "flow cleanup 必须 always_run=true")
    hook = context.cleanup_hooks.get(action_ref)
    if not callable(hook):
        raise RunnerError("CLEANUP_UNAVAILABLE", f"未注入可调用的 cleanup hook: {action_ref}")
    evidence_required = _value(cleanup, "evidence_required") is True
    if evidence_required and context.evidence_dir is None:
        raise RunnerError(
            "CLEANUP_EVIDENCE_UNAVAILABLE",
            f"cleanup {action_ref} 要求证据，但 RuntimeContext.evidence_dir 未配置",
        )

    parameters: dict[str, Any] = {}
    bindings: list[tuple[str, str, str, str]] = []
    raw_bindings = list(_value(cleanup, "data_bindings", []) or [])
    if not raw_bindings:
        raise RunnerError(
            "ARTIFACT_SCHEMA_INVALID",
            "flow cleanup.data_bindings 不能为空；禁止零参数 cleanup hook",
        )
    for index, binding in enumerate(raw_bindings):
        slot = str(_value(binding, "slot", "") or "").strip()
        data_id = str(_value(binding, "data_id", "") or "").strip()
        binding_ref = str(_value(binding, "binding_ref", "") or "").strip()
        variable_ref = str(_value(binding, "variable_ref", "") or "").strip()
        if not slot or not data_id or not binding_ref or not variable_ref:
            raise RunnerError(
                "ARTIFACT_SCHEMA_INVALID",
                "flow cleanup.data_bindings"
                f"[{index}] 缺少 slot/data_id/binding_ref/variable_ref",
            )
        if slot in parameters:
            raise RunnerError("ARTIFACT_SCHEMA_INVALID", f"flow cleanup slot 重复: {slot}")
        parameters[slot] = _runtime_variable(context.variables, variable_ref)
        bindings.append((slot, data_id, binding_ref, variable_ref))
    return PreparedFlowCleanup(
        action_ref=action_ref,
        cleanup_goal_id=cleanup_goal_id,
        handler_kind=handler_kind,
        evidence_required=evidence_required,
        parameters=parameters,
        data_bindings=tuple(bindings),
        hook=hook,
    )


def run_prepared_flow_cleanup(
    prepared: PreparedFlowCleanup,
    context: RuntimeContext,
    *,
    run_id: str,
) -> tuple[StepResult, list[str], list[str]]:
    """Run one prepared flow cleanup exactly once and return its audit outcome."""

    started = perf_counter()
    evidence: list[str] = []
    errors: list[str] = []
    try:
        cleanup_result = prepared.hook(**dict(prepared.parameters))
    except Exception as exc:  # pragma: no cover - hook-specific failures
        cleanup_result = None
        errors.append(f"CLEANUP_FAILED: {prepared.action_ref}: {exc}")
    succeeded = isinstance(cleanup_result, CleanupResult) and cleanup_result.success is True
    serialized = (
        {"success": cleanup_result.success, "details": cleanup_result.details}
        if isinstance(cleanup_result, CleanupResult)
        else {"success": False, "error": "hook 必须返回 CleanupResult"}
    )
    if prepared.evidence_required:
        evidence_name = write_evidence(
            context,
            f"{run_id}-flow-cleanup-{prepared.action_ref}",
            {
                "action_ref": prepared.action_ref,
                "cleanup_goal_id": prepared.cleanup_goal_id,
                "handler_kind": prepared.handler_kind,
                "data_bindings": [
                    {
                        "slot": slot,
                        "data_id": data_id,
                        "binding_ref": binding_ref,
                        "variable_ref": variable_ref,
                    }
                    for slot, data_id, binding_ref, variable_ref in prepared.data_bindings
                ],
                "result": serialized,
            },
        )
        if evidence_name:
            evidence.append(evidence_name)
        else:
            succeeded = False
    if not succeeded and not errors:
        errors.append(
            f"CLEANUP_FAILED: {prepared.action_ref} 必须返回 success=true 的 CleanupResult"
        )
    return (
        StepResult(
            step_id=f"cleanup:{prepared.action_ref}",
            status=RunStatus.PASSED if succeeded else RunStatus.FAILED,
            message="flow cleanup executed" if succeeded else "flow cleanup failed",
            duration_ms=(perf_counter() - started) * 1000,
            details={
                "cleanup_goal_id": prepared.cleanup_goal_id,
                "handler_kind": prepared.handler_kind,
                "parameter_slots": [
                    slot for slot, _, _, _ in prepared.data_bindings
                ],
            },
            evidence=list(evidence),
        ),
        evidence,
        errors,
    )


def redact(
    value: Any,
    *,
    secret_names: set[str] | None = None,
    secret_values: set[str] | None = None,
) -> Any:
    """递归脱敏 evidence，避免运行时变量落盘。"""

    secret_names = secret_names or set()
    secret_values = {item for item in (secret_values or set()) if item}

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if _SENSITIVE_KEY_RE.search(str(key)) or str(key) in secret_names
            else redact(item, secret_names=secret_names, secret_values=secret_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secret_names=secret_names, secret_values=secret_values) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in sorted(secret_values, key=len, reverse=True):
            redacted = redacted.replace(secret, "[REDACTED]")
        return _SENSITIVE_TEXT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return value


def write_evidence(context: RuntimeContext, name: str, payload: Any) -> str | None:
    """将脱敏 JSON evidence 写到受控目录，返回相对文件名。"""

    if context.evidence_dir is None:
        return None
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "evidence"
    evidence_dir = context.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = (evidence_dir / f"{safe_name}.json").resolve()
    try:
        path.relative_to(evidence_dir)
    except ValueError as exc:
        raise RunnerError("EVIDENCE_PATH_INVALID", "evidence 文件路径越界") from exc
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            redact(
                payload,
                secret_names=set(context.secret_variable_names),
                secret_values={
                    str(context.variables[name])
                    for name in context.secret_variable_names
                    if name in context.variables and isinstance(context.variables[name], (str, int, float))
                },
            ),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path.name


def result_for_error(
    *,
    executor_kind: str,
    artifact_bundle: Any,
    error: RunnerError,
    run_id: str | None = None,
) -> RunResult:
    flow_id, stage_id = artifact_stage_identity(artifact_bundle)
    result = RunResult.new(
        run_id=run_id or f"run-{uuid4().hex}",
        executor_kind=executor_kind,
        flow_id=flow_id,
        stage_id=stage_id,
    )
    result.errors.append(f"{error.code}: {error.message}")
    return finish_result(result, RunStatus.BLOCKED if error.code in _BLOCKING_CODES else RunStatus.ERROR)


_BLOCKING_CODES = {
    "ARTIFACT_DIR_MISSING",
    "ARTIFACT_PATH_INVALID",
    "MANIFEST_MISSING",
    "MANIFEST_INVALID",
    "MANIFEST_IDENTITY_MISMATCH",
    "MANIFEST_REF_MISSING",
    "ARTIFACT_REFS_MISSING",
    "ARTIFACT_MISSING",
    "ARTIFACT_HASH_INVALID",
    "ARTIFACT_HASH_MISMATCH",
    "ARTIFACT_IDENTITY_MISMATCH",
    "MANIFEST_IDENTITY_MISMATCH",
    "ARTIFACT_PAYLOAD_MISSING",
    "ARTIFACT_PAYLOAD_INVALID",
    "EXECUTOR_MISMATCH",
    "EXECUTOR_DEFERRED",
    "RUNTIME_RESOURCE_MISSING",
    "QUERY_NOT_READ_ONLY",
    "PERFORMANCE_DRIVER_UNAVAILABLE",
}


class DeferredRunner:
    """保留或尚未接入执行器的明确阻断结果。"""

    def __init__(self, executor_kind: str):
        self.executor_kind = executor_kind

    def preflight(self, artifact_dir: Path, artifact_bundle: Any, context: RuntimeContext) -> None:
        raise RunnerError("EXECUTOR_DEFERRED", f"执行器暂未接入: {self.executor_kind}")

    def run(self, artifact_dir: Path, artifact_bundle: Any, context: RuntimeContext) -> RunResult:
        return result_for_error(
            executor_kind=self.executor_kind,
            artifact_bundle=artifact_bundle,
            error=RunnerError("EXECUTOR_DEFERRED", f"执行器暂未接入: {self.executor_kind}"),
        )
