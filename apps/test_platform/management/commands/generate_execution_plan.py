from django.core.management.base import BaseCommand, CommandError

from apps.test_platform.generation_service import generate_execution_plan_artifact


class Command(BaseCommand):
    help = "在后台生成一个执行计划产物"

    def add_arguments(self, parser):
        parser.add_argument("--artifact-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            artifact = generate_execution_plan_artifact(options["artifact_id"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(artifact.artifact_id))
