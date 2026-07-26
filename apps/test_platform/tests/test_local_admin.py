import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test import override_settings

from apps.test_platform.models import ApprovedKnowledgeEntry, TestResourceProfile


class LocalAdminTests(TestCase):
    def test_admin_opens_without_login(self):
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "用户名:")
        self.assertNotContains(response, 'action="/admin/login/"')

    def test_account_management_routes_are_not_exposed(self):
        self.assertEqual(self.client.get("/admin/auth/user/").status_code, 404)
        self.assertEqual(self.client.get("/admin/auth/group/").status_code, 404)
        self.assertEqual(self.client.get("/admin/login/").status_code, 404)
        self.assertEqual(self.client.get("/admin/logout/").status_code, 404)
        self.assertEqual(self.client.get("/admin/password_change/").status_code, 404)

    def test_remote_requests_are_rejected(self):
        response = self.client.get("/admin/", REMOTE_ADDR="203.0.113.10")

        self.assertEqual(response.status_code, 403)

    def test_registered_ui_and_knowledge_uploads_can_be_opened(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            MEDIA_ROOT=Path(directory)
        ):
            profile = TestResourceProfile.objects.create(
                name="UI assets",
                ui_procedure_database=SimpleUploadedFile(
                    "site.sqlite", b"sqlite-assets"
                ),
            )
            knowledge = ApprovedKnowledgeEntry.objects.create(
                system_id="demo",
                title="Business rules",
                content="rules",
                source_file=SimpleUploadedFile("rules.md", b"# Rules"),
            )

            ui_response = self.client.get(
                f"/uploads/{profile.ui_procedure_database.name}"
            )
            knowledge_response = self.client.get(
                f"/uploads/{knowledge.source_file.name}"
            )

            self.assertEqual(ui_response.status_code, 200)
            self.assertEqual(b"".join(ui_response.streaming_content), b"sqlite-assets")
            self.assertEqual(knowledge_response.status_code, 200)
            self.assertEqual(b"".join(knowledge_response.streaming_content), b"# Rules")
            self.assertEqual(
                self.client.get("/uploads/test_platform/unregistered.txt").status_code,
                404,
            )
