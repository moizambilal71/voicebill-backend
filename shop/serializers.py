from django.db import IntegrityError, transaction
from rest_framework import serializers

from .models import (
    Alias, Barcode, Bill, BillItem, BillTemplate, Customer, Notification, PriceHistory, Product, ShopSettings, Variant,
)


class ShopScopedMixin:
    """Related ids (variant, customer...) may only point at the signed-in shop's own rows.

    The view passes `context={"shop": shop}`. Without a shop the choices are empty, so a missing context can
    never leak another shop's data. Set `scoped = {"field": lambda shop: queryset}`.
    """

    scoped = {}

    def get_fields(self):
        fields = super().get_fields()
        shop = self.context.get("shop")
        for name, make in self.scoped.items():
            field = fields.get(name)
            if field is not None and not field.read_only:
                field.queryset = make(shop) if shop is not None else field.queryset.none()
        return fields


def _variants(shop):
    return Variant.objects.filter(product__shop=shop)


class AliasSerializer(ShopScopedMixin, serializers.ModelSerializer):
    scoped = {"variant": _variants}

    class Meta:
        model = Alias
        fields = ["id", "text", "variant"]


def clean_codes(codes):
    """Strip, drop empties and duplicates, keep order."""
    seen, out = set(), []
    for c in codes or []:
        c = str(c).strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def set_barcodes(variant, codes):
    """Make `codes` the complete list of codes for this category. A code used elsewhere in the shop is refused."""
    codes = clean_codes(codes)
    shop_id = variant.product.shop_id
    for c in codes:
        other = Barcode.objects.filter(shop_id=shop_id, code=c).exclude(variant=variant).select_related("variant__product").first()
        if other:
            raise serializers.ValidationError(
                {"barcodes": f"Code {c} is already linked to {other.variant.product.name} ({other.variant.name})."}
            )
    variant.barcodes.exclude(code__in=codes).delete()
    have = set(variant.barcodes.values_list("code", flat=True))
    for c in codes:
        if c not in have:
            Barcode.objects.create(shop_id=shop_id, variant=variant, code=c)


class VariantSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    barcodes = serializers.ListField(child=serializers.CharField(max_length=128), required=False, write_only=True)

    class Meta:
        model = Variant
        fields = ["id", "name", "price", "stock", "barcodes"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["barcodes"] = [b.code for b in instance.barcodes.all()]
        return data


class BarcodeSerializer(ShopScopedMixin, serializers.ModelSerializer):
    scoped = {"variant": _variants}

    class Meta:
        model = Barcode
        fields = ["id", "code", "variant"]
        extra_kwargs = {"code": {"validators": []}}

    def validate_code(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Code cannot be empty.")
        other = Barcode.objects.filter(shop=self.context["shop"], code=value).select_related("variant__product").first()
        if other:
            raise serializers.ValidationError(
                f"Already linked to {other.variant.product.name} ({other.variant.name})."
            )
        return value


class ProductSerializer(serializers.ModelSerializer):
    variants = VariantSerializer(many=True)
    aliases = AliasSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = ["id", "name", "unit", "track_stock", "variants", "aliases", "created_at"]

    def validate_variants(self, value):
        if not value:
            raise serializers.ValidationError("A product needs at least one variant.")
        names = [v.get("name", "Standard") for v in value]
        if len(names) != len(set(names)):
            raise serializers.ValidationError("Variant names must be unique.")
        all_codes = [c for v in value for c in clean_codes(v.get("barcodes"))]
        if len(all_codes) != len(set(all_codes)):
            raise serializers.ValidationError("The same code is used twice on this product.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        variants = validated_data.pop("variants")
        product = Product.objects.create(**validated_data)
        for v in variants:
            v.pop("id", None)
            codes = v.pop("barcodes", [])
            variant = Variant.objects.create(product=product, **v)
            set_barcodes(variant, codes)
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        """Edit a product and its categories in one go.

        Variants sent with an id are updated, without an id are added, and existing ones that are
        left out are removed (old bills keep their item name and price). A changed price is logged.
        """
        variants = validated_data.pop("variants", None)
        instance = super().update(instance, validated_data)
        if variants is None:
            return instance

        existing = {v.id: v for v in instance.variants.all()}
        sent_ids = {d["id"] for d in variants if d.get("id") is not None}
        unknown = sent_ids - existing.keys()
        if unknown:
            raise serializers.ValidationError({"variants": "A category does not belong to this product."})

        try:
            for vid, v in existing.items():
                if vid not in sent_ids:
                    v.delete()
            for data in variants:
                vid = data.pop("id", None)
                codes = data.pop("barcodes", None)
                if vid is None:
                    new = Variant.objects.create(product=instance, **data)
                    set_barcodes(new, codes)
                    continue
                v = existing[vid]
                if "price" in data and data["price"] != v.price:
                    PriceHistory.objects.create(variant=v, old_price=v.price, new_price=data["price"])
                for field, value in data.items():
                    setattr(v, field, value)
                v.save()
                if codes is not None:  # not sent = leave the codes alone
                    set_barcodes(v, codes)
        except IntegrityError:
            raise serializers.ValidationError({"variants": "Category names must be different from each other."})
        return instance


class PriceHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceHistory
        fields = ["id", "variant", "old_price", "new_price", "changed_at"]


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "created_at"]


class BillItemSerializer(ShopScopedMixin, serializers.ModelSerializer):
    scoped = {"variant": _variants}
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    discount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    sold_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    item_name = serializers.CharField(required=False)

    class Meta:
        model = BillItem
        fields = [
            "id", "variant", "item_name", "quantity", "unit",
            "list_price", "sold_price", "price_source", "line_total", "discount",
        ]
        read_only_fields = ["list_price", "price_source"]

    def validate(self, attrs):
        """Apply the master rule.

        - sold_price given (spoken)  -> use it; link to variant only for reporting.
        - sold_price missing         -> must have a variant; use catalog price.
        """
        variant = attrs.get("variant")
        spoken_price = attrs.get("sold_price")

        if variant:
            attrs.setdefault("item_name", str(variant))
            attrs.setdefault("unit", variant.product.unit)
            attrs["list_price"] = variant.price

        if spoken_price is not None:
            attrs["price_source"] = BillItem.PriceSource.SPOKEN
        elif variant:
            attrs["sold_price"] = variant.price
            attrs["price_source"] = BillItem.PriceSource.CATALOG
        else:
            raise serializers.ValidationError(
                {"sold_price": "Item is not in the catalog. Kitne ka hai? (price required)"}
            )

        if not attrs.get("item_name"):
            raise serializers.ValidationError({"item_name": "Item name is required."})
        return attrs


class BillSerializer(ShopScopedMixin, serializers.ModelSerializer):
    scoped = {"customer": lambda shop: Customer.objects.filter(shop=shop)}
    items = BillItemSerializer(many=True, read_only=True)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total_discount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Bill
        fields = [
            "id", "customer", "customer_name", "status", "items",
            "total", "total_discount", "created_at", "confirmed_at",
        ]
        read_only_fields = ["status", "confirmed_at"]


class ShopSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShopSettings
        fields = ["language", "shop_name", "shop_address", "shop_phone", "reminder_days", "dark_mode"]
        extra_kwargs = {"reminder_days": {"min_value": 1, "max_value": 30}}


class BillTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillTemplate
        fields = [
            "id", "name", "is_default", "is_selected", "paper_size", "layout", "font_size",
            "header_text", "footer_text", "show_customer", "show_date", "show_unit",
            "show_discount", "show_item_numbers",
        ]
        read_only_fields = ["is_default", "is_selected"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Template needs a name.")
        return value


class NotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "message", "variant", "created_at", "is_read"]

    def get_is_read(self, obj):
        return obj.read_at is not None
