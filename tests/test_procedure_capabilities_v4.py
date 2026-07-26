from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from apps.test_platform.runners.procedure import ProcedureRunner
from apps.test_platform.runners.contracts import RunnerError, RuntimeContext


class _Element:
    def __init__(self, attributes=None, frame=None):
        self.attributes = dict(attributes or {})
        self._frame = frame

    def get_attribute(self, name):
        return self.attributes.get(name, "")

    def content_frame(self):
        return self._frame


class _Locator:
    def __init__(self, name="", *, count=1, items=None):
        self.name = name
        self._items = list(items or [])
        self._count = len(self._items) if items is not None else count
        self.events = []
        self.value = ""
        self.checked = False
        self.attributes = {}
        self._element = None

    @property
    def first(self):
        return self._items[0] if self._items else self

    def nth(self, index):
        return self._items[index]

    def count(self):
        return self._count

    def element_handle(self):
        return self._element

    def click(self, **_kwargs):
        self.events.append("click")

    def fill(self, value):
        self.value = str(value)
        self.events.append(("fill", self.value))

    def set_input_files(self, value):
        self.value = str(value)
        self.events.append(("upload", self.value))

    def wait_for(self, **kwargs):
        self.events.append(("wait", kwargs.get("state")))

    def input_value(self):
        return self.value

    def is_checked(self):
        return self.checked

    def get_attribute(self, name):
        return self.attributes.get(name)

    def evaluate(self, _script):
        return self.checked


class _Frame:
    def __init__(self, name="", url="", element=None):
        self.name = name
        self.url = url
        self.child_frames = []
        self._element = element or _Element()
        self.locators = {}

    def frame_element(self):
        return self._element

    def _lookup(self, key):
        return self.locators.get(key, _Locator(key, count=0))

    def locator(self, selector):
        return self._lookup(("locator", selector))

    def get_by_label(self, target, **_kwargs):
        return self._lookup(("label", target))

    def get_by_placeholder(self, target, **_kwargs):
        return self._lookup(("placeholder", target))

    def get_by_role(self, role, **kwargs):
        return self._lookup((role, kwargs.get("name")))

    def get_by_text(self, target, **_kwargs):
        return self._lookup(("text", target))


class _Context:
    def __init__(self, pages=None):
        self.pages = list(pages or [])


class _Page(_Frame):
    def __init__(self, title="", url="https://example.test/"):
        super().__init__(name="main", url=url)
        self.main_frame = self
        self.context = _Context([self])
        self._title = title
        self._closed = False
        self.front = False

    def title(self):
        return self._title

    def is_closed(self):
        return self._closed

    def bring_to_front(self):
        self.front = True

    def wait_for_load_state(self, *_args, **_kwargs):
        return None


class _Download:
    suggested_filename = "report.pdf"

    def save_as(self, path):
        Path(path).write_bytes(b"report")


class _DownloadEvent:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @property
    def value(self):
        return _Download()


class ProcedureProcedureCapabilityTests(unittest.TestCase):
    def test_frame_index_and_locator_texts_resolve_exact_candidate(self):
        page = _Page()
        child = _Frame("details", "https://example.test/frame")
        page.child_frames = [child]
        first = _Locator("first")
        second = _Locator("second")
        child.locators[("label", "Backup label")] = _Locator(
            "choices", items=[first, second]
        )

        ProcedureRunner()._action(
            page,
            {
                "action": "click",
                "target": "Missing label",
                "locator_texts": ["Backup label"],
                "frame_path": ["details"],
                "index": 1,
            },
            {},
            RuntimeContext(),
            {},
        )

        self.assertEqual(first.events, [])
        self.assertEqual(second.events, ["click"])

    def test_switch_context_returns_selected_page(self):
        first = _Page("First")
        second = _Page("Report", "https://example.test/report")
        context = _Context([first, second])
        first.context = second.context = context

        active = ProcedureRunner()._action(
            first,
            {"action": "switch_context", "context_type": "tab", "title": "Report"},
            {},
            RuntimeContext(),
            {},
        )

        self.assertIs(active, second)
        self.assertTrue(second.front)

    def test_switch_context_rejects_ambiguous_title(self):
        first = _Page("Same")
        second = _Page("Same")
        context = _Context([first, second])
        first.context = second.context = context
        with self.assertRaisesRegex(RunnerError, "上下文无法唯一解析"):
            ProcedureRunner()._action(
                first,
                {"action": "switch_context", "title": "Same"},
                {},
                RuntimeContext(),
                {},
            )

    def test_download_records_file_and_download_check_rejects_wrong_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            page = _Page()
            button = _Locator("Export")
            page.locators[("button", "Export")] = button
            page.expect_download = lambda: _DownloadEvent()
            state = {}
            runner = ProcedureRunner()
            runner._action(
                page,
                {"action": "download", "target": "Export", "expected_filename": "report.pdf"},
                {},
                RuntimeContext(evidence_dir=Path(temporary)),
                state,
            )
            runner._check(
                page,
                {"type": "download_success", "expected_filename": "report.pdf"},
                {},
                state,
            )
            self.assertTrue(Path(state["last_download"]["path"]).is_file())
            with self.assertRaisesRegex(RuntimeError, "filename mismatch"):
                runner._check(
                    page,
                    {"type": "download_success", "expected_filename": "other.pdf"},
                    {},
                    state,
                )

    def test_upload_check_requires_real_file_and_matching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "avatar.png"
            source.write_bytes(b"image")
            page = _Page()
            field = _Locator("Avatar")
            page.locators[("label", "Avatar")] = field
            state = {}
            runner = ProcedureRunner()
            runner._action(
                page,
                {"action": "upload", "target": "Avatar", "value": str(source)},
                {},
                RuntimeContext(),
                state,
            )
            runner._check(page, {"type": "upload_success", "target": "Avatar"}, {}, state)
            with self.assertRaisesRegex(RuntimeError, "upload target mismatch"):
                runner._check(page, {"type": "upload_success", "target": "Receipt"}, {}, state)

    def test_transfer_pdf_and_page_closed_checks_fail_closed(self):
        runner = ProcedureRunner()
        page = _Page(url="https://example.test/report.pdf")
        runner._check(page, {"type": "pdf_loaded"}, {}, {})
        page.url = "https://example.test/report"
        with self.assertRaisesRegex(RuntimeError, "pdf not loaded"):
            runner._check(page, {"type": "pdf_loaded"}, {}, {})
        with self.assertRaisesRegex(RuntimeError, "page still open"):
            runner._check(page, {"type": "page_closed"}, {}, {})
        page._closed = True
        runner._check(page, {"type": "page_closed"}, {}, {})

    def test_selected_uses_locator_texts_and_structured_state(self):
        page = _Page()
        option = _Locator("Approved")
        option.checked = True
        page.locators[("text", "Approved option")] = option
        ProcedureRunner()._check(
            page,
            {
                "type": "selected",
                "target": "Missing",
                "locator_texts": ["Approved option"],
            },
            {},
            {},
        )
        option.checked = False
        with self.assertRaisesRegex(RuntimeError, "selected value mismatch"):
            ProcedureRunner()._check(
                page,
                {
                    "type": "selected",
                    "target": "Missing",
                    "locator_texts": ["Approved option"],
                },
                {},
                {},
            )


if __name__ == "__main__":
    unittest.main()
