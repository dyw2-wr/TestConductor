"""Lightweight HTTP entry points for the new test-platform boundary.

The intent pipeline itself remains an application service. These endpoints are
deliberately limited to health/version discovery until the authenticated
intent API is designed and persisted.
"""

import json
import mimetypes
from pathlib import Path, PurePosixPath
import shutil

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from .ingestion import INGESTION_SCHEMA_VERSION, supported_extensions
from .models import (
    ApprovedKnowledgeEntry,
    TestExecutionRun,
    TestResourceProfile,
    TestWorkflow,
)


def _run_root(record: TestExecutionRun) -> Path:
    storage_root = Path(settings.TEST_PLATFORM_ARTIFACT_ROOT).resolve()
    run_root = (storage_root / Path(record.storage_root_ref)).resolve()
    try:
        run_root.relative_to(storage_root)
    except ValueError as exc:
        raise Http404("运行存储路径无效") from exc
    return run_root


def _run_record(run_id: str) -> TestExecutionRun:
    try:
        return TestExecutionRun.objects.get(run_id=run_id)
    except TestExecutionRun.DoesNotExist as exc:
        raise Http404("运行记录不存在") from exc


def _safe_run_file(run_root: Path, path_ref: str) -> Path:
    ref = PurePosixPath(str(path_ref or "").replace("\\", "/"))
    if (
        ref.is_absolute()
        or not ref.parts
        or any(part in {"", ".", ".."} for part in ref.parts)
    ):
        raise Http404("文件路径无效")
    path = (run_root / Path(*ref.parts)).resolve()
    try:
        path.relative_to(run_root)
    except ValueError as exc:
        raise Http404("文件路径无效") from exc
    if not path.is_file():
        raise Http404("文件不存在")
    return path


def _report_payload(record: TestExecutionRun, run_root: Path) -> dict:
    json_ref = (record.report_paths or {}).get("json")
    if not json_ref:
        raise Http404("JSON 报告不存在")
    path = _safe_run_file(run_root, json_ref)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Http404("JSON 报告无法读取") from exc
    if not isinstance(value, dict) or value.get("run_id") != record.run_id:
        raise Http404("JSON 报告身份无效")
    return value


