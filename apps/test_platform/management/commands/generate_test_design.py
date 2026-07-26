from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.test_platform.generation_service import generate_design_artifact


class Command(BaseCommand):
    help = "在后台生成一个测试计划产物"

    def add_arguments(self, parser):
        parser.add_argument("--workflow-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            artifact = generate_design_artifact(options["workflow_id"])
        except Exception as exc:
            from apps.test_platform.models import TestWorkflow

            workflow = TestWorkflow.objects.filter(pk=options["workflow_id"]).first()
            progress = dict(getattr(workflow, "generation_progress", None) or {})
            TestWorkflow.objects.filter(pk=options["workflow_id"]).update(
                status=TestWorkflow.Status.ERROR,
                last_error=str(exc)[:20_000],
                generation_progress={
                    "phase": "failed",
                    "message": "测试计划生成失败",
                    "percent": int(progress.get("percent") or 0),
                    "updated_at": timezone.now().isoformat(),
                },
            )
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(artifact.artifact_id))
