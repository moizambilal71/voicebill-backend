import os
import sys

from django.apps import AppConfig


class ShopConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "shop"

    def ready(self):
        # Start the stock monitor with `runserver` (only in the process that serves requests, not in the
        # autoreloader's parent) unless STOCK_MONITOR=0. Tests, migrate and other commands never start it.
        if os.environ.get("STOCK_MONITOR", "1") == "0" or "runserver" not in sys.argv:
            return
        if os.environ.get("RUN_MAIN") != "true" and "--noreload" not in sys.argv:
            return
        from . import monitor

        monitor.start_background(int(os.environ.get("STOCK_CHECK_INTERVAL_SECONDS", "300")))
