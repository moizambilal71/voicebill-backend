from decimal import Decimal

from django.db import models, transaction


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
    # True while an "out of stock" notification is active for this category (reset when it is restocked).
    out_of_stock_notified = models.BooleanField(default=False)

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
    # How much stock this line really took when the bill was confirmed (stock never goes below 0).
    stock_deducted = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))

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


class ShopSettings(models.Model):
    """One row for the whole shop: bill language and shop details printed on bills."""

    class Language(models.TextChoices):
        ENGLISH = "en", "English"
        URDU = "ur", "Urdu"
        ROMAN = "roman", "Roman Urdu"
        BOTH = "both", "Urdu + English"

    language = models.CharField(max_length=10, choices=Language.choices, default=Language.ENGLISH)
    shop_name = models.CharField(max_length=200, blank=True)
    shop_address = models.CharField(max_length=300, blank=True)
    shop_phone = models.CharField(max_length=50, blank=True)
    # How often the "you still have out-of-stock items" reminder is sent.
    reminder_days = models.PositiveSmallIntegerField(default=3)

    class Meta:
        verbose_name_plural = "shop settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.shop_name or "Shop settings"


class BillTemplate(models.Model):
    """How a printed bill looks. One built-in default plus any the shopkeeper adds.

    Exactly one template is selected at any time; if none is chosen the default is.
    """

    class Paper(models.TextChoices):
        MM58 = "58mm", "58 mm thermal roll"
        MM80 = "80mm", "80 mm thermal roll"
        A4 = "a4", "A4 sheet"

    class Layout(models.TextChoices):
        RECEIPT = "receipt", "Receipt"
        INVOICE = "invoice", "Invoice table"
        MINIMAL = "minimal", "Minimal"

    class FontSize(models.TextChoices):
        SMALL = "small", "Small"
        MEDIUM = "medium", "Medium"
        LARGE = "large", "Large"

    name = models.CharField(max_length=80)
    is_default = models.BooleanField(default=False)
    is_selected = models.BooleanField(default=False)
    paper_size = models.CharField(max_length=10, choices=Paper.choices, default=Paper.MM80)
    layout = models.CharField(max_length=10, choices=Layout.choices, default=Layout.RECEIPT)
    font_size = models.CharField(max_length=10, choices=FontSize.choices, default=FontSize.MEDIUM)
    header_text = models.CharField(max_length=200, blank=True)
    footer_text = models.CharField(max_length=200, blank=True)
    show_customer = models.BooleanField(default=True)
    show_date = models.BooleanField(default=True)
    show_unit = models.BooleanField(default=True)
    show_discount = models.BooleanField(default=True)
    show_item_numbers = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "created_at"]

    def __str__(self):
        return self.name

    @classmethod
    def ensure_default(cls):
        default = cls.objects.filter(is_default=True).first()
        if default is None:
            default = cls.objects.create(name="Default receipt", is_default=True)
        if not cls.objects.filter(is_selected=True).exists():
            default.is_selected = True
            default.save(update_fields=["is_selected"])
        return default

    def select(self):
        with transaction.atomic():
            BillTemplate.objects.exclude(pk=self.pk).update(is_selected=False)
            self.is_selected = True
            self.save(update_fields=["is_selected"])


class Notification(models.Model):
    """A message for the shopkeeper, shown under the bell icon."""

    class Kind(models.TextChoices):
        OUT_OF_STOCK = "out_of_stock", "Out of stock"
        REMINDER = "reminder", "Stock reminder"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=200)
    message = models.CharField(max_length=500, blank=True)
    variant = models.ForeignKey(
        Variant, related_name="notifications", on_delete=models.CASCADE, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.title
