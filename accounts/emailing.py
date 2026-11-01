"""One modern look for every email VoiceBill sends, and the address its buttons should open.

Buttons point at the website the customer actually used: http://localhost:3000 while you test locally, your
domain once deployed. That address comes from the browser's Origin header, but only if it is one we trust
(localhost, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS or FRONTEND_URL); anything else falls back to FRONTEND_URL.
"""

import html
from fnmatch import fnmatch
from urllib.parse import urlparse

from django.conf import settings

MINT = "#14876a"
MINT_DARK = "#0b6a53"


def _trusted(origin):
    parts = urlparse(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if origin.rstrip("/") == settings.FRONTEND_URL.rstrip("/"):
        return True
    if parts.hostname in ("localhost", "127.0.0.1") and parts.scheme == "http":
        return settings.DEBUG
    if any(fnmatch(origin.rstrip("/"), pat) for pat in settings.CSRF_TRUSTED_ORIGINS):
        return True
    return parts.hostname in settings.ALLOWED_HOSTS and parts.hostname not in ("localhost", "127.0.0.1")


def frontend_url(request=None):
    """The site address to put in email buttons (no trailing slash)."""
    if request is not None:
        origin = request.META.get("HTTP_ORIGIN") or ""
        if not origin:
            ref = urlparse(request.META.get("HTTP_REFERER") or "")
            origin = f"{ref.scheme}://{ref.netloc}" if ref.scheme and ref.netloc else ""
        if origin and _trusted(origin):
            return origin.rstrip("/")
    return settings.FRONTEND_URL.rstrip("/")


def button(url, label, color=MINT, outline=False):
    style = (
        f"border:2px solid {color};color:{color};background:#ffffff;"
        if outline
        else f"background:{color};color:#ffffff;border:2px solid {color};"
    )
    return (
        f'<a href="{html.escape(url)}" style="display:inline-block;{style}text-decoration:none;padding:13px 26px;'
        f'border-radius:12px;font-weight:700;font-size:15px;margin:4px 6px 4px 0">{html.escape(label)}</a>'
    )


def code_box(code):
    return (
        '<div style="margin:22px 0;padding:18px 10px;text-align:center;background:#eefaf5;border:1px solid #bfe8d8;'
        'border-radius:16px"><span style="font-family:Consolas,Menlo,monospace;font-size:38px;font-weight:700;'
        f'letter-spacing:10px;color:{MINT_DARK}">{html.escape(code)}</span></div>'
    )


def details_table(rows):
    cells = "".join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid #eef0ee;color:#6b7280;font-size:13px;width:42%">{html.escape(str(k))}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #eef0ee;font-size:14px;font-weight:600;color:#111827">{html.escape(str(v) or "-")}</td></tr>'
        for k, v in rows
    )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0">{cells}</table>'


def layout(title, intro_html, body_html="", buttons_html="", footer="", preheader="", emoji=""):
    """The shared email frame: mint header, white card, big title, buttons, quiet footer."""
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f6f4">
<span style="display:none;max-height:0;overflow:hidden;opacity:0">{html.escape(preheader or title)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f6f4;padding:28px 12px">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;font-family:'Segoe UI',Arial,sans-serif">
    <tr><td style="background:{MINT};background-image:linear-gradient(135deg,#17b98c,{MINT_DARK});border-radius:20px 20px 0 0;padding:26px 30px">
      <span style="font-size:22px;font-weight:800;color:#ffffff;letter-spacing:.3px">🎙️ VoiceBill</span>
      <span style="display:block;margin-top:2px;font-size:12px;color:#d9f4ea">Voice &amp; barcode billing for your shop</span>
    </td></tr>
    <tr><td style="background:#ffffff;padding:32px 30px 26px;border-radius:0 0 20px 20px;box-shadow:0 6px 24px rgba(0,0,0,.06)">
      {f'<div style="font-size:40px;line-height:1;margin-bottom:10px">{emoji}</div>' if emoji else ''}
      <h1 style="margin:0 0 10px;font-size:24px;line-height:1.25;color:#111827">{html.escape(title)}</h1>
      <div style="font-size:15px;line-height:1.6;color:#374151">{intro_html}</div>
      {body_html}
      {f'<div style="margin-top:24px">{buttons_html}</div>' if buttons_html else ''}
    </td></tr>
    <tr><td style="padding:18px 10px;text-align:center;font-size:12px;line-height:1.6;color:#8a948f">{footer or "You received this email because of activity on your VoiceBill account."}</td></tr>
  </table>
</td></tr></table></body></html>"""
