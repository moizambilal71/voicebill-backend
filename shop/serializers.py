from django.db import transaction
from rest_framework import serializers

from .models import Alias, Bill, BillItem, Customer, PriceHistory, Product, Variant


class AliasSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alias
        fields = ["id", "text", "variant"]


class VariantSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = Variant
        fields = ["id", "name", "price", "stock"]


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
        return value

    @transaction.atomic
    def create(self, validated_data):
        variants = validated_data.pop("variants")
        product = Product.objects.create(**validated_data)
        for v in variants:
            v.pop("id", None)
            Variant.objects.create(product=product, **v)
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        # Variant prices are changed via the update-price endpoint so history is kept.
        validated_data.pop("variants", None)
        return super().update(instance, validated_data)


class PriceHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceHistory
        fields = ["id", "variant", "old_price", "new_price", "changed_at"]


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "created_at"]


class BillItemSerializer(serializers.ModelSerializer):
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


class BillSerializer(serializers.ModelSerializer):
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
