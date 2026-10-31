from django.core.management.base import BaseCommand, CommandError

from shop.models import Shop


class Command(BaseCommand):
    help = "Change a shop's plan by the owner's email, e.g. after a payment arrives:  set_plan name@example.com pro"

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("plan", choices=[c for c, _ in Shop.Plan.choices])

    def handle(self, *args, **opts):
        shop = Shop.objects.filter(owner__email__iexact=opts["email"]).first()
        if shop is None:
            raise CommandError("No shop belongs to that email.")
        shop.plan = opts["plan"]
        shop.save(update_fields=["plan"])
        self.stdout.write(self.style.SUCCESS(f"{opts['email']} is now on the {shop.get_plan_display()} plan."))
