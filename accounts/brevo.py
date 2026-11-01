"""Email backend that sends through Brevo's web API (https://developers.brevo.com).

Turn on with:  EMAIL_BACKEND=accounts.brevo.BrevoEmailBackend  and  BREVO_API_KEY=...
The sender address (DEFAULT_FROM_EMAIL) must be a verified sender in your Brevo account.
Supports an HTML version (EmailMultiAlternatives) and attachments.
"""

import base64
import logging
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

log = logging.getLogger(__name__)
URL = "https://api.brevo.com/v3/smtp/email"


class BrevoEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not settings.BREVO_API_KEY:
            if not self.fail_silently:
                raise ValueError("BREVO_API_KEY is not set.")
            return 0
        sent = 0
        for msg in email_messages:
            name, address = parseaddr(msg.from_email or settings.DEFAULT_FROM_EMAIL)
            payload = {
                "sender": {"email": address, **({"name": name} if name else {})},
                "to": [{"email": to} for to in msg.to],
                "subject": msg.subject,
                "textContent": msg.body,
            }
            html = next((content for content, mime in getattr(msg, "alternatives", []) if mime == "text/html"), None)
            if html:
                payload["htmlContent"] = html
            if msg.reply_to:
                payload["replyTo"] = {"email": parseaddr(msg.reply_to[0])[1]}
            attachments = []
            for att in msg.attachments:
                if isinstance(att, tuple) and len(att) == 3:
                    filename, content, _mime = att
                    if isinstance(content, str):
                        content = content.encode()
                    attachments.append({"name": filename, "content": base64.b64encode(content).decode()})
            if attachments:
                payload["attachment"] = attachments
            try:
                res = requests.post(
                    URL, json=payload, timeout=20,
                    headers={"api-key": settings.BREVO_API_KEY, "accept": "application/json"},
                )
                if res.status_code >= 300:
                    # Log Brevo's reason (e.g. sender not verified) but never the key.
                    log.error("Brevo refused the email (%s): %s", res.status_code, res.text[:300])
                    raise RuntimeError(f"Brevo error {res.status_code}")
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent
