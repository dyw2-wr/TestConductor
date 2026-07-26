from django.core.management.base import BaseCommand, CommandError

from apps.test_platform.execution_service import (
    execute_execution_plan_artifact,
    mark_run_error,
)


class Command(BaseCommand):
    help = "Execute one approved TestConductor execution-plan artifact"

    def add_arguments(self, parser):
        parser.add_argument("--execution-plan-id", type=int, required=True)
        parser.add_argument("--run-id", required=True)

    def handle(self, *args, **options):
        run_id = options["run_id"]
        try:
            summary = execute_execution_plan_artifact(
                options["execution_plan_id"],
                run_id=run_id,
            )
        except Exception as exc:
            mark_run_error(run_id, str(exc))
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{summary.run_id}: {getattr(summary.status, 'value', summary.status)}")
