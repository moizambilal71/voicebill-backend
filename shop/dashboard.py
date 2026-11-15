"""Numbers for the Dashboard page, worked out in the database so it stays fast for shops with many bills."""

import datetime
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Bill, BillItem, Variant

MONEY = ExpressionWrapper(F("sold_price") * F("quantity"), output_field=DecimalField(max_digits=22, decimal_places=3))
ITEM_TOTAL = ExpressionWrapper(F("items__sold_price") * F("items__quantity"), output_field=DecimalField(max_digits=22, decimal_places=3))
LOW_STOCK_AT = 5


def _money(value):
    return float((value or Decimal("0")).quantize(Decimal("0.01")))


@api_view(["GET"])
def dashboard(request):
    """?days=7 or ?days=30. Only confirmed bills count as sales. Days follow the shop's timezone."""
    shop = request.user.shop
    days = 30 if request.query_params.get("days") == "30" else 7
    today = timezone.localdate()
    first = today - datetime.timedelta(days=days - 1)

    confirmed = Bill.objects.filter(shop=shop, status=Bill.Status.CONFIRMED, confirmed_at__date__gte=first)
    items = BillItem.objects.filter(bill__in=confirmed)

    per_day_rows = {
        r["day"]: r["total"]
        for r in items.annotate(day=TruncDate("bill__confirmed_at")).values("day").annotate(total=Sum(MONEY))
    }
    per_day = []
    for i in range(days):
        d = first + datetime.timedelta(days=i)
        per_day.append({"date": d.isoformat(), "total": _money(per_day_rows.get(d))})

    period_sales = sum((v or Decimal("0")) for v in per_day_rows.values())
    count = confirmed.count()
    today_sales = per_day_rows.get(today) or Decimal("0")
    today_bills = confirmed.filter(confirmed_at__date=today).count()

    top_items = [
        {"name": r["item_name"], "qty": float(r["qty"]), "unit": r["unit"], "total": _money(r["total"])}
        for r in items.values("item_name").annotate(qty=Sum("quantity"), unit=Max("unit"), total=Sum(MONEY)).order_by("-total")[:5]
    ]
    top_customers = [
        {"name": r["customer_name"].strip(), "total": _money(r["total"])}
        for r in confirmed.exclude(customer_name__exact="").values("customer_name").annotate(total=Sum(ITEM_TOTAL)).order_by("-total")[:3]
    ]
    recent = [
        {"id": b.id, "customer_name": b.customer_name, "total": _money(b.amount)}
        for b in Bill.objects.filter(shop=shop, status=Bill.Status.CONFIRMED).annotate(amount=Sum(ITEM_TOTAL)).order_by("-confirmed_at", "-id")[:5]
    ]

    low = Variant.objects.filter(product__shop=shop, product__track_stock=True, stock__lte=LOW_STOCK_AT).select_related("product")
    low_stock = [
        {
            "id": v.id,
            "name": v.product.name if v.name == "Standard" or v.name == v.product.name else f"{v.product.name} ({v.name})",
            "stock": float(v.stock),
            "unit": v.product.unit,
        }
        for v in low.order_by("stock", "product__name")[:6]
    ]

    return Response(
        {
            "days": days,
            "today_sales": _money(today_sales),
            "today_bills": today_bills,
            "sales": _money(period_sales),
            "bills": count,
            "average": _money(period_sales / count) if count else 0.0,
            "per_day": per_day,
            "top_items": top_items,
            "top_customers": top_customers,
            "recent": recent,
            "low_stock": low_stock,
            "low_stock_count": low.count(),
        }
    )
