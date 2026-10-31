"""Stock monitoring: creates the notifications shown under the bell icon.

Three things feed the bell:
  1. sync_variant / sync_all  - raise an "out of stock" notification the moment a tracked item hits 0
     (called when a bill is confirmed, and every time the app asks for its notifications) and clear it
     when the item is restocked.
  2. run_checks               - the periodic job: sync_all + the "you still have out-of-stock items"
     reminder, sent every `ShopSettings.reminder_days` days.
  3. start_background         - a small thread that runs run_checks every few minutes while the server is up.
     For production without runserver, run `python manage.py check_stock` from cron / Task Scheduler instead.
"""

import logging
import threading
import time
from datetime import timedelta

from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from .models import Notification, Shop, ShopSettings, Variant

log = logging.getLogger(__name__)

_started = False


def label(variant):
    product = variant.product
    if product.variants.count() > 1:
        return f"{product.name} ({variant.name})"
    return product.name


def sync_variant(variant):
    """Bring one category's notification state in line with its stock. Returns True if one was created."""
    variant.refresh_from_db()
    product = variant.product
    out = product.track_stock and variant.stock <= 0
    if out and not variant.out_of_stock_notified:
        Notification.objects.create(
            shop=product.shop,
            kind=Notification.Kind.OUT_OF_STOCK,
            title=f"{label(variant)} is out of stock",
            message="This item is out of stock. Please add stock for it.",
            variant=variant,
        )
        Variant.objects.filter(pk=variant.pk).update(out_of_stock_notified=True)
        return True
    if not out and variant.out_of_stock_notified:
        # Restocked (or tracking turned off): the old alert is no longer true.
        Notification.objects.filter(variant=variant, kind=Notification.Kind.OUT_OF_STOCK, read_at__isnull=True).update(
            read_at=timezone.now()
        )
        Variant.objects.filter(pk=variant.pk).update(out_of_stock_notified=False)
    return False


def sync_all(shop=None):
    """Check every category (of one shop, or of all shops when `shop` is None)."""
    created = 0
    # Only categories that might need a change: out of stock now, or flagged from an earlier alert.
    relevant = Q(out_of_stock_notified=True) | Q(product__track_stock=True, stock__lte=0)
    variants = Variant.objects.select_related("product__shop").filter(relevant)
    if shop is not None:
        variants = variants.filter(product__shop=shop)
    for v in variants:
        created += sync_variant(v)
    return created


def send_reminder_if_due(shop, now=None):
    """One digest notification every `reminder_days` days while items are still out of stock."""
    now = now or timezone.now()
    names = [
        label(v)
        for v in Variant.objects.select_related("product").filter(
            product__shop=shop, product__track_stock=True, stock__lte=0
        )
    ]
    if not names:
        return False
    days = ShopSettings.for_shop(shop).reminder_days
    latest = Notification.objects.filter(shop=shop).first()  # newest of any kind
    if latest is not None and now - latest.created_at < timedelta(days=days):
        return False
    shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    n = Notification.objects.create(
        shop=shop,
        kind=Notification.Kind.REMINDER,
        title=f"Reminder: {len(names)} item{'s' if len(names) != 1 else ''} still out of stock",
        message=f"{shown}. Please update your stock.",
    )
    Notification.objects.filter(pk=n.pk).update(created_at=now)
    return True


def run_checks(now=None):
    created = sync_all()
    reminded = sum(send_reminder_if_due(shop, now) for shop in Shop.objects.all())
    return {"out_of_stock": created, "reminder": bool(reminded)}


def _loop(interval):
    time.sleep(5)  # let the server finish starting
    while True:
        try:
            run_checks()
        except Exception:  # never let the monitor die
            log.exception("Stock monitor check failed")
        finally:
            close_old_connections()
        time.sleep(interval)


def start_background(interval_seconds=300):
    """Start the monitor thread once per process."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, args=(interval_seconds,), name="stock-monitor", daemon=True).start()
    log.info("Stock monitor started (every %s s)", interval_seconds)
