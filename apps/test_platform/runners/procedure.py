"""Execute approved Procedure calls locally with Playwright."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from time import perf_counter
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from apps.test_platform.ui_modules import UiModuleCatalog, UiModuleCatalogError

from .base import (
    ExecutorRunner,
    artifact_stage_identity,
    prepare_artifact,
    write_evidence,
)
from .contracts import (
    RunResult,
    RunStatus,
    RunnerError,
    RuntimeContext,
    StepResult,
    finish_result,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _playwright_session(headless: bool) -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RunnerError(
            "UI_RUNTIME_MISSING",
            "TestConductor 未安装 Playwright",
        ) from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def _substitute(value: Any, parameters: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        result = value
        for name, parameter in parameters.items():
            result = result.replace("${" + name + "}", str(parameter))
        return result
    if isinstance(value, list):
        return [_substitute(item, parameters) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, parameters) for key, item in value.items()}
    return value


class ProcedureRunner(ExecutorRunner):
    """Run exact Procedure versions from the selected asset database."""

    executor_kind = "procedure_playwright"
    artifact_schema = "procedure-stage-bundle.v4"

    def __init__(
        self,
        *,
        session_factory: Callable[[bool], Any] | None = None,
    ) -> None:
        self.session_factory = session_factory or _playwright_session

    def _prepare(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ):
        workspace = prepare_artifact(
            artifact_dir,
            artifact_bundle,
            expected_executor=self.executor_kind,
        )
        manifest = workspace.manifest
        if manifest.get("artifact_schema_version") != self.artifact_schema:
            raise RunnerError("UI_ARTIFACT_SCHEMA_INVALID", "UI 产物版本不受支持")
        database = str(context.procedure_asset_database or "").strip()
        if not database:
            raise RunnerError("UI_ASSET_DATABASE_MISSING", "未提供沉淀资产库")
        try:
            catalog = UiModuleCatalog.from_asset_database(database)
        except UiModuleCatalogError as exc:
            raise RunnerError("UI_ASSET_DATABASE_INVALID", str(exc)) from exc
        expected_library_hash = "sha256:" + catalog.library_hash
        if (
            manifest.get("library_id") != catalog.library_id
            or manifest.get("library_hash") != expected_library_hash
            or (
                context.procedure_library_id
                and context.procedure_library_id != catalog.library_id
            )
            or (
                context.procedure_library_hash
                and context.procedure_library_hash != catalog.library_hash
            )
        ):
            raise RunnerError(
                "UI_ASSET_LIBRARY_MISMATCH",
                "当前沉淀资产库与已审批计划不一致",
            )
        calls = manifest.get("procedure_calls")
        if not isinstance(calls, list) or not calls:
            raise RunnerError("UI_PROCEDURE_CALLS_INVALID", "UI manifest 缺少 Procedure 调用")
        for call in calls:
            if not isinstance(call, Mapping):
                raise RunnerError("UI_PROCEDURE_CALLS_INVALID", "Procedure 调用格式无效")
            fingerprint = str(call.get("procedure_fingerprint") or "")
            if not fingerprint.startswith("sha256:"):
                raise RunnerError("UI_PROCEDURE_IDENTITY_INVALID", "Procedure 指纹无效")
            try:
                catalog.get(
                    str(call.get("procedure_id") or ""),
                    call.get("procedure_version"),
                    fingerprint=fingerprint.removeprefix("sha256:"),
                )
            except UiModuleCatalogError as exc:
                raise RunnerError("UI_PROCEDURE_IDENTITY_INVALID", str(exc)) from exc
            self._parameters(call, catalog, context)
        return workspace, catalog, calls

    @staticmethod
    def _parameters(
        call: Mapping[str, Any],
        catalog: UiModuleCatalog,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        fingerprint = str(call["procedure_fingerprint"]).removeprefix("sha256:")
        module = catalog.get(
            str(call["procedure_id"]),
            int(call["procedure_version"]),
            fingerprint=fingerprint,
        )
        input_refs: dict[str, str] = {}
        for binding in call.get("data_bindings") or []:
            if not isinstance(binding, Mapping) or not isinstance(
                binding.get("input_refs"), Mapping
            ):
                raise RunnerError("UI_DATA_BINDING_INVALID", "UI 数据绑定格式无效")
            for slot, variable_ref in binding["input_refs"].items():
                name = str(slot).removeprefix("input.")
                if not name or name in input_refs:
                    raise RunnerError("UI_DATA_BINDING_INVALID", "UI 参数重复绑定")
                input_refs[name] = str(variable_ref)
        values: dict[str, Any] = {}
        for parameter in module.required_parameters:
            name = str(parameter["name"])
            source = str(parameter["source"])
            variable_ref = (
                input_refs.get(name)
                if source == "input_data"
                else str(parameter["source_key"])
            )
            if not variable_ref or variable_ref not in context.variables:
                raise RunnerError(
                    "UI_VARIABLE_MISSING",
                    f"Procedure 参数 {name} 缺少运行时变量 {variable_ref or '<empty>'}",
                )
            values[name] = context.variables[variable_ref]
        return values

    @staticmethod
    def _frame_scope(page, item: Mapping[str, Any]):
        raw_path = item.get("frame_path")
        if isinstance(raw_path, (list, tuple)):
            parts = [str(value or "").strip() for value in raw_path if str(value or "").strip()]
        else:
            value = str(raw_path or item.get("frame") or "").strip()
            parts = [value] if value else []
        if not parts:
            return page
        scope = page.main_frame
        for part in parts:
            if part.startswith("css="):
                locator = scope.locator(part[4:])
                if locator.count() != 1:
                    raise RunnerError("UI_FRAME_NOT_UNIQUE", f"frame 定位不唯一: {part}")
                handle = locator.first.element_handle()
                frame = handle.content_frame() if handle is not None else None
                if frame is None:
                    raise RunnerError("UI_FRAME_NOT_FOUND", f"frame 不可用: {part}")
                scope = frame
                continue
            matches = []
            for frame in scope.child_frames:
                attributes = []
                try:
                    element = frame.frame_element()
                    attributes = [
                        element.get_attribute("id"),
                        element.get_attribute("name"),
                        element.get_attribute("title"),
                        element.get_attribute("src"),
                    ]
                except Exception:
                    pass
                if part in {str(frame.name or ""), str(frame.url or ""), *map(str, attributes)}:
                    matches.append(frame)
            if len(matches) != 1:
                raise RunnerError(
                    "UI_FRAME_NOT_UNIQUE" if matches else "UI_FRAME_NOT_FOUND",
                    f"frame 路径无法唯一解析: {part}",
                )
            scope = matches[0]
        return scope

    @classmethod
    def _locator(cls, page, item: Mapping[str, Any], *, click: bool = False):
        scope = cls._frame_scope(page, item)
        locator = str(item.get("locator") or "").strip()
        index_value = item.get("index")
        index = int(index_value) if index_value not in (None, "") else None

        def select(candidate):
            count = candidate.count()
            if index is not None:
                if index < 0 or index >= count:
                    raise RunnerError("UI_TARGET_INDEX_INVALID", "Procedure 目标 index 超出范围")
                return candidate.nth(index)
            return candidate.first

        if locator:
            if locator.startswith("css="):
                return select(scope.locator(locator[4:]))
            return select(scope.locator(locator))
        placeholder = str(item.get("placeholder") or "").strip()
        target = str(item.get("target") or "").strip()
        locator_texts = item.get("locator_texts") or []
        if isinstance(locator_texts, str):
            locator_texts = [locator_texts]
        texts = [target, *(str(value or "").strip() for value in locator_texts)]
        texts = [value for pos, value in enumerate(texts) if value and value not in texts[:pos]]
        candidates = []
        if placeholder:
            candidates.append(scope.get_by_placeholder(placeholder, exact=True))
        for text in texts:
            candidates.append(scope.get_by_label(text, exact=True))
            if click:
                candidates.extend(
                    [
                        scope.get_by_role("button", name=text, exact=True),
                        scope.get_by_role("link", name=text, exact=True),
                    ]
                )
            candidates.append(scope.get_by_text(text, exact=True))
            candidates.append(scope.locator(f'[name="{text}"], [id="{text}"]'))
        for candidate in candidates:
            try:
                count = candidate.count()
                if count > 0 and (index is None or index < count):
                    return select(candidate)
            except Exception:
                continue
        if candidates:
            return select(candidates[0])
        raise RunnerError("UI_TARGET_MISSING", "Procedure 动作缺少目标")

    def _check(
        self,
        page,
        raw: Mapping[str, Any],
        parameters: Mapping[str, Any],
        state: dict[str, Any] | None = None,
    ) -> None:
        state = state if state is not None else {}
        check = _substitute(dict(raw), parameters)
        kind = str(check.get("type") or "")
        timeout = int(check.get("timeout_ms") or 10_000)
        if kind in {"page_loaded", "home_page_loaded"}:
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
            current_url = str(getattr(page, "url", "") or "")
            contains = str(check.get("url_contains") or "").strip()
            forbidden = str(check.get("url_not_equals") or "").strip()
            if contains and contains not in current_url:
                raise RuntimeError("page URL does not contain expected value")
            if forbidden and current_url.rstrip("/") == forbidden.rstrip("/"):
                raise RuntimeError("page URL matches forbidden value")
            if kind == "home_page_loaded":
                path = (urlsplit(current_url).path or "/").strip("/").lower()
                if path not in {"", "home", "index", "index.html", "default", "default.html"}:
                    raise RuntimeError("current page is not a home page")
            return
        if kind == "any_of":
            errors = []
            for child in check.get("items") or []:
                try:
                    self._check(page, child, parameters, state)
                    return
                except Exception as exc:
                    errors.append(str(exc))
            raise RuntimeError("all any_of checks failed: " + "; ".join(errors))
        if kind == "download_success":
            download = dict(state.get("last_download") or {})
            path = Path(str(download.get("path") or ""))
            expected = str(check.get("expected_filename") or "").strip()
            actual = str(download.get("suggested_filename") or path.name)
            if not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeError("download file missing or empty")
            if expected and expected != actual:
                raise RuntimeError("download filename mismatch")
            return
        if kind == "upload_success":
            upload = dict(state.get("last_upload") or {})
            expected = str(check.get("target") or "").strip()
            path = Path(str(upload.get("path") or ""))
            if (
                not upload
                or not path.is_file()
                or (expected and upload.get("target") != expected)
            ):
                raise RuntimeError("upload target mismatch")
            return
        if kind == "pdf_loaded":
            url = str(getattr(page, "url", "") or "").lower()
            if ".pdf" in url or url.startswith("blob:") or "chrome-extension" in url:
                return
            if self._frame_scope(page, check).locator(
                'iframe[src*=".pdf"], embed[type="application/pdf"], object[type="application/pdf"]'
            ).count() <= 0:
                raise RuntimeError("pdf not loaded")
            return
        if kind == "page_closed":
            if not bool(page.is_closed()):
                raise RuntimeError("page still open")
            return
        locator = self._locator(page, check)
        if kind == "visible":
            locator.wait_for(state="visible", timeout=timeout)
        elif kind == "not_visible":
            locator.wait_for(state="hidden", timeout=timeout)
        elif kind == "field_value":
            if locator.input_value() != str(check.get("value") or ""):
                raise RuntimeError("field value mismatch")
        elif kind == "selected":
            expected = str(check.get("value") or "")
            selected = False
            try:
                selected = locator.is_checked()
            except Exception:
                pass
            if not selected:
                try:
                    selected = locator.get_attribute("aria-selected") == "true"
                except Exception:
                    pass
            if not selected:
                try:
                    value = locator.input_value()
                    selected = value == (expected or str(check.get("target") or ""))
                except Exception:
                    pass
            if not selected:
                try:
                    selected = bool(
                        locator.evaluate(
                            "el => Boolean(el.selected || el.checked || el.getAttribute('aria-selected') === 'true')"
                        )
                    )
                except Exception:
                    pass
            if not selected:
                raise RuntimeError("selected value mismatch")
        else:
            raise RunnerError("UI_CHECK_UNSUPPORTED", f"不支持的 Procedure 检查: {kind}")

    def _action(
        self,
        page,
        raw: Mapping[str, Any],
        parameters: Mapping[str, Any],
        context: RuntimeContext,
        state: dict[str, Any] | None = None,
    ):
        state = state if state is not None else {}
        item = _substitute(dict(raw), parameters)
        action = str(item.get("action") or "")
        if action == "navigate":
            page.goto(str(item["target"]), wait_until="domcontentloaded")
        elif action == "back":
            page.go_back(wait_until="domcontentloaded")
        elif action == "wait":
            page.wait_for_timeout(int(item["duration_ms"]))
        elif action == "click":
            self._locator(page, item, click=True).click(
                button=str(item.get("button") or "left"),
                click_count=int(item.get("click_count") or 1),
                modifiers=list(item.get("modifiers") or []),
            )
        elif action == "input":
            locator = self._locator(page, item)
            locator.fill(str(item.get("value") or ""))
            if item.get("submit_mode") == "enter":
                locator.press("Enter")
        elif action == "select":
            value = item.get("value")
            if value in (None, ""):
                value = (item.get("choice_path") or [""])[-1]
            self._locator(page, item).select_option(label=str(value))
        elif action == "hover":
            self._locator(page, item).hover()
        elif action == "press":
            target = str(item.get("target") or "").strip()
            if target or item.get("locator"):
                self._locator(page, item).press(str(item["key"]))
            else:
                page.keyboard.press(str(item["key"]))
        elif action == "upload":
            path = str(item["value"])
            self._locator(page, item).set_input_files(path)
            state["last_upload"] = {"target": str(item.get("target") or ""), "path": path}
        elif action == "download":
            target = self._locator(page, item, click=True)
            with page.expect_download() as pending:
                target.click()
            download = pending.value
            root = context.evidence_dir or Path(gettempdir()) / "test_conductor-downloads"
            root.mkdir(parents=True, exist_ok=True)
            suggested = str(download.suggested_filename or "download.bin")
            destination = root / f"{uuid4().hex}-{suggested}"
            download.save_as(str(destination))
            expected = str(item.get("expected_filename") or "").strip()
            if expected and expected != suggested:
                raise RuntimeError("download filename mismatch")
            state["last_download"] = {
                "path": str(destination),
                "suggested_filename": suggested,
            }
        elif action == "drag":
            drop_target = str(item.get("drop_target") or "").strip()
            drop_locator = str(item.get("drop_locator") or "").strip()
            if drop_target or drop_locator:
                source = self._locator(page, item)
                destination = self._locator(
                    page,
                    {"target": drop_target, "locator": drop_locator},
                )
                source.drag_to(destination)
            else:
                ranges = page.locator('input[type="range"]')
                source = None
                expected_current = str(item.get("target") or "").strip()
                for index in range(ranges.count()):
                    candidate = ranges.nth(index)
                    if not expected_current or candidate.input_value() == expected_current:
                        source = candidate
                        break
                if source is None:
                    raise RunnerError(
                        "UI_TARGET_MISSING",
                        "未找到匹配当前值的 range 控件",
                    )
                source.fill(str(item.get("value") or item.get("percent") or ""))
                source.dispatch_event("input")
                source.dispatch_event("change")
        elif action == "scroll":
            if item.get("until_visible"):
                page.get_by_text(str(item["until_visible"]), exact=True).scroll_into_view_if_needed()
            else:
                amount = int(item.get("amount") or 600)
                if str(item.get("direction") or "down") in {"up", "left"}:
                    amount = -amount
                page.mouse.wheel(0, amount)
        elif action == "menu_path":
            for target in item.get("path") or []:
                self._locator(page, {"target": target}, click=True).click()
        elif action in {"verify", "assert_result"}:
            self._check(
                page,
                {
                    "type": (
                        "visible"
                        if item.get("expected_visible", True)
                        else "not_visible"
                    ),
                    "target": item.get("target"),
                    "timeout_ms": item.get("timeout_ms", 10_000),
                },
                parameters,
                state,
            )
        elif action == "screenshot":
            if context.evidence_dir is not None:
                context.evidence_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(context.evidence_dir / f"{item.get('name') or 'procedure'}.png"),
                    full_page=True,
                )
        elif action == "close":
            page.close()
        elif action == "switch_context":
            pages = list(page.context.pages)
            candidates = [candidate for candidate in pages if not candidate.is_closed()]
            title = str(item.get("title") or item.get("target") or "").strip()
            url = str(item.get("url") or "").strip()
            if title:
                candidates = [candidate for candidate in candidates if candidate.title() == title]
            if url:
                candidates = [candidate for candidate in candidates if url in str(candidate.url or "")]
            index_value = item.get("index")
            if index_value not in (None, ""):
                index = int(index_value)
                candidates = [candidates[index]] if 0 <= index < len(candidates) else []
            if len(candidates) != 1:
                raise RunnerError("UI_CONTEXT_NOT_UNIQUE", "页面上下文无法唯一解析")
            page = candidates[0]
            page.bring_to_front()
            page.wait_for_load_state("domcontentloaded")
        elif action == "remember":
            return
        else:
            raise RunnerError(
                "UI_ACTION_UNSUPPORTED",
                f"不支持的 Procedure 动作: {action}",
            )
        return page

    def preflight(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
    ) -> None:
        self._prepare(artifact_dir, artifact_bundle, context)

    def run(
        self,
        artifact_dir: Path,
        artifact_bundle: Any,
        context: RuntimeContext,
        **_: Any,
    ) -> RunResult:
        workspace, catalog, calls = self._prepare(
            artifact_dir,
            artifact_bundle,
            context,
        )
        flow_id, stage_id = artifact_stage_identity(artifact_bundle)
        result = RunResult.new(
            run_id=f"run-{uuid4().hex}",
            executor_kind=self.executor_kind,
            flow_id=flow_id,
            stage_id=stage_id,
        )
        result.manifest_path = str(workspace.manifest_path)
        result.external_action_started = True
        try:
            with self.session_factory(context.ui_browser_headless) as page:
                state: dict[str, Any] = {}
                for call in calls:
                    started = perf_counter()
                    procedure_id = str(call["procedure_id"])
                    version = int(call["procedure_version"])
                    fingerprint = str(call["procedure_fingerprint"]).removeprefix(
                        "sha256:"
                    )
                    parameters = self._parameters(call, catalog, context)
                    payload = catalog.payload(
                        procedure_id,
                        version,
                        fingerprint=fingerprint,
                    )
                    precondition = payload["precondition"]
                    url_prefix = str(precondition.get("url_prefix") or "")
                    if not str(getattr(page, "url", "") or "").startswith(url_prefix):
                        page.goto(url_prefix, wait_until="domcontentloaded")
                    for segment in payload["segments"]:
                        for item in segment["items"]:
                            page = self._action(page, item, parameters, context, state)
                        for check in segment["completion_checks"]:
                            self._check(page, check, parameters, state)
                    for check in payload["postcondition"]["checks"]:
                        self._check(page, check, parameters, state)
                    result.steps.append(
                        StepResult(
                            step_id=str(call["row_id"]),
                            status=RunStatus.PASSED,
                            message=f"已执行 {procedure_id}@v{version}",
                            duration_ms=(perf_counter() - started) * 1000,
                            details={
                                "procedure_id": procedure_id,
                                "version": version,
                                "fingerprint": "sha256:" + fingerprint,
                            },
                        )
                    )
            evidence = write_evidence(
                context,
                f"{flow_id}-{stage_id}-ui",
                {
                    "library_id": catalog.library_id,
                    "library_hash": "sha256:" + catalog.library_hash,
                    "steps": [step.as_dict() for step in result.steps],
                    "finished_at": _now(),
                },
            )
            if evidence:
                result.evidence.append(evidence)
            return finish_result(result, RunStatus.PASSED)
        except RunnerError:
            raise
        except Exception as exc:
            detail = " ".join(str(exc).split())[:500]
            result.errors.append(
                f"UI_PROCEDURE_EXECUTION_FAILED: {type(exc).__name__}"
                + (f": {detail}" if detail else "")
            )
            result.steps.append(
                StepResult(
                    step_id=(
                        str(calls[len(result.steps)].get("row_id") or "ui")
                        if len(result.steps) < len(calls)
                        else "ui"
                    ),
                    status=RunStatus.FAILED,
                    message="Procedure 执行失败",
                )
            )
            return finish_result(result, RunStatus.FAILED)


__all__ = ["ProcedureRunner"]
