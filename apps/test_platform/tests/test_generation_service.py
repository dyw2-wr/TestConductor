from pathlib import Path
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.test_platform.generation_service import (
    artifact_generation_lock,
    generate_design_artifact,
    generate_execution_plan_artifact,
    queue_design_generation,
    queue_execution_plan_generation,
    queue_execution_plan_rebind,
)
from apps.test_platform.models import (
    ExecutionPlanArtifact,
    TestPlanArtifact,
    TestResourceProfile,
    TestWorkflow,
)
from apps.test_platform.planning.compiler import TestPlanCompiler
from apps.test_platform.planning.artifact_paths import artifact_category
from apps.test_platform.planning.planner import DefaultPlanPromptBuilder, PlanDraftGenerator
from apps.test_platform.workflow import IntentToExecutionWorkflow
from tests.test_design_layer_v4 import _pipeline, _request, _valid_candidate
from tests.test_planning_flow_v4 import (
    _approved_bundle as _approved_design_bundle,
    _candidate as _plan_candidate,
    _catalog as _planning_catalog,
)


class GenerationServiceTests(TestCase):
    def test_design_worker_persists_generated_artifact_and_workflow_state(self):
        pipeline, _ = _pipeline(_valid_candidate())
        generated = pipeline.generate(_request())
        ingested = SimpleNamespace(
            request=generated.request,
            as_dict=lambda: {
                "schema_version": "requirement-ingestion.v1",
                "request": generated.request.model_dump(mode="json"),
                "warnings": [],
                "sources": [],
            },
        )
        prepared_kwargs = {}

        def prepare_design_request(**kwargs):
            prepared_kwargs.update(kwargs)
            return ingested

        service = SimpleNamespace(
            prepare_design_request=prepare_design_request,
            generate_design=lambda *args, **kwargs: generated,
        )
        profile = TestResourceProfile.objects.create(
            name="Design worker profile",
            port_host="127.0.0.1",
            port_number=9000,
        )
        workflow = TestWorkflow.objects.create(
            title="Generate persisted design",
            requirement_text="登录锁定需求",
            resource_profile=profile,
            allowed_channels=["ui"],
            coverage_by_category={},
        )

        with patch(
            "apps.test_platform.generation_service.get_workflow",
            return_value=service,
        ):
            artifact = generate_design_artifact(workflow.pk)

        workflow.refresh_from_db()
        self.assertEqual(artifact.status, TestPlanArtifact.Status.REVIEW)
        self.assertEqual(artifact.source_intent, workflow)
        self.assertEqual(artifact.generation_result["design"]["design_id"], generated.design.design_id)
        self.assertEqual(
            artifact.generation_result["ingestion"]["schema_version"],
            "requirement-ingestion.v1",
        )
        self.assertEqual(workflow.status, TestWorkflow.Status.DESIGN_REVIEW)
        self.assertEqual(workflow.last_error, "")
        self.assertEqual(workflow.generation_progress["phase"], "completed")
        self.assertEqual(workflow.generation_progress["percent"], 100)
        self.assertEqual(prepared_kwargs["selections"]["techniques"], [])
        self.assertEqual(prepared_kwargs["selections"]["techniques_by_channel"], {})

    def test_execution_plan_worker_compiles_and_persists_reviewable_artifacts(self):
        design_bundle = _approved_design_bundle()
        catalog = _planning_catalog()
        compiler = TestPlanCompiler()

        class Gateway:
            def generate(self, messages, output_schema):
                return _plan_candidate(design_bundle)

        service = IntentToExecutionWorkflow(
            SimpleNamespace(),
            PlanDraftGenerator(DefaultPlanPromptBuilder(), Gateway(), compiler),
            plan_compiler=compiler,
        )
        profile = TestResourceProfile.objects.create(
            name="Execution worker profile",
            system_id="account-web",
            environment="staging",
            port_host="127.0.0.1",
            port_number=9000,
        )
        source = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Approved design",
            test_categories=["ui", "database"],
            design_id=design_bundle.design.design_id,
            version=design_bundle.design.version,
            content_hash=design_bundle.review.design_content_hash,
            approved_bundle=design_bundle.model_dump(mode="json"),
            status=TestPlanArtifact.Status.APPROVED,
        )
        runtime_hash = "sha256:" + "7" * 64
        resources = SimpleNamespace(
            catalog=catalog,
            runtime_config_hash=runtime_hash,
        )

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch("apps.test_platform.generation_service.subprocess.Popen"):
            placeholder = queue_execution_plan_generation(source)
            existing_root = (
                Path(directory)
                / "test-plans"
                / source.artifact_id
                / "execution-batches"
                / placeholder.artifact_id
            )
            existing_root.mkdir(parents=True)
            stale_file = existing_root / "stale.txt"
            stale_file.write_text("obsolete", encoding="utf-8")
            with patch(
                "apps.test_platform.generation_service.get_workflow",
                return_value=service,
            ), patch(
                "apps.test_platform.generation_service.resolve_test_resources",
                return_value=resources,
            ):
                result = generate_execution_plan_artifact(placeholder.pk)
            stale_file_removed = not stale_file.exists()
            artifact_root_exists = (Path(directory) / result.artifact_root_ref).is_dir()
            generated_categories = sorted(
                path.name
                for path in (
                    Path(directory) / result.artifact_root_ref / "generated-files"
                ).iterdir()
                if path.is_dir()
            )

        self.assertEqual(result.status, ExecutionPlanArtifact.Status.REVIEW)
        self.assertEqual(result.runtime_config_hash, runtime_hash)
        self.assertTrue(result.compilation_result["validation"]["passed"])
        self.assertTrue(result.compilation_result["artifacts"])
        self.assertTrue(artifact_root_exists)
        self.assertTrue(stale_file_removed)
        self.assertEqual(
            generated_categories,
            sorted(
                {
                    artifact_category(item["executor_kind"])
                    for item in result.compilation_result["artifacts"]
                }
            ),
        )
        self.assertEqual(
            result.artifact_root_ref,
            f"test-plans/{source.artifact_id}/execution-batches/{result.artifact_id}",
        )
        self.assertEqual(result.last_error, "")
        self.assertEqual(result.generation_progress["phase"], "completed")
        self.assertEqual(result.generation_progress["percent"], 100)

    def test_resource_rebind_queues_successor_without_demoting_approved_version(self):
        profile = TestResourceProfile.objects.create(
            name="Generation resource",
            port_host="127.0.0.1",
            port_number=9000,
        )
        test_plan = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Generation plan",
            test_categories=["port"],
            design_id="DESIGN-GENERATION",
            version=1,
            content_hash="sha256:" + "1" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        previous = ExecutionPlanArtifact.objects.create(
            source_test_plan=test_plan,
            resource_profile=profile,
            title="Approved execution",
            test_categories=["port"],
            plan_id="PLAN-GENERATION",
            version=1,
            content_hash="sha256:" + "2" * 64,
            compilation_result={"plan": {"plan_id": "PLAN-GENERATION"}},
            artifact_root_ref="generation/v1",
            runtime_config_hash="sha256:" + "3" * 64,
            approved_bundle={"plan": {"plan_id": "PLAN-GENERATION"}},
            status=ExecutionPlanArtifact.Status.APPROVED,
        )

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch("apps.test_platform.generation_service.subprocess.Popen") as popen:
            queued = queue_execution_plan_rebind(test_plan)

        previous.refresh_from_db()
        self.assertEqual(previous.status, ExecutionPlanArtifact.Status.APPROVED)
        self.assertEqual(queued.status, ExecutionPlanArtifact.Status.GENERATING)
        self.assertEqual(queued.version, 2)
        self.assertEqual(queued.plan_id, previous.plan_id)
        self.assertEqual(queued.generation_progress["phase"], "queued")
        command = popen.call_args.args[0]
        self.assertIn("rebind_execution_plan", command)
        self.assertIn(str(queued.pk), command)

    def test_design_generation_queue_persists_state_and_worker_command(self):
        profile = TestResourceProfile.objects.create(
            name="Design generation resource",
            port_host="127.0.0.1",
            port_number=9000,
        )
        workflow = TestWorkflow.objects.create(
            title="Generate design",
            requirement_text="端口应可连接",
            resource_profile=profile,
            allowed_channels=["port"],
            coverage_by_category={"port": ["positive"]},
        )

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch("apps.test_platform.generation_service.subprocess.Popen") as popen:
            queue_design_generation(workflow)

        workflow.refresh_from_db()
        self.assertEqual(workflow.status, TestWorkflow.Status.DESIGN_GENERATING)
        self.assertEqual(workflow.generation_progress["phase"], "queued")
        self.assertEqual(workflow.generation_progress["percent"], 0)
        command = popen.call_args.args[0]
        self.assertIn("generate_test_design", command)
        self.assertIn(str(workflow.pk), command)
        self.assertNotIn("--user-id", command)

    def test_design_generation_queue_atomically_rejects_a_stale_duplicate(self):
        profile = TestResourceProfile.objects.create(
            name="Atomic design generation",
            port_host="127.0.0.1",
            port_number=9000,
        )
        workflow = TestWorkflow.objects.create(
            title="Generate only once",
            requirement_text="端口应可连接",
            resource_profile=profile,
            allowed_channels=["port"],
            coverage_by_category={"port": ["positive"]},
        )
        stale_copy = TestWorkflow.objects.get(pk=workflow.pk)

        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch("apps.test_platform.generation_service.subprocess.Popen") as popen:
            queue_design_generation(workflow)
            with self.assertRaisesRegex(ValidationError, "不允许重新生成"):
                queue_design_generation(stale_copy)

        self.assertEqual(popen.call_count, 1)

    def test_design_worker_start_failure_is_recoverable_and_auditable(self):
        profile = TestResourceProfile.objects.create(
            name="Failed design worker",
            port_host="127.0.0.1",
            port_number=9000,
        )
        workflow = TestWorkflow.objects.create(
            title="Failed generation",
            requirement_text="端口应可连接",
            resource_profile=profile,
            allowed_channels=["port"],
            coverage_by_category={"port": ["positive"]},
        )
        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.generation_service.subprocess.Popen",
            side_effect=OSError("cannot spawn"),
        ):
            with self.assertRaisesRegex(ValidationError, "后台生成进程启动失败"):
                queue_design_generation(workflow)

        workflow.refresh_from_db()
        self.assertEqual(workflow.status, TestWorkflow.Status.ERROR)
        self.assertIn("cannot spawn", workflow.last_error)
        self.assertEqual(workflow.generation_progress["phase"], "failed")

    def test_execution_plan_worker_start_failure_marks_placeholder_error(self):
        profile = TestResourceProfile.objects.create(
            name="Failed plan worker",
            port_host="127.0.0.1",
            port_number=9000,
        )
        test_plan = TestPlanArtifact.objects.create(
            resource_profile=profile,
            title="Approved source",
            test_categories=["port"],
            design_id="DESIGN-SPAWN-FAIL",
            version=1,
            content_hash="sha256:" + "8" * 64,
            status=TestPlanArtifact.Status.APPROVED,
        )
        with tempfile.TemporaryDirectory() as directory, override_settings(
            TEST_PLATFORM_ARTIFACT_ROOT=Path(directory)
        ), patch(
            "apps.test_platform.generation_service.subprocess.Popen",
            side_effect=OSError("planner unavailable"),
        ):
            with self.assertRaisesRegex(ValidationError, "后台生成进程启动失败"):
                queue_execution_plan_generation(test_plan)

        placeholder = test_plan.execution_plans.get()
        self.assertEqual(placeholder.status, ExecutionPlanArtifact.Status.ERROR)
        self.assertIn("planner unavailable", placeholder.last_error)
        self.assertEqual(placeholder.generation_progress["phase"], "failed")

    def test_generation_lock_rejects_concurrency_and_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with artifact_generation_lock(root, "PLAN-1"):
                with self.assertRaisesRegex(ValidationError, "正在生成"):
                    with artifact_generation_lock(root, "PLAN-1"):
                        self.fail("concurrent lock unexpectedly acquired")
            with artifact_generation_lock(root, "PLAN-1"):
                self.assertTrue(
                    (root / ".generation-locks" / "PLAN-1.lock").is_file()
                )

    def test_generation_lock_is_released_when_owner_process_crashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = """
import os
import sys
from pathlib import Path
import django
django.setup()
from apps.test_platform.generation_service import artifact_generation_lock
with artifact_generation_lock(Path(sys.argv[1]), 'CRASH'):
    print('locked', flush=True)
    os._exit(23)
"""
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(root)],
                cwd=Path(__file__).resolve().parents[3],
                env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(child.stdout.readline().strip(), "locked")
            self.assertEqual(child.wait(timeout=15), 23)

            with artifact_generation_lock(root, "CRASH"):
                self.assertTrue(
                    (root / ".generation-locks" / "CRASH.lock").is_file()
                )
