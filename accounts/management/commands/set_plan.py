from django.core.management.base import BaseCommand, CommandError

from shop.models import Shop


class Command(BaseCommand):
    help = "Change a shop's plan by the owner's email, e.g. after a payment arrives:  set_plan name@example.com pro"

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("plan", choices=[c for c, _ in Shop.Plan.choices])
        parser.add_argument("--days", type=int, help="Pro lasts this many days (default: PRO_DAYS, 30).")
        parser.add_argument("--forever", action="store_true", help="Pro never ends.")

    def handle(self, *args, **opts):
        shop = Shop.objects.filter(owner__email__iexact=opts["email"]).first()
        if shop is None:
            raise CommandError("No shop belongs to that email.")
        from accounts import plans

        if opts["plan"] == "pro" and not opts["forever"]:
            plans.grant_pro(shop, opts["days"])
        else:
            shop.plan = opts["plan"]
            shop.pro_until = None
            shop.save(update_fields=["plan", "pro_until"])
        until = f" (until {shop.pro_until:%d %b %Y})" if shop.pro_until else ""
        self.stdout.write(self.style.SUCCESS(f"{opts['email']} is now on the {shop.get_plan_display()} plan{until}."))
