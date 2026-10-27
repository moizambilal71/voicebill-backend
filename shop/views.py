from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import Alias, Bill, BillItem, Customer, PriceHistory, Product, Variant
from .serializers import (
    AliasSerializer,
    BillItemSerializer,
    BillSerializer,
    CustomerSerializer,
    PriceHistorySerializer,
    ProductSerializer,
    VariantSerializer,
)


@api_view(["GET"])
def health(request):
    return Response({"status": "ok", "service": "voicebill-backend"})


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.prefetch_related("variants", "aliases")
    serializer_class = ProductSerializer

    @action(detail=True, methods=["post"])
    def add_variant(self, request, pk=None):
        product = self.get_object()
        serializer = VariantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.validated_data.pop("id", None)
        serializer.save(product=product)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def add_alias(self, request, pk=None):
        product = self.get_object()
        serializer = AliasSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        variant = serializer.validated_data.get("variant")
        if variant and variant.product_id != product.id:
            return Response({"variant": "Variant does not belong to this product."}, status=400)
        serializer.save(product=product)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class VariantViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Variant.objects.select_related("product")
    serializer_class = VariantSerializer

    @action(detail=True, methods=["post"])
    def update_price(self, request, pk=None):
        """Permanent catalog price change. Only called after the shopkeeper confirms."""
        variant = self.get_object()
        try:
            new_price = Decimal(str(request.data.get("price")))
        except (InvalidOperation, TypeError):
            return Response({"price": "A valid price is required."}, status=400)
        if new_price < 0:
            return Response({"price": "Price cannot be negative."}, status=400)

        with transaction.atomic():
            PriceHistory.objects.create(
                variant=variant, old_price=variant.price, new_price=new_price
            )
            variant.price = new_price
            variant.save(update_fields=["price"])
        return Response(VariantSerializer(variant).data)

    @action(detail=True, methods=["get"])
    def price_history(self, request, pk=None):
        variant = self.get_object()
        return Response(PriceHistorySerializer(variant.price_history.all(), many=True).data)


class CustomerViewSet(viewsets.ModelViewSet):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer


class BillViewSet(viewsets.ModelViewSet):
    queryset = Bill.objects.prefetch_related("items", "items__variant")
    serializer_class = BillSerializer

    def _fresh(self, bill):
        """Re-read the bill so the response reflects the latest items."""
        return Response(BillSerializer(self.get_queryset().get(pk=bill.pk)).data)

    def _editable(self, bill):
        if bill.status != Bill.Status.DRAFT:
            return Response({"detail": "Only draft bills can be edited."}, status=400)
        return None

    @action(detail=True, methods=["post"])
    def add_item(self, request, pk=None):
        bill = self.get_object()
        if (err := self._editable(bill)) is not None:
            return err
        serializer = BillItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(bill=bill)
        response = self._fresh(bill)
        response.status_code = status.HTTP_201_CREATED
        return response

    @action(detail=True, methods=["patch"], url_path=r"items/(?P<item_id>\d+)")
    def edit_item(self, request, pk=None, item_id=None):
        bill = self.get_object()
        if (err := self._editable(bill)) is not None:
            return err
        item = get_object_or_404(BillItem, pk=item_id, bill=bill)
        data = {
            "variant": item.variant_id,
            "item_name": item.item_name,
            "quantity": item.quantity,
            "unit": item.unit,
        }
        if item.price_source == BillItem.PriceSource.SPOKEN:
            data["sold_price"] = item.sold_price
        data.update(request.data)
        serializer = BillItemSerializer(item, data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self._fresh(bill)

    @edit_item.mapping.delete
    def remove_item(self, request, pk=None, item_id=None):
        bill = self.get_object()
        if (err := self._editable(bill)) is not None:
            return err
        get_object_or_404(BillItem, pk=item_id, bill=bill).delete()
        return self._fresh(bill)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Finalize a bill and reduce stock for tracked products."""
        bill = self.get_object()
        if (err := self._editable(bill)) is not None:
            return err
        if not bill.items.exists():
            return Response({"detail": "Cannot confirm an empty bill."}, status=400)

        with transaction.atomic():
            for item in bill.items.select_related("variant__product"):
                v = item.variant
                if v and v.product.track_stock:
                    v.stock = v.stock - item.quantity
                    v.save(update_fields=["stock"])
            bill.status = Bill.Status.CONFIRMED
            bill.confirmed_at = timezone.now()
            bill.save(update_fields=["status", "confirmed_at"])
        return Response(BillSerializer(bill).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        bill = self.get_object()
        if (err := self._editable(bill)) is not None:
            return err
        bill.status = Bill.Status.CANCELLED
        bill.save(update_fields=["status"])
        return Response(BillSerializer(bill).data)
