"""Short database transactions for one execution batch's audit record."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Protocol
from uuid import uuid4

from django.conf import settings
from django.utils import timezone


_SAFE_RUN_ID = re.compile(r"^[A-Z0-9](?:[A-Z0-9_.-]{0,126}[A-Z0-9-])?$")
_SAFE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_REPORT_KEYS = frozenset({"root", "json", "html", "junit"})
_EXECUTOR_CATEGORIES = {
    "procedure_playwright": "ui",
    "http_api": "api",
    "database": "database",
    "performance": "performance",
    "tcp_port": "port",
}
_TERMINAL_STATUSES = (
    "passed",
    "failed",
    "blocked",
    "error",
    "inconclusive",
    "dry_run",
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class RunHistoryError(RuntimeError):
    pass


class RunIdConflict(RunHistoryError):
    pass


class RunHistoryRecorder(Protocol):
    def begin(
        self,
        *,
        run_id: str,
        started_at: str,
        artifact_root: Path,
    ) -> None: ...

    def finalize(
        self,
        *,
        summary: Any,
        plan: Any | None,
        artifact_root: Path,
        context: Any,
    ) -> None: ...

    def mark_failed(self, *, run_id: str, error_code: str) -> None: ...


def generate_run_id() -> str:
    """Return a readable, date-grouped identifier with a collision-safe suffix."""

    # The runner layer is intentionally usable as a standalone library.  In
    # that mode Django settings have not been initialized, so asking
    # ``django.utils.timezone`` for the current local date raises
    # ImproperlyConfigured before any test stage can run.
    local_date = (
        timezone.localdate()
        if settings.configured
        else datetime.now().astimezone().date()
    )
    return f"RUN-{local_date:%Y%m%d}-{uuid4().hex[:12].upper()}"


def validate_run_id(run_id: str) -> str:
    value = str(run_id or "")
    windows_base = value.split(".", 1)[0]
    if (
        not _SAFE_RUN_ID.fullmatch(value)
        or windows_base in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(
            "run_id 必须为 1-128 位规范大写 ASCII 批次号，不能使用尾部点/下划线或 Windows 保留名"
        )
    return value


def _status(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _status_counts(values: list[Any]) -> dict[str, int]:
    counts = {status: 0 for status in _TERMINAL_STATUSES}
    for value in values:
        status = _status(_field(value, "status"))
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(values)
    return counts


def _fallback_result_counts(summary: Any) -> dict[str, Any]:
    flows = list(_field(summary, "flows", []) or [])
    stages = list(_field(summary, "stages", []) or [])
    return {
        "flows": _status_counts(flows),
        "stages": _status_counts(stages),
    }


def _category_result_counts(
    report: dict[str, Any], summary: Any | None = None
) -> dict[str, dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    flows = report.get("flows") if isinstance(report.get("flows"), list) else []
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        stages = flow.get("stages") if isinstance(flow.get("stages"), list) else []
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            executor = str(stage.get("executor_kind") or "")
            category = _EXECUTOR_CATEGORIES.get(executor)
            if category:
                grouped.setdefault(category, []).append(stage)
    if not grouped and summary is not None:
        for stage in list(_field(summary, "stages", []) or []):
            executor = _status(_field(stage, "executor_kind"))
            category = _EXECUTOR_CATEGORIES.get(executor)
            if category:
                grouped.setdefault(category, []).append(stage)
    return {category: _status_counts(stages) for category, stages in grouped.items()}


def _error_codes(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        candidate = str(value).split(":", 1)[0].strip()
        code = candidate if _SAFE_ERROR_CODE.fullmatch(candidate) else "RUN_ERROR"
        if code not in result:
            result.append(code)
    return result


def _parse_time(value: Any) -> datetime:
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        raise RunHistoryError(f"无效运行时间: {value!r}")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _safe_ref(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text or "://" in text or re.match(r"^[A-Za-z]:", text):
        raise RunHistoryError("报告路径必须是 storage root 下的相对路径")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RunHistoryError("报告路径包含越界片段")
    return path.as_posix()


def _storage_root_ref(artifact_root: Path) -> str:
    from django.conf import settings

    base = Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
    root = Path(artifact_root).resolve()
    try:
        relative = root.relative_to(base)
    except ValueError as exc:
        raise RunHistoryError(
            "artifact_root 必须位于 TEST_PLATFORM_ARTIFACT_ROOT 下"
        ) from exc
    return relative.as_posix() or "."


def _safe_report_paths(artifact_root: Path, values: Any) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    root = artifact_root.resolve()
    result: dict[str, str] = {}
    for key, value in values.items():
        if key not in _REPORT_KEYS:
            continue
        ref = _safe_ref(value)
        resolved = (root / Path(ref)).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RunHistoryError("报告路径越过 artifact_root") from exc
        result[key] = ref
    return result


def _read_report(artifact_root: Path, report_paths: dict[str, str]) -> dict[str, Any]:
    json_ref = report_paths.get("json")
    if not json_ref:
        return {}
    path = (artifact_root / Path(json_ref)).resolve()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunHistoryError(f"无法读取已生成的 report.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunHistoryError("report.json 顶层必须是对象")
    try:
        from .reporting import TestReportGenerator

        TestReportGenerator._validate_payload(payload)
    except ValueError as exc:
        raise RunHistoryError(f"report.json 完整性校验失败: {exc}") from exc
    return payload


def _manifest_ref(artifact_root: Path, context: Any, value: Any) -> str:
    if not value:
        return ""
    evidence_dir = _field(context, "evidence_dir")
    if evidence_dir is None:
        return ""
    root = artifact_root.resolve()
    path = (Path(evidence_dir).resolve() / Path(str(value))).resolve()
    if not path.is_file():
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


class DjangoRunHistoryRecorder:
    """Persist only redacted report metadata; detailed evidence stays on disk."""

    def begin(self, *, run_id: str, started_at: str, artifact_root: Path) -> None:
        from django.db import IntegrityError, transaction

        from .models import TestExecutionRun

        try:
            with transaction.atomic():
                safe_run_id = validate_run_id(run_id)
                existing = (
                    TestExecutionRun.objects.select_for_update()
                    .filter(run_id=safe_run_id)
                    .first()
                )
                if existing is None:
                    TestExecutionRun.objects.create(
                        run_id=safe_run_id,
                        status=TestExecutionRun.Status.RUNNING,
                        report_status=TestExecutionRun.ReportStatus.PENDING,
                        started_at=_parse_time(started_at),
                        storage_root_ref=_storage_root_ref(artifact_root),
                    )
                    return
                if existing.status != TestExecutionRun.Status.QUEUED:
                    raise RunIdConflict(f"run_id 已存在，拒绝重复执行: {run_id}")
                expected_ref = _storage_root_ref(artifact_root)
                if existing.storage_root_ref != expected_ref:
                    raise RunHistoryError("排队记录的产物目录与实际执行目录不一致")
                existing.status = TestExecutionRun.Status.RUNNING
                existing.started_at = _parse_time(started_at)
                existing.save(update_fields=("status", "started_at", "updated_at"))
        except IntegrityError as exc:
            raise RunIdConflict(f"run_id 已存在，拒绝重复执行: {run_id}") from exc

    def mark_failed(self, *, run_id: str, error_code: str) -> None:
        """Best-effort terminal state when report/history finalization itself fails."""

        from django.utils import timezone

        from .models import TestExecutionRun

        code = str(error_code or "RUN_HISTORY_FINALIZE_FAILED").strip().upper()
        if not _SAFE_ERROR_CODE.fullmatch(code):
            code = "RUN_HISTORY_FINALIZE_FAILED"
        TestExecutionRun.objects.filter(
            run_id=validate_run_id(run_id),
            status__in=(
                TestExecutionRun.Status.QUEUED,
                TestExecutionRun.Status.RUNNING,
            ),
        ).update(
            status=TestExecutionRun.Status.ERROR,
            report_status=TestExecutionRun.ReportStatus.FAILED,
            finished_at=timezone.now(),
            errors=[code],
        )

    def finalize(
        self,
        *,
        summary: Any,
        plan: Any | None,
        artifact_root: Path,
        context: Any,
    ) -> None:
        from django.db import transaction

        from .models import TestExecutionRun

        run_id = validate_run_id(str(_field(summary, "run_id", "")))
        report_paths = _safe_report_paths(
            artifact_root,
            dict(_field(summary, "report_paths", {}) or {}),
        )
        report = _read_report(artifact_root, report_paths)
        report_hash = str(report.get("report_content_hash") or "")
        if report_hash and not _SAFE_HASH.fullmatch(report_hash):
            raise RunHistoryError("report_content_hash 格式无效")
        report_files_available = all(
            key in report_paths and (artifact_root / report_paths[key]).is_file()
            for key in ("json", "html", "junit")
        )
        report_status = (
            TestExecutionRun.ReportStatus.AVAILABLE
            if report_files_available
            else TestExecutionRun.ReportStatus.FAILED
        )
        report_errors = (
            report.get("errors") if isinstance(report.get("errors"), list) else None
        )
        errors = _error_codes(
            report_errors
            if report_errors is not None
            else list(_field(summary, "errors", []) or [])
        )
        result_summary = {
            "counts": (
                report.get("summary")
                if isinstance(report.get("summary"), dict)
                else _fallback_result_counts(summary)
            ),
            "limitations": (
                [str(item) for item in report.get("limitations", [])]
                if isinstance(report.get("limitations"), list)
                else []
            ),
            "categories": _category_result_counts(report, summary),
        }
        identity = report.get("identity") if isinstance(report.get("identity"), dict) else {}
        started_at = _parse_time(
            _field(summary, "started_at") or report.get("started_at")
        )
        finished_at = _parse_time(
            _field(summary, "finished_at") or report.get("finished_at")
        )
        duration_ms = report.get("duration_ms")
        status = _status(_field(summary, "status"))
        if (
            status not in TestExecutionRun.Status.values
            or status == TestExecutionRun.Status.RUNNING
        ):
            raise RunHistoryError(f"无效终态: {status}")

        with transaction.atomic():
            record = TestExecutionRun.objects.select_for_update().get(run_id=run_id)
            if record.status != TestExecutionRun.Status.RUNNING:
                raise RunHistoryError(f"运行记录已经结束，拒绝重复 finalize: {run_id}")
            record.status = status
            record.report_status = report_status
            record.design_id = str(
                _field(plan, "design_id", "") or identity.get("design_id") or ""
            )
            record.design_version = _field(plan, "design_version") or identity.get(
                "design_version"
            )
            record.plan_id = str(
                _field(plan, "plan_id", "") or identity.get("plan_id") or ""
            )
            record.plan_version = _field(plan, "version") or identity.get("plan_version")
            plan_hash = identity.get("plan_content_hash")
            if not plan_hash and callable(getattr(plan, "content_hash", None)):
                plan_hash = plan.content_hash()
            record.plan_content_hash = str(plan_hash or "")
            record.artifact_set_hash = str(identity.get("artifact_set_hash") or "")
            record.target_system_id = str(_field(plan, "target_system_id", "") or "")
            record.target_environment = str(_field(plan, "target_environment", "") or "")
            record.started_at = started_at
            record.finished_at = finished_at
            record.duration_ms = float(duration_ms) if duration_ms is not None else None
            record.manifest_path = _manifest_ref(
                artifact_root,
                context,
                _field(summary, "manifest_path"),
            )
            record.report_paths = report_paths
            record.report_content_hash = report_hash
            record.result_summary = result_summary
            record.errors = errors
            record.save()


def get_default_run_history_recorder() -> RunHistoryRecorder | None:
    """Enable persistence automatically only inside a configured Django process."""

    try:
        from django.apps import apps
        from django.conf import settings
    except ImportError:
        return None
    if not settings.configured or not apps.ready:
        return None
    if not apps.is_installed("apps.test_platform"):
        return None
    return DjangoRunHistoryRecorder()


__all__ = [
    "generate_run_id",
    "DjangoRunHistoryRecorder",
    "RunHistoryError",
    "RunHistoryRecorder",
    "RunIdConflict",
    "get_default_run_history_recorder",
    "validate_run_id",
]
