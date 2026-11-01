"""Manual payments: validating screenshots, emailing the admin, and approving / rejecting a request."""

import html
import logging
from email.utils import parseaddr
from urllib.parse import quote

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from shop.models import Notification

from . import emailing, plans
from .models import PaymentMethod, PaymentProof, PaymentRequest

log = logging.getLogger(__name__)

SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]


class ProofError(ValueError):
    """A screenshot that cannot be accepted. The message is safe to show."""


def sniff_image(head):
    """The real type of an upload from its first bytes (the browser's claim is not trusted)."""
    for sig, mime in SIGNATURES:
        if head.startswith(sig):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_files(files):
    """Check every uploaded screenshot. Returns [(file, content_type)]."""
    if not files:
        raise ProofError("Please upload at least one screenshot of your payment.")
    if len(files) > settings.MAX_PROOF_FILES:
        raise ProofError(f"You can upload up to {settings.MAX_PROOF_FILES} screenshots.")
    out = []
    for f in files:
        if f.size > settings.MAX_PROOF_BYTES:
            raise ProofError(f"“{f.name[:40]}” is too big. Each screenshot must be under {settings.MAX_PROOF_BYTES // (1024 * 1024)} MB.")
        mime = sniff_image(f.read(16))
        f.seek(0)
        if mime is None:
            raise ProofError(f"“{f.name[:40]}” is not a JPG, PNG or WebP image.")
        out.append((f, mime))
    return out


def admin_email():
    if settings.ADMIN_NOTIFY_EMAIL:
        return settings.ADMIN_NOTIFY_EMAIL
    return parseaddr(settings.DEFAULT_FROM_EMAIL)[1]


def admin_url(payment):
    return f"{settings.ADMIN_BASE_URL}/admin/accounts/paymentrequest/{payment.pk}/change/"


def notify_admin(payment):
    """Email the admin: who paid, how, the transaction details, the screenshots attached, and a button to the record."""
    rows = [
        ("Customer", payment.user.first_name or payment.user.email),
        ("Login email", payment.user.email),
        ("Shop", str(payment.shop)),
        ("Payment method", payment.method.name),
        ("Amount", payment.amount_label),
        ("Their account / email", payment.payer_account),
        ("Transaction ID", payment.transaction_id),
        ("Screenshots", payment.proofs.count()),
        ("Request", f"#{payment.pk}"),
    ]
    url = admin_url(payment)
    text = "New Pro payment request\n\n" + "\n".join(f"{k}: {v or '-'}" for k, v in rows) + f"\n\nView payment details: {url}\n"
    body = emailing.layout(
        "New Pro payment request",
        "Check that the money arrived in your account, then approve or reject it.",
        emailing.details_table(rows),
        emailing.button(url, "View payment details"),
        "The screenshots are attached. You need to be signed in to the admin for the button to open.",
        preheader=f"{payment.user.email} paid with {payment.method.name}",
        emoji="💳",
    )
    msg = EmailMultiAlternatives(
        f"Payment request #{payment.pk} · {payment.method.name} · {payment.user.email}", text, None, [admin_email()],
        reply_to=[payment.user.email],
    )
    msg.attach_alternative(body, "text/html")
    budget = 3_500_000  # stay inside what email providers accept; the rest are in the admin page
    for i, proof in enumerate(payment.proofs.all(), 1):
        if proof.size > budget:
            continue
        budget -= proof.size
        with proof.file.open("rb") as fh:
            msg.attach(f"screenshot-{i}.{proof.file.name.rsplit('.', 1)[-1]}", fh.read(), proof.content_type)
    msg.send()


def whatsapp_link(payment):
    if not settings.ADMIN_WHATSAPP_NUMBER:
        return ""
    text = (
        f"Hello, my VoiceBill Pro payment request #{payment.pk} was not approved. "
        f"Method: {payment.method.name}. Transaction ID: {payment.transaction_id or 'n/a'}. Account email: {payment.user.email}."
    )
    return f"https://wa.me/{settings.ADMIN_WHATSAPP_NUMBER}?text={quote(text)}"


