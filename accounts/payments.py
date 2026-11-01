"""Manual payments: validating screenshots, emailing the admin, and approving / rejecting a request."""

import html
import logging
from email.utils import parseaddr
from urllib.parse import quote

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

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


def _button(url, label, color="#14876a"):
    return (
        f'<a href="{html.escape(url)}" style="display:inline-block;background:{color};color:#fff;text-decoration:none;'
        f'padding:12px 22px;border-radius:10px;font-weight:600;font-family:Arial,sans-serif">{html.escape(label)}</a>'
    )


def _table(rows):
    cells = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#666">{html.escape(k)}</td>'
        f'<td style="padding:6px 0"><b>{html.escape(str(v) or "-")}</b></td></tr>'
        for k, v in rows
    )
    return f'<table style="font-family:Arial,sans-serif;font-size:14px">{cells}</table>'


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
    body = (
        '<div style="font-family:Arial,sans-serif;max-width:560px">'
        '<h2 style="margin:0 0 4px">New Pro payment request</h2>'
        '<p style="color:#666;margin:0 0 16px">Check that the money arrived, then approve or reject it.</p>'
        f"{_table(rows)}<p style=\"margin:22px 0\">{_button(url, 'View payment details')}</p>"
        '<p style="color:#888;font-size:12px">The screenshots are attached. You need to be signed in to the admin to open the button.</p></div>'
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
    if payment.status == PaymentRequest.Status.APPROVED:
        subject = "Your VoiceBill Pro plan is active"
        text = f"Hi {name},\n\nThank you! We received your payment and your VoiceBill Pro plan is now active. You can make unlimited bills.\n\nOpen the app and keep selling.\n"
        body = (
            f'<div style="font-family:Arial,sans-serif;max-width:520px"><h2>Your Pro plan is active 🎉</h2>'
            f"<p>Hi {html.escape(name)},</p><p>Thank you! We received your payment and your VoiceBill <b>Pro</b> plan is now active. "
            "You can make unlimited bills.</p></div>"
        )
    else:
        link = whatsapp_link(payment)
        note = f"\n\nNote from us: {payment.admin_note}" if payment.admin_note else ""
        subject = "We could not confirm your VoiceBill payment"
        text = (
            f"Hi {name},\n\nWe could not find your payment for request #{payment.pk}, so your plan was not upgraded.{note}\n\n"
            + (f"To talk to us on WhatsApp (your payment details are filled in for you):\n{link}\n" if link else "Please reply to this email so we can sort it out.\n")
        )
        body = (
            f'<div style="font-family:Arial,sans-serif;max-width:520px"><h2>We could not confirm your payment</h2>'
            f"<p>Hi {html.escape(name)},</p><p>We could not find your payment for request <b>#{payment.pk}</b>, so your plan was not upgraded.</p>"
            + (f"<p><i>{html.escape(payment.admin_note)}</i></p>" if payment.admin_note else "")
            + ("<p>Please contact us so we can sort it out:</p><p>" + _button(link, "Chat with us on WhatsApp", "#25D366") + "</p>" if link else "<p>Please reply to this email so we can sort it out.</p>")
            + "</div>"
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
        shop = payment.shop
        shop.plan = shop.Plan.PRO
        shop.save(update_fields=["plan"])
    try:
        notify_customer(payment)
    except Exception:  # noqa: BLE001 - the decision stands even if the email fails; the admin is told
        log.exception("Could not email the customer about payment #%s", payment.pk)
        raise_after = True
    else:
        raise_after = False
    payment.email_failed = raise_after
    return payment
