from django.apps import AppConfig


class TradingConfig(AppConfig):
    name = 'trading'

    def ready(self):
        from . import tasks  # noqa: F401
