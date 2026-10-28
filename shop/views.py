from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from . import voice as voice_service

from .models import Alias, Bill, BillItem, BillTemplate, Customer, PriceHistory, Product, ShopSettings, Variant
from .serializers import (
    AliasSerializer,
    BillItemSerializer,
    BillSerializer,
    BillTemplateSerializer,
    CustomerSerializer,
    PriceHistorySerializer,
    ProductSerializer,
    ShopSettingsSerializer,
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


MAX_AUDIO_BYTES = 10 * 1024 * 1024


def _voice_error(exc):
    if isinstance(exc, voice_service.VoiceError):
        return Response({"detail": str(exc)}, status=exc.status)
    return Response(
        {"detail": f"Voice service error: {exc.__class__.__name__}. Check the API key, model names and internet."},
        status=502,
    )


@api_view(["POST"])
@parser_classes([MultiPartParser])
def voice_transcribe(request):
    """Audio file -> text. Form field: audio."""
    audio = request.FILES.get("audio")
    if audio is None:
        return Response({"detail": "Send the recording in the 'audio' field."}, status=400)
    if audio.size > MAX_AUDIO_BYTES:
        return Response({"detail": "Recording is too long. Keep commands short."}, status=400)
    if audio.size < 1000:
        return Response({"detail": "Recording was empty. Hold the mic a little longer."}, status=400)
    try:
        text = voice_service.transcribe(audio.name or "speech.webm", audio.read(), audio.content_type)
    except Exception as exc:  # noqa: BLE001 - surface any provider error as a clean message
        return _voice_error(exc)
    return Response({"text": text})


@api_view(["POST"])
def voice_interpret(request):
    """Text (+ optional bill_id) -> proposed actions. Nothing is saved here."""
    text = request.data.get("text", "")
    bill = None
    if request.data.get("bill_id"):
        bill = Bill.objects.prefetch_related("items").filter(pk=request.data["bill_id"]).first()
    try:
        return Response(voice_service.interpret(text, bill))
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


@api_view(["GET", "PUT", "PATCH"])
def shop_settings(request):
    """Bill language + shop details (one record for the whole shop)."""
    obj = ShopSettings.load()
    if request.method == "GET":
        return Response(ShopSettingsSerializer(obj).data)
    serializer = ShopSettingsSerializer(obj, data=request.data, partial=request.method == "PATCH")
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


class BillTemplateViewSet(viewsets.ModelViewSet):
    """Built-in default + templates the shopkeeper adds. Exactly one is selected."""

    queryset = BillTemplate.objects.all()
    serializer_class = BillTemplateSerializer

    def list(self, request, *args, **kwargs):
        BillTemplate.ensure_default()
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        BillTemplate.ensure_default()
        serializer.save(is_default=False, is_selected=False)

    def update(self, request, *args, **kwargs):
        if self.get_object().is_default:
            return Response(
                {"detail": "The default template cannot be edited. Add a new template instead."}, status=400
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        if template.is_default:
            return Response({"detail": "The default template cannot be deleted."}, status=400)
        template.delete()
        BillTemplate.ensure_default()  # if the deleted one was selected, fall back to default
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def select(self, request, pk=None):
        template = self.get_object()
        template.select()
        return Response(BillTemplateSerializer(template).data)
