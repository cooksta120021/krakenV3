import sys

from django_huey.management.commands.djangohuey import Command as BaseCommand


class Command(BaseCommand):
    def handle(self, *args, **options):
        import multiprocessing

        if sys.platform.startswith("win"):
            try:
                multiprocessing.set_start_method("spawn", force=True)
            except Exception:
                pass

        return super().handle(*args, **options)
