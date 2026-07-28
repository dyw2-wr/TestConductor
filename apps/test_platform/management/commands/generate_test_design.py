from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.test_platform.generation_service import generate_design_artifact


class Command(BaseCommand):
    help = "在后台生成一个测试计划产物"

    def add_arguments(self, parser):
        parser.add_argument("--workflow-id", type=int, required=True)
        parser.add_argument("--count", type=int, default=1)
        parser.add_argument("--previous-artifact-id", type=int)

    def handle(self, *args, **options):
        try:
            count = int(options["count"])
            if not 1 <= count <= 10:
                raise CommandError("--count 必须在 1 到 10 之间")
            artifacts = []
            for index in range(count):
                artifacts.append(
                    generate_design_artifact(
                        options["workflow_id"],
                        previous_artifact_id=(
                            options["previous_artifact_id"] if index == 0 else None
                        ),
                    )
                )
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
        self.stdout.write(
            self.style.SUCCESS(",".join(artifact.artifact_id for artifact in artifacts))
        )