def _download(path: Path) -> FileResponse:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = FileResponse(
        path.open("rb"),
        content_type=content_type,
        as_attachment=True,
        filename=path.name,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def uploaded_file(request, path_ref: str):
    """Serve only files referenced by a workflow or resource row."""

    normalized = PurePosixPath(str(path_ref or "").replace("\\", "/"))
    if (
        normalized.is_absolute()
        or not normalized.parts
        or any(part in {"", ".", ".."} for part in normalized.parts)
    ):
        raise Http404("上传文件路径无效")
    value = normalized.as_posix()
    workflow_match = any(
        TestWorkflow.objects.filter(**{field_name: value}).exists()
        for field_name in ("requirement_file",)
    )
    resource_match = any(
        TestResourceProfile.objects.filter(**{field_name: value}).exists()
        for field_name in (
            "ui_procedure_database",
            "api_openapi_file",
            "database_query_file",
            "performance_profile_file",
        )
    )
    knowledge_match = ApprovedKnowledgeEntry.objects.filter(source_file=value).exists()
    if not workflow_match and not resource_match and not knowledge_match:
        raise Http404("上传文件未登记")
    media_root = Path(settings.MEDIA_ROOT).resolve()
    path = (media_root / Path(*normalized.parts)).resolve()
    try:
        path.relative_to(media_root)
    except ValueError as exc:
        raise Http404("上传文件路径无效") from exc
    if not path.is_file():
        raise Http404("上传文件不存在")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = FileResponse(path.open("rb"), content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{path.name}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@require_GET
def health(request):
    """Return a dependency-light liveness response."""

    return JsonResponse(
        {
            "status": "ok",
            "service": "test_platform",
            "layer": "design_and_planning",
        }
    )


@require_GET
def version(request):
    """Expose the currently supported design, planning, and catalog versions."""

    return JsonResponse(
        {
            "service": "test_platform",
            "design_schema_version": "test-design.v4",
            "planning_schema_version": "test-plan.v4",
            "catalog_schema_version": "planning-catalog.v4",
            "ingestion_schema_version": INGESTION_SCHEMA_VERSION,
            "supported_input_modes": ["frontend_text", "file_upload"],
            "ingestion_entrypoint": "apps.test_platform.ingestion.prepare_request",
            "ingestion_http_endpoint": None,
            "business_entrypoint": "/admin/test_platform/testintentimport/",
            "local_business_ui": True,
            "supported_file_extensions": list(supported_extensions()),
            "ocr_requires_external_engine": True,
            "ocr_engine_available": shutil.which("tesseract") is not None,
            "supported_channels": [
                "ui",
                "api",
                "database",
                "performance",
                "port",
            ],
            "implemented_executors": [
                "http_api",
                "database",
                "performance",
                "tcp_port",
            ],
            "external_executors": ["procedure_playwright"],
            "deferred_executors": [],
        }
    )


@require_GET
def report(request, run_id: str, kind: str):
    """Return one recorded report while enforcing the configured storage root."""

    if kind not in {"html", "json", "junit"}:
        raise Http404("未知报告类型")
    record = _run_record(run_id)
    path_ref = record.report_paths.get(kind)
    if not path_ref:
        raise Http404("报告不存在")
    run_root = _run_root(record)
    path = _safe_run_file(run_root, path_ref)
    if kind == "html" and (record.report_paths or {}).get("json"):
        from .reporting import TestReportGenerator

        payload = _report_payload(record, run_root)
        response = HttpResponse(
            TestReportGenerator().render_html(
                payload,
                evidence_prefix="../evidence",
            ),
            content_type="text/html; charset=utf-8",
        )
        response["Content-Disposition"] = f'inline; filename="{run_id}-html.html"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = (
            "sandbox allow-top-navigation-by-user-activation"
        )
        return response
    content_types = {
        "html": "text/html; charset=utf-8",
        "json": "application/json",
        "junit": "application/xml",
    }
    response = FileResponse(path.open("rb"), content_type=content_types[kind])
    response["Content-Disposition"] = f'inline; filename="{run_id}-{kind}{path.suffix}"'
    response["X-Content-Type-Options"] = "nosniff"
    if kind == "html":
        response["Content-Security-Policy"] = (
            "sandbox allow-top-navigation-by-user-activation"
        )
    return response


@require_GET
def evidence(request, run_id: str, name: str):
    """Download one evidence file that is explicitly listed in this run's report."""

    if Path(name).name != name or name in {".", ".."}:
        raise Http404("证据名称无效")
    record = _run_record(run_id)
    run_root = _run_root(record)
    payload = _report_payload(record, run_root)
    allowed = {Path(str(value)).name for value in payload.get("evidence", [])}
    if name not in allowed:
        raise Http404("证据未登记在本批次报告中")
    return _download(_safe_run_file(run_root, f"evidence/{name}"))


@require_GET
def artifact(request, run_id: str, path_ref: str):
    """Download one compiled artifact that is explicitly listed in the report."""

    record = _run_record(run_id)
    run_root = _run_root(record)
    payload = _report_payload(record, run_root)
    allowed = {
        str(item.get("artifact_path_ref"))
        for flow in payload.get("flows", [])
        if isinstance(flow, dict)
        for stage in flow.get("stages", [])
        if isinstance(stage, dict)
        for item in stage.get("artifacts", [])
        if isinstance(item, dict) and item.get("artifact_path_ref")
    }
    normalized = PurePosixPath(str(path_ref).replace("\\", "/")).as_posix()
    if normalized not in allowed:
        raise Http404("执行产物未登记在本批次报告中")
    return _download(_safe_run_file(run_root, normalized))
