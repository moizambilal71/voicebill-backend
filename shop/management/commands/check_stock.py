import time

from django.core.management.base import BaseCommand

from shop import monitor


class Command(BaseCommand):
    help = "Check stock and create out-of-stock notifications / reminders. Use --loop to keep running."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep checking forever.")
        parser.add_argument("--interval", type=int, default=300, help="Seconds between checks with --loop.")

    def handle(self, *args, **opts):
        while True:
            result = monitor.run_checks()
            self.stdout.write(f"out-of-stock alerts: {result['out_of_stock']}, reminder sent: {result['reminder']}")
            if not opts["loop"]:
                return
            time.sleep(opts["interval"])
