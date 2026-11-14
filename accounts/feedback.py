"""Feedback from shopkeepers: validate, save, and email the owner."""

import logging

from django.core.mail import EmailMultiAlternatives

from . import emailing
from .models import Feedback
from .payments import admin_email

log = logging.getLogger(__name__)

RECOMMEND = {"yes", "maybe", "no"}
USES = {"Voice billing", "Barcode scan", "Printing bills", "Stock alerts", "Dashboard / reports"}
PROBLEMS = {"", "Voice did not understand", "Hard to use", "Printing", "Missing a feature", "Too slow", "Price / plans", "Other"}


class FeedbackError(ValueError):
    pass


def _stars(value, name):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise FeedbackError(f"Please rate: {name}.") from None
    if not 1 <= n <= 5:
        raise FeedbackError(f"Please rate: {name}.")
    return n


def create(user, data):
    overall = _stars(data.get("overall"), "How happy are you with VoiceBill")
    ease = _stars(data.get("ease"), "How easy is it to use")
    voice = _stars(data["voice"], "Voice billing") if data.get("voice") not in (None, "") else None
    recommend = data.get("recommend") or ""
    if recommend and recommend not in RECOMMEND:
        raise FeedbackError("Please choose yes, maybe or no.")
    uses = [u for u in (data.get("most_used") or []) if u in USES] if isinstance(data.get("most_used"), list) else []
    problem = data.get("problem") or ""
    if problem not in PROBLEMS:
        raise FeedbackError("Please choose a problem from the list.")
    message = str(data.get("message") or "").strip()[:3000]
    return Feedback.objects.create(
        user=user, overall=overall, ease=ease, voice=voice, recommend=recommend,
        most_used=", ".join(uses), problem=problem, message=message,
    )


def notify_admin(fb):
    """Email the owner. Never raises: the feedback is already saved."""
    try:
        who = fb.user.first_name or fb.user.email
        rows = [
            ("From", who),
            ("Login email", fb.user.email),
            ("Happy overall", f"{fb.overall} / 5"),
            ("Ease of use", f"{fb.ease} / 5"),
            ("Voice billing", f"{fb.voice} / 5" if fb.voice else "-"),
            ("Would recommend", fb.recommend or "-"),
            ("Uses most", fb.most_used or "-"),
            ("Biggest problem", fb.problem or "-"),
        ]
        message_html = ""
        if fb.message:
            import html

            message_html = (
                '<div style="margin:16px 0;padding:14px 16px;background:#f3f6f4;border-radius:12px;white-space:pre-wrap;'
                f'font-size:15px;line-height:1.55;color:#111827">{html.escape(fb.message)}</div>'
            )
        from django.conf import settings

        url = f"{settings.ADMIN_BASE_URL}/admin/accounts/feedback/{fb.pk}/change/"
        text = f"New feedback from {who} ({fb.user.email})\n\n" + "\n".join(f"{k}: {v}" for k, v in rows) + f"\n\nMessage:\n{fb.message or '-'}\n\n{url}\n"
        body = emailing.layout(
            "New feedback from a user",
            f"{who} sent feedback. You can reply to this email to answer them.",
            emailing.details_table(rows) + message_html,
            emailing.button(url, "Open in admin"),
            preheader=f"{fb.overall}/5 from {fb.user.email}",
            emoji="💬",
        )
        msg = EmailMultiAlternatives(f"Feedback ({fb.overall}/5) from {fb.user.email}", text, None, [admin_email()], reply_to=[fb.user.email])
        msg.attach_alternative(body, "text/html")
        msg.send()
    except Exception:  # noqa: BLE001
        log.exception("Could not email feedback #%s", fb.pk)
