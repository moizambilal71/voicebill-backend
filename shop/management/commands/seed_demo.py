from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from shop.models import Alias, Product, Shop, Variant, create_starter_data

DEMO = [
    # (name, unit, track_stock, [(variant, price, stock)], [(alias, variant_name|None)])
    ("Sugar", "kg", False, [("Standard", 150, 0)], [("cheeni", None), ("chini", None)]),
    ("Daliya", "kg", False,
     [("Regular", 100, 0), ("Medium", 150, 0), ("Premium", 200, 0)],
     [("sasta wala", "Regular"), ("achha wala", "Premium")]),
    ("Washing wear", "piece", True,
     [("A", 2500, 10), ("B", 3000, 10), ("C", 3500, 10), ("D", 4000, 10)], []),
    ("Revach sunblock", "piece", True,
     [("Small", 450, 20), ("Medium", 750, 20), ("Large", 1100, 20)], [("sunblock", None)]),
]


class Command(BaseCommand):
    help = "Load demo products into a shop (safe to run more than once)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Email of the account that owns the shop.")

    def handle(self, *args, **options):
        shop = Shop.objects.filter(owner__email__iexact=options["email"]).first()
        if shop is None:
            raise CommandError("No account with that email. Sign up first.")
        for name, unit, track, variants, aliases in DEMO:
            product, _ = Product.objects.get_or_create(
                shop=shop, name=name, defaults={"unit": unit, "track_stock": track}
            )
            for vname, price, stock in variants:
                Variant.objects.get_or_create(
                    product=product, name=vname,
                    defaults={"price": Decimal(price), "stock": Decimal(stock)},
                )
            for text, vname in aliases:
                variant = product.variants.get(name=vname) if vname else None
                Alias.objects.get_or_create(product=product, variant=variant, text=text)
        self.stdout.write(self.style.SUCCESS(f"Demo data ready: {Product.objects.filter(shop=shop).count()} products."))