def notify_customer(payment):
    """Tell the customer (on their sign-in email, never the payment account) what happened."""
    name = payment.user.first_name or "there"
    site = (payment.site_url or settings.FRONTEND_URL).rstrip("/")
    if payment.status == PaymentRequest.Status.APPROVED:
        subject = "Your VoiceBill Pro plan is active"
        text = (
            f"Hi {name},\n\nThank you! We received your payment and your VoiceBill Pro plan is now active. "
            f"You can make unlimited bills.\n\nOpen your dashboard: {site}/\nCreate your first bill: {site}/?new=1\n"
        )
        body = emailing.layout(
            "Your Pro plan is active!",
            f"Hi {html.escape(name)},<br>Thank you! We received your payment and your VoiceBill <b>Pro</b> plan is now active. "
            "You can make <b>unlimited bills</b> from today.",
            emailing.details_table([("Plan", "Pro"), ("Paid with", payment.method.name), ("Bills", "Unlimited"), ("Active until", f"{payment.shop.pro_until:%d %b %Y}" if payment.shop.pro_until else "No end date")]),
            emailing.button(f"{site}/", "Open Dashboard") + emailing.button(f"{site}/?new=1", "Create Your First Bill", outline=True),
            "Need help? Just reply to this email.",
            preheader="Payment received. Unlimited bills are ready.",
            emoji="🎉",
        )
    else:
        link = whatsapp_link(payment)
        note = f"\n\nNote from us: {payment.admin_note}" if payment.admin_note else ""
        subject = "We could not confirm your VoiceBill payment"
        text = (
            f"Hi {name},\n\nWe could not find your payment for request #{payment.pk}, so your plan was not upgraded.{note}\n\n"
            + (f"To talk to us on WhatsApp (your payment details are filled in for you):\n{link}\n" if link else "Please reply to this email so we can sort it out.\n")
            + f"\nYou can also try again: {site}/plans\n"
        )
        buttons = (emailing.button(link, "Chat with us on WhatsApp", "#1fa855") if link else "") + emailing.button(f"{site}/plans", "Try again", outline=True)
        body = emailing.layout(
            "We could not confirm your payment",
            f"Hi {html.escape(name)},<br>We could not find your payment for request <b>#{payment.pk}</b>, so your plan was not upgraded."
            + (f'<br><br><i style="color:#6b7280">“{html.escape(payment.admin_note)}”</i>' if payment.admin_note else "")
            + "<br><br>Please contact us and we will sort it out quickly."
            + ("" if link else " Just reply to this email."),
            emailing.details_table([("Request", f"#{payment.pk}"), ("Payment method", payment.method.name), ("Transaction ID", payment.transaction_id or "-")]),
            buttons,
            "Your account and bills are safe. Nothing was deleted.",
            preheader="We could not find your payment yet.",
            emoji="⚠️",
        )
    msg = EmailMultiAlternatives(subject, text, None, [payment.user.email], reply_to=[admin_email()])
    msg.attach_alternative(body, "text/html")
    msg.send()


class ReviewError(Exception):
    pass


@transaction.atomic
def review(payment, approve, admin_user=None, note=""):
    """Approve (turns the shop to Pro) or reject a pending request, then email the customer. Does nothing twice."""
    payment = PaymentRequest.objects.select_for_update().select_related("shop", "user", "method").get(pk=payment.pk)
    if payment.status != PaymentRequest.Status.PENDING:
        raise ReviewError(f"Request #{payment.pk} was already {payment.get_status_display().lower()}.")
    payment.status = PaymentRequest.Status.APPROVED if approve else PaymentRequest.Status.REJECTED
    payment.reviewed_at = timezone.now()
    payment.reviewed_by = admin_user
    if note:
        payment.admin_note = note[:300]
    payment.save()
    if approve:
        plans.grant_pro(payment.shop)
        Notification.objects.create(
            shop=payment.shop, kind=Notification.Kind.PLAN_ACTIVE, title="Your Pro plan is active",
            message=f"Unlimited bills until {payment.shop.pro_until:%d %b %Y}. Thank you!",
        )
    try:
        notify_customer(payment)
    except Exception:  # noqa: BLE001 - the decision stands even if the email fails; the admin is told
        log.exception("Could not email the customer about payment #%s", payment.pk)
        raise_after = True
    else:
        raise_after = False
    payment.email_failed = raise_after
    return payment
