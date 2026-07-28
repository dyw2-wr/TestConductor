import json

from django.test import RequestFactory, SimpleTestCase

from apps.test_platform.admin import TestResourceProfileForm as ResourceProfileForm
from apps.test_platform.models import TestResourceProfile as ResourceProfile
from apps.test_platform.runners import RunnerRegistry
from apps.test_platform.views import version


class AgentUiOnlyTests(SimpleTestCase):
    def test_resource_model_only_exposes_agent_ui_inputs(self):
        field_names = {field.name for field in ResourceProfile._meta.fields}

        self.assertIn("ui_agent_asset_file", field_names)
        self.assertIn("ui_agent_asset_text", field_names)
        self.assertNotIn("ui_procedure_database", field_names)
        self.assertNotIn("ui_sediment_file", field_names)

    def test_resource_form_has_no_ui_execution_mode_switch(self):
        field_names = set(ResourceProfileForm.base_fields)

        self.assertIn("ui_agent_asset_file", field_names)
        self.assertIn("ui_agent_asset_text", field_names)
        self.assertNotIn("ui_execution_mode", field_names)
        self.assertNotIn("ui_procedure_database", field_names)
        self.assertNotIn("ui_sediment_file", field_names)

    def test_runner_registry_only_registers_agent_for_ui(self):
        registered = set(RunnerRegistry().registered_kinds)

        self.assertIn("stagehand_agent", registered)
        self.assertNotIn("procedure_playwright", registered)
        self.assertNotIn("sediment_playwright", registered)

    def test_version_capabilities_only_publish_agent_ui(self):
        response = version(RequestFactory().get("/version/"))
        payload = json.loads(response.content)

        self.assertIn("stagehand_agent", payload["implemented_executors"])
        self.assertNotIn("procedure_playwright", payload["implemented_executors"])
        self.assertNotIn("sediment_playwright", payload["implemented_executors"])
        self.assertNotIn("procedure_playwright", payload["external_executors"])
        self.assertNotIn("sediment_playwright", payload["external_executors"])
