"""Email the owner when the OpenAI account has a problem (no credits, bad key, rate limit...).

One email per kind of problem every few hours, so a long outage does not flood the inbox. The shopkeeper still
sees the normal error message; this is only for the owner.
"""

import logging
from datetime import timedelta

from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

log = logging.getLogger(__name__)
REPEAT = timedelta(hours=6)


def classify(exc):
    """(kind, title, what to do) for an OpenAI error the owner must act on, else None."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return None
    text = str(exc).lower()
    if isinstance(exc, openai.AuthenticationError):
        return ("openai_key", "OpenAI API key is invalid or was removed",
                "Create a new key at platform.openai.com/api-keys, put it in OPENAI_API_KEY in /opt/voicebill/backend/.env, then run: systemctl restart voicebill-api")
    if isinstance(exc, openai.PermissionDeniedError):
        return ("openai_permission", "OpenAI refused the request (permission)",
                "Check that the project/key may use the transcribe and chat models, and that the account is in good standing.")
    code = str(getattr(exc, "code", "") or "")
    if isinstance(exc, openai.RateLimitError):
        if "insufficient_quota" in code or "quota" in text or "billing" in text or "credit" in text:
            return ("openai_credits", "OpenAI credits are finished (or the billing limit is reached)",
                    "Add credit at platform.openai.com/settings/organization/billing. Until then voice billing will not work for any customer.")
        return ("openai_rate", "OpenAI is rate limiting VoiceBill", "Usually temporary. If it keeps happening, raise your usage tier or limits.")
    if isinstance(exc, openai.NotFoundError):
        return ("openai_model", "An OpenAI model name was not found",
                "Check OPENAI_TRANSCRIBE_MODEL and OPENAI_CHAT_MODEL in /opt/voicebill/backend/.env.")
    return None


def notify_if_needed(exc):
    """Call from the voice error handler. Never raises."""
    try:
        found = classify(exc)
        if found is None:
            return False
        kind, title, todo = found
        from accounts import emailing
        from accounts.models import AdminAlert
        from accounts.payments import admin_email

        now = timezone.now()
        row, created = AdminAlert.objects.get_or_create(kind=kind, defaults={"last_sent_at": now})
        if not created:
            if now - row.last_sent_at < REPEAT:
                return False
            row.last_sent_at = now
            row.save(update_fields=["last_sent_at"])
        detail = f"{exc.__class__.__name__}: {str(exc)[:400]}"
        text = f"{title}\n\nWhat to do: {todo}\n\nError from OpenAI:\n{detail}\n"
        body = emailing.layout(
            title,
            f"A customer just tried voice billing and OpenAI returned an error.<br><br><b>What to do:</b> {todo}",
            emailing.details_table([("Error", detail), ("Time", now.strftime("%Y-%m-%d %H:%M UTC"))]),
            footer="This alert is sent at most once every 6 hours for each kind of problem.",
            preheader=title,
            emoji="⚠️",
        )
        msg = EmailMultiAlternatives(f"⚠️ VoiceBill: {title}", text, None, [admin_email()])
        msg.attach_alternative(body, "text/html")
        msg.send()
        return True
    except Exception:  # noqa: BLE001
        log.exception("Could not send the OpenAI alert")
        return False
