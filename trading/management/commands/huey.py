from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Alias for 'djangohuey' (Huey consumer)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue",
            default="default",
            help="Queue name (default: default)",
        )

    def handle(self, *args, **options):
        from django.core.management import call_command

        queue = options.get("queue") or "default"
        call_command("djangohuey", queue=queue)
