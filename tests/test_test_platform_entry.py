"""Smoke tests for the dependency-light TestDesign v4 Django entry point."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


class TestPlatformEntryTests(unittest.TestCase):
    def test_health_and_v4_versions_load_without_legacy_modules(self):
        script = r'''
import json
import os
import sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from apps.test_platform.planning.compiler import TestPlanCompiler
from django.test import RequestFactory
from django.urls import Resolver404, resolve

TestPlanCompiler()

factory = RequestFactory()
health = resolve("/health/").func(factory.get("/health/"))
version = resolve("/version/").func(factory.get("/version/"))
try:
    resolve("/legacy/")
    legacy_registered = True
except Resolver404:
    legacy_registered = False
reserved_business_paths = [
    "/api/designs/generate",
    "/api/plans/generate",
]
registered_business_paths = []
for path in reserved_business_paths:
    try:
        resolve(path)
        registered_business_paths.append(path)
    except Resolver404:
        pass

print(json.dumps({
    "health": json.loads(health.content),
    "version": json.loads(version.content),
    "legacy_registered": legacy_registered,
    "legacy_module_loaded": "apps.core.views" in sys.modules,
    "procedure_adapter_loaded": "apps.test_platform.planning.adapters.procedure" in sys.modules,
    "procedure_runtime_loaded": any(
        name == "procedure_playwright" or name.startswith("procedure_playwright.")
        for name in sys.modules
    ),
    "registered_business_paths": registered_business_paths,
}))
'''
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["health"]["status"], "ok")
        self.assertEqual(payload["version"]["design_schema_version"], "test-design.v4")
        self.assertEqual(payload["version"]["planning_schema_version"], "test-plan.v4")
        self.assertEqual(payload["version"]["catalog_schema_version"], "planning-catalog.v4")
        self.assertEqual(
            payload["version"]["ingestion_schema_version"],
            "requirement-ingestion.v1",
        )
        self.assertEqual(
            payload["version"]["ingestion_entrypoint"],
            "apps.test_platform.ingestion.prepare_request",
        )
        self.assertIsNone(payload["version"]["ingestion_http_endpoint"])
        self.assertEqual(
            payload["version"]["supported_channels"],
            ["ui", "api", "database", "performance", "port"],
        )
        self.assertEqual(
            payload["version"]["implemented_executors"],
            ["http_api", "database", "performance", "tcp_port"],
        )
        self.assertEqual(
            payload["version"]["external_executors"],
            ["procedure_playwright"],
        )
        self.assertEqual(
            payload["version"]["deferred_executors"],
            [],
        )
        self.assertFalse(payload["legacy_registered"])
        self.assertFalse(payload["legacy_module_loaded"])
        self.assertFalse(payload["procedure_adapter_loaded"])
        self.assertFalse(payload["procedure_runtime_loaded"])
        self.assertEqual(payload["registered_business_paths"], [])


if __name__ == "__main__":
    unittest.main()
