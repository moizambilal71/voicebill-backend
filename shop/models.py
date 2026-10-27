from decimal import Decimal

from django.db import models


class Unit(models.TextChoices):
    KG = "kg", "Kilogram"
    GRAM = "g", "Gram"
    LITER = "l", "Liter"
    PIECE = "piece", "Piece"
    METER = "meter", "Meter"
    DOZEN = "dozen", "Dozen"
    PACK = "pack", "Pack"


class Product(models.Model):
    """A catalog item, e.g. 'Daliya' or 'Washing wear'.

    Every product has at least one Variant. A product without categories
    gets a single variant named 'Standard'.
    """

    name = models.CharField(max_length=200)
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.PIECE)
    track_stock = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Variant(models.Model):
    """A category / grade of a product, e.g. Daliya 'Regular' at Rs 100/kg."""

    product = models.ForeignKey(Product, related_name="variants", on_delete=models.CASCADE)
    name = models.CharField(max_length=100, default="Standard")
    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))

    class Meta:
        ordering = ["product__name", "price"]
        unique_together = [("product", "name")]

    def __str__(self):
        return f"{self.product.name} ({self.name})"


class Alias(models.Model):
    """Spoken names for a product or a specific variant.

    e.g. 'cheeni', 'chini' -> Sugar; 'sasta wala' -> Daliya (Regular).
    """

    product = models.ForeignKey(Product, related_name="aliases", on_delete=models.CASCADE)
    variant = models.ForeignKey(
        Variant, related_name="aliases", on_delete=models.CASCADE, null=True, blank=True
    )
    text = models.CharField(max_length=100)

    class Meta:
        verbose_name_plural = "aliases"

    def __str__(self):
        return self.text


class PriceHistory(models.Model):
    """Log of permanent catalog price changes (never bill-only overrides)."""

    variant = models.ForeignKey(Variant, related_name="price_history", on_delete=models.CASCADE)
    old_price = models.DecimalField(max_digits=12, decimal_places=2)
    new_price = models.DecimalField(max_digits=12, decimal_places=2)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]
        verbose_name_plural = "price history"


class Customer(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Bill(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    customer = models.ForeignKey(
        Customer, related_name="bills", on_delete=models.SET_NULL, null=True, blank=True
    )
    customer_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Bill #{self.pk} - {self.customer_name or 'Walk-in'}"

    @property
    def total(self):
        return sum((item.line_total for item in self.items.all()), Decimal("0"))

    @property
    def total_discount(self):
        return sum((item.discount for item in self.items.all()), Decimal("0"))


class BillItem(models.Model):
    """One line on a bill.

    Master rule:
      - price_source = 'catalog' -> sold_price came from the variant's price
      - price_source = 'spoken'  -> shopkeeper said a price; catalog is untouched
    For open-price items (not in catalog), variant is null and item_name holds the name.
    """

    class PriceSource(models.TextChoices):
        CATALOG = "catalog", "Catalog price"
        SPOKEN = "spoken", "Spoken price"

    bill = models.ForeignKey(Bill, related_name="items", on_delete=models.CASCADE)
    variant = models.ForeignKey(
        Variant, related_name="bill_items", on_delete=models.SET_NULL, null=True, blank=True
    )
    item_name = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("1"))
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.PIECE)
    list_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sold_price = models.DecimalField(max_digits=12, decimal_places=2)
    price_source = models.CharField(
        max_length=10, choices=PriceSource.choices, default=PriceSource.CATALOG
    )

    def __str__(self):
        return f"{self.item_name} x {self.quantity}"

    @property
    def line_total(self):
        return (self.sold_price * self.quantity).quantize(Decimal("0.01"))

    @property
    def discount(self):
        if self.list_price is None or self.sold_price >= self.list_price:
            return Decimal("0")
        return ((self.list_price - self.sold_price) * self.quantity).quantize(Decimal("0.01"))
