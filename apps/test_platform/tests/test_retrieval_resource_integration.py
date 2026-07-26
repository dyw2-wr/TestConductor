"""UI Procedure assets do not depend on navigation/vector retrieval."""

from __future__ import annotations

from pathlib import Path
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.test_platform.models import TestResourceProfile
from apps.test_platform.planning.resources import resolve_test_resources
from apps.test_platform.tests.test_resource_resolution import _bundle

from .ui_asset_fixture import build_asset_database


@override_settings(TEST_PLATFORM_PROCEDURE_DISCOVERY_ENABLED=True)
class RetrievalResourceIntegrationTests(TestCase):
    def test_ui_assets_ignore_removed_navigation_discovery_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "portal.sqlite"
            expected = build_asset_database(database)
            profile = TestResourceProfile.objects.create(
                name="Portal UI",
                system_id="portal-suite",
                environment="staging",
                ui_procedure_database=SimpleUploadedFile(
                    "portal.sqlite",
                    database.read_bytes(),
                    content_type="application/vnd.sqlite3",
                ),
            )

            resolved = resolve_test_resources(profile, _bundle())

        ui = resolved.catalog.procedure_profiles[0]
        self.assertEqual(ui.library_id, expected["library_id"])
        self.assertNotIn("retrieval_context", ui.model_dump(mode="json"))
        self.assertNotIn("navigation_profile", ui.model_dump(mode="json"))
