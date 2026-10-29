import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from . import voice as voice_service

from . import importer, monitor
from .models import Alias, Barcode, Bill, BillItem, BillTemplate, Customer, Notification, PriceHistory, Product, ShopSettings, Variant
from .serializers import (
    AliasSerializer,
    BarcodeSerializer,
    BillItemSerializer,
    BillSerializer,
    BillTemplateSerializer,
    CustomerSerializer,
    NotificationSerializer,
    PriceHistorySerializer,
    ProductSerializer,
    ShopSettingsSerializer,
    set_barcodes,
    VariantSerializer,
)


@api_view(["GET"])
def health(request):
    return Response({"status": "ok", "service": "voicebill-backend"})


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.prefetch_related("variants__barcodes", "aliases")
    serializer_class = ProductSerializer

    def list(self, request, *args, **kwargs):
        """Plain list by default. With ?q=, ?limit= or ?offset= it searches (name, category, spoken name,
        barcode) and returns {count, results} so big catalogs can be paged."""
        params = request.query_params
        if not any(k in params for k in ("q", "limit", "offset")):
            return super().list(request, *args, **kwargs)
        qs = self.get_queryset()
        q = (params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(variants__name__icontains=q)
                | Q(aliases__text__icontains=q)
                | Q(variants__barcodes__code__istartswith=q)
            ).distinct()
        try:
            limit = min(max(int(params.get("limit", 50)), 1), 200)
            offset = max(int(params.get("offset", 0)), 0)
        except ValueError:
            return Response({"detail": "limit and offset must be numbers."}, status=400)
        count = qs.count()
        page = qs.order_by("name", "id")[offset : offset + limit]
        return Response({"count": count, "results": ProductSerializer(page, many=True).data})

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_file(self, request):
        """Bulk import from CSV/Excel. Send dry_run=1 first to see what would happen, then without it to save."""
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Attach a file in the 'file' field."}, status=400)
        try:
            rows = importer.read_rows(upload)
        except importer.ImportFileError as exc:
            return Response({"detail": str(exc)}, status=400)
        dry = str(request.data.get("dry_run", "")).lower() in ("1", "true", "yes")
        return Response(importer.run(rows, apply=not dry))

    @action(detail=True, methods=["post"])
    def add_variant(self, request, pk=None):
        product = self.get_object()
        serializer = VariantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.pop("id", None)
        codes = data.pop("barcodes", [])
        with transaction.atomic():
            variant = Variant.objects.create(product=product, **data)
            set_barcodes(variant, codes)
        return Response(VariantSerializer(variant).data, status=status.HTTP_201_CREATED)

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

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != "list":
            return qs
        params = self.request.query_params
        q = (params.get("q") or "").strip()
        if q:
            qs = qs.filter(customer_name__icontains=q)
        # Dates are local (Asia/Karachi) days, YYYY-MM-DD. "date" = one day; "date_from"/"date_to" = a range.
        for key, lookup in (("date", "created_at__date"), ("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
            value = params.get(key)
            if value:
                try:
                    qs = qs.filter(**{lookup: datetime.date.fromisoformat(value)})
                except ValueError:
                    pass
        return qs

    def _fresh(self, bill):
        """Re-read the bill so the response reflects the latest items."""
        return Response(BillSerializer(self.get_queryset().get(pk=bill.pk)).data)

    def _editable(self, bill):
        if bill.status != Bill.Status.DRAFT:
            return Response({"detail": "Only draft bills can be edited."}, status=400)
        return None

    @staticmethod
    def _wants_ignore_stock(request):
        return str(request.data.get("ignore_stock", "")).lower() in ("1", "true", "yes")

    @staticmethod
    def _stock_problem(variant, wanted):
        """Message when `wanted` units of a tracked variant are more than the stock, else None."""
        if variant is None or not variant.product.track_stock or wanted <= variant.stock:
            return None
        name = f"{variant.product.name} ({variant.name})" if variant.product.variants.count() > 1 else variant.product.name
        if variant.stock <= 0:
            return f"{name}: Out of stock hai. Kindly add the item in your stock."
        return f"{name}: sirf {variant.stock.normalize():f} {variant.product.unit} stock mein hai. Kindly add the item in your stock."

    def _wanted_on_bill(self, bill, variant, quantity, exclude_item_id=None):
        others = bill.items.filter(variant=variant)
        if exclude_item_id is not None:
            others = others.exclude(pk=exclude_item_id)
        return sum((i.quantity for i in others), Decimal("0")) + quantity

    @action(detail=True, methods=["post"])
    def add_item(self, request, pk=None):
        bill = self.get_object()
        if (err := self._editable(bill)) is not None:
            return err
        serializer = BillItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        variant = serializer.validated_data.get("variant")
        if not self._wants_ignore_stock(request) and variant is not None:
            problem = self._stock_problem(variant, self._wanted_on_bill(bill, variant, serializer.validated_data["quantity"]))
            if problem:
                return Response({"detail": problem, "code": "out_of_stock"}, status=400)
        serializer.save(bill=bill)
        response = self._fresh(bill)
        response.status_code = status.HTTP_201_CREATED
        return response

    @action(detail=True, methods=["post"])
    def scan(self, request, pk=None):
        """A barcode / QR was scanned: add the item (or +1 if it is already on the bill)."""
        bill = self.get_object()
        if (err := self._editable(bill)) is not None:
            return err
        code = str(request.data.get("code") or "").strip()
        if not code:
            return Response({"detail": "Nothing was scanned."}, status=400)
        barcode = Barcode.objects.select_related("variant__product").filter(code=code).first()
        if barcode is None:
            return Response(
                {"detail": "This code is not in your catalog yet.", "code": "unknown_barcode", "barcode": code}, status=404
            )
        variant = barcode.variant
        if not self._wants_ignore_stock(request):
            problem = self._stock_problem(variant, self._wanted_on_bill(bill, variant, Decimal("1")))
            if problem:
                return Response({"detail": problem, "code": "out_of_stock"}, status=400)
        line = bill.items.filter(variant=variant, price_source=BillItem.PriceSource.CATALOG).first()
        if line:
            line.quantity = line.quantity + 1
            line.save(update_fields=["quantity"])
        else:
            serializer = BillItemSerializer(data={"variant": variant.id, "quantity": 1})
            serializer.is_valid(raise_exception=True)
            line = serializer.save(bill=bill)
        fresh = self._fresh(bill).data
        return Response({"bill": fresh, "item_name": line.item_name, "quantity": line.quantity})

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
        variant = serializer.validated_data.get("variant")
        if not self._wants_ignore_stock(request) and variant is not None:
            wanted = self._wanted_on_bill(bill, variant, serializer.validated_data["quantity"], exclude_item_id=item.pk)
            if problem := self._stock_problem(variant, wanted):
                return Response({"detail": problem, "code": "out_of_stock"}, status=400)
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

        ignore = self._wants_ignore_stock(request)
        with transaction.atomic():
            if not ignore:
                totals = {}
                for item in bill.items.select_related("variant__product"):
                    if item.variant:
                        totals[item.variant] = totals.get(item.variant, Decimal("0")) + item.quantity
                for variant, wanted in totals.items():
                    if problem := self._stock_problem(variant, wanted):
                        return Response({"detail": problem, "code": "out_of_stock"}, status=400)
            for item in bill.items.select_related("variant__product"):
                v = item.variant
                if v and v.product.track_stock:
                    v.refresh_from_db(fields=["stock"])
                    taken = min(max(v.stock, Decimal("0")), item.quantity)  # stock never goes below 0
                    v.stock = v.stock - taken
                    v.save(update_fields=["stock"])
                    item.stock_deducted = taken
                    item.save(update_fields=["stock_deducted"])
            touched = {i.variant for i in bill.items.select_related("variant__product") if i.variant}
            bill.status = Bill.Status.CONFIRMED
            bill.confirmed_at = timezone.now()
            bill.save(update_fields=["status", "confirmed_at"])
        for variant in touched:
            monitor.sync_variant(variant)  # raises the "out of stock" notification right away
        return Response(BillSerializer(bill).data)

    @staticmethod
    def _restore_stock(bill):
        for item in bill.items.select_related("variant__product"):
            v = item.variant
            if v and item.stock_deducted:
                v.stock = v.stock + item.stock_deducted
                v.save(update_fields=["stock"])
                item.stock_deducted = Decimal("0")
                item.save(update_fields=["stock_deducted"])
                monitor.sync_variant(v)  # restocked -> clear the alert

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        """Let the shopkeeper edit a confirmed bill again (before printing). Stock is given back until re-confirmed."""
        bill = self.get_object()
        if bill.status != Bill.Status.CONFIRMED:
            return Response({"detail": "Only confirmed bills can be reopened."}, status=400)
        with transaction.atomic():
            self._restore_stock(bill)
            bill.status = Bill.Status.DRAFT
            bill.confirmed_at = None
            bill.save(update_fields=["status", "confirmed_at"])
        return self._fresh(bill)

    def perform_destroy(self, instance):
        with transaction.atomic():
            if instance.status == Bill.Status.CONFIRMED:
                self._restore_stock(instance)  # a deleted sale never happened
            instance.delete()

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
        return Response(voice_service.interpret(text, bill, request.data.get("pending")))
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


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """The bell icon. `list` returns {"unread": n, "items": [...]} (latest 50)."""

    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer

    def list(self, request, *args, **kwargs):
        monitor.sync_all()  # stock may have been edited by hand; keep the bell truthful
        items = Notification.objects.all()[:50]
        return Response(
            {
                "unread": Notification.objects.filter(read_at__isnull=True).count(),
                "items": NotificationSerializer(items, many=True).data,
            }
        )

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        n = self.get_object()
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=["read_at"])
        return Response(NotificationSerializer(n).data)

    @action(detail=False, methods=["post"])
    def read_all(self, request):
        Notification.objects.filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"unread": 0})


class BarcodeViewSet(mixins.CreateModelMixin, mixins.DestroyModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """Codes linked to categories. `lookup/?code=` tells whether a scanned code is already known."""

    queryset = Barcode.objects.select_related("variant__product")
    serializer_class = BarcodeSerializer

    @action(detail=False, methods=["get"])
    def lookup(self, request):
        code = (request.query_params.get("code") or "").strip()
        b = self.get_queryset().filter(code=code).first() if code else None
        if b is None:
            return Response({"found": False, "code": code})
        v = b.variant
        return Response(
            {
                "found": True,
                "code": code,
                "variant": v.id,
                "variant_name": v.name,
                "product": v.product_id,
                "product_name": v.product.name,
                "price": v.price,
                "stock": v.stock,
                "track_stock": v.product.track_stock,
                "unit": v.product.unit,
            }
        )
