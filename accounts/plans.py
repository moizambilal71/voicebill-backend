"""Pro lasts PRO_DAYS (30) after each approved payment, then the shop goes back to Free until it pays again."""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from shop.models import Notification, Shop

from . import emailing

log = logging.getLogger(__name__)


def grant_pro(shop, days=None):
    """Switch the shop to Pro for `days`. Paying while Pro is still running adds the days on top of what is left."""
    days = days or settings.PRO_DAYS
    now = timezone.now()
    start = shop.pro_until if shop.plan == Shop.Plan.PRO and shop.pro_until and shop.pro_until > now else now
    shop.plan = Shop.Plan.PRO
    shop.pro_until = start + timedelta(days=days)
    shop.save(update_fields=["plan", "pro_until"])
    return shop


def _expired_message(shop):
    return (
        "Your Pro plan has ended and your shop is back on the Free plan. "
        "To keep making bills, tap Upgrade, choose a payment method and send your payment. We will activate Pro again."
    )


def _email_expired(shop):
    owner = shop.owner
    if owner is None:
        return
    last = owner.payment_requests.exclude(site_url="").first()
    site = (last.site_url if last else settings.FRONTEND_URL).rstrip("/")
    name = owner.first_name or "there"
    steps = (
        '<ol style="margin:14px 0;padding-left:20px;color:#374151;line-height:1.8">'
        "<li>Click <b>Upgrade to Pro</b></li><li>Select a payment method</li>"
        "<li>Send your payment and upload the screenshot</li></ol>"
    )
    text = (
        f"Hi {name},\n\nYour VoiceBill Pro plan has ended, so your shop is back on the Free plan. Your products and bills are safe.\n\n"
        f"To continue: click Upgrade to Pro, select a payment method, send your payment and upload the screenshot. "
        f"We will activate Pro again.\n\nUpgrade: {site}/plans\n"
    )
    msg = EmailMultiAlternatives("Your VoiceBill Pro plan has ended", text, None, [owner.email])
    msg.attach_alternative(
        emailing.layout(
            "Your Pro plan has ended",
            f"Hi {name},<br>Your Pro plan has ended and your shop is back on the <b>Free</b> plan. "
            "Your products, customers and bills are all safe.<br><br>To keep making bills, follow the same steps again:",
            steps,
            emailing.button(f"{site}/plans", "Upgrade to Pro"),
            "Already paid? We will activate your plan as soon as we confirm it.",
            preheader="Renew in a minute to keep making bills.",
            emoji="⏰",
        ),
        "text/html",
    )
    msg.send()


def downgrade_if_due(shop):
    """If this shop's Pro has run out: go back to Free, tell the owner in the app and by email. Safe to call often."""
    if shop.plan != Shop.Plan.PRO or shop.pro_until is None or shop.pro_until > timezone.now():
        return False
    # The single UPDATE decides who does the downgrade, so two requests at once cannot both send the email.
    changed = Shop.objects.filter(pk=shop.pk, plan=Shop.Plan.PRO, pro_until__lte=timezone.now()).update(plan=Shop.Plan.FREE)
    shop.plan = Shop.Plan.FREE
    if not changed:
        return False
    Notification.objects.create(
        shop=shop, kind=Notification.Kind.PLAN_EXPIRED, title="Your Pro plan has ended", message=_expired_message(shop)
    )
    try:
        _email_expired(shop)
    except Exception:  # noqa: BLE001 - the downgrade stands; the in-app notice is already there
        log.exception("Could not email the plan-ended notice for shop %s", shop.pk)
    return True


def downgrade_expired():
    """The periodic job: downgrade every shop whose Pro ran out (even if nobody opened the app)."""
    due = Shop.objects.filter(plan=Shop.Plan.PRO, pro_until__lte=timezone.now())
    return sum(downgrade_if_due(shop) for shop in due.select_related("owner"))
