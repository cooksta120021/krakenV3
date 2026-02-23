from django.core.management.base import BaseCommand

from trading.services.executor import run_active_sleeves


class Command(BaseCommand):
    help = "Run one-off pass over all active sleeve strategies (can be looped externally)."

    def handle(self, *args, **options):
        run_active_sleeves()
        self.stdout.write(self.style.SUCCESS("Completed active sleeve run"))
