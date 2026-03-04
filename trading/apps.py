from django.apps import AppConfig
from django.conf import settings
from django.core.cache import cache
import sys
import time


class TradingConfig(AppConfig):
    name = 'trading'

    def ready(self):
        from . import tasks

        enabled = bool(getattr(settings, "STARTUP_ORDERLOG_BACKFILL_ENABLED", True))
        if not enabled:
            return

        argv = [str(a or "") for a in (sys.argv or [])]
        if not any(a in {"runserver", "huey"} for a in argv):
            return

        min_interval_s = int(getattr(settings, "STARTUP_ORDERLOG_BACKFILL_MIN_INTERVAL_S", 300) or 300)
        k = "startup_backfill:orderlog_exec_fields"
        try:
            acquired = bool(cache.add(k, float(time.time()), timeout=min_interval_s))
        except Exception:
            acquired = False
        if not acquired:
            return

        try:
            tasks.startup_backfill_orderlog_exec_fields.delay()
        except Exception:
            pass
