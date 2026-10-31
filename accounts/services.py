import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from shop.models import Shop, create_starter_data

from .models import EmailCode, Profile, TrustedDevice

log = logging.getLogger(__name__)
User = get_user_model()

DEVICE_COOKIE = "vb_device"


class CodeError(Exception):
    """A verification code was wrong, expired or used up. The message is safe to show."""


class EmailSendError(Exception):
    pass


class GoogleError(Exception):
    pass


def normalize_email(email):
    return (email or "").strip().lower()


def _hash(value):
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ emailed codes

def send_code(email, purpose, user=None):
    """Create a fresh 6-digit code (older unused ones stop working) and email it."""
    now = timezone.now()
    EmailCode.objects.filter(email=email, purpose=purpose, consumed_at__isnull=True).update(consumed_at=now)
    code = f"{secrets.randbelow(1_000_000):06d}"
    row = EmailCode(
        email=email,
        purpose=purpose,
        user=user,
        expires_at=now + timedelta(seconds=settings.EMAIL_CODE_TTL_SECONDS),
    )
    row.code_hash = _hash(f"{row.id}:{code}")
    row.save()

    reason = {
        EmailCode.Purpose.SIGNUP: "finish creating your VoiceBill account",
        EmailCode.Purpose.LOGIN: "sign in to VoiceBill on this device",
        EmailCode.Purpose.RESET: "reset your VoiceBill password",
    }[purpose]
    minutes = settings.EMAIL_CODE_TTL_SECONDS // 60
    body = (
        f"Your VoiceBill verification code is:\n\n    {code}\n\n"
        f"Use it to {reason}. It works once and expires in {minutes} minutes.\n\n"
        "Never share this code with anyone. VoiceBill staff will never ask for it.\n"
        "If you did not ask for it, you can ignore this email; your account is safe."
    )
    try:
        send_mail(f"{code} is your VoiceBill code", body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - SMTP errors vary
        log.exception("Could not send the verification email")
        raise EmailSendError("We could not send the email right now. Please try again in a minute.") from exc
    return row


def seconds_until_resend(email, purpose):
    last = (
        EmailCode.objects.filter(email=email, purpose=purpose).order_by("-created_at").values_list("created_at", flat=True).first()
    )
    if last is None:
        return 0
    wait = settings.EMAIL_CODE_RESEND_SECONDS - (timezone.now() - last).total_seconds()
    return max(int(wait) + (1 if wait > 0 else 0), 0)


def check_code(email, purpose, code):
    """Validate and use up a code. Returns the EmailCode row. Wrong guesses are counted; too many kills the code."""
    code = (code or "").strip().replace(" ", "")
    row = (
        EmailCode.objects.filter(email=email, purpose=purpose, consumed_at__isnull=True).order_by("-created_at").first()
    )
    if row is None or row.expires_at <= timezone.now():
        raise CodeError("This code has expired. Ask for a new one.")
    if row.attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
        raise CodeError("Too many wrong tries. Ask for a new code.")
    ok = code.isdigit() and len(code) == 6 and hmac.compare_digest(_hash(f"{row.id}:{code}"), row.code_hash)
    if not ok:
        row.attempts += 1
        if row.attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
            row.consumed_at = timezone.now()
        row.save(update_fields=["attempts", "consumed_at"])
        left = settings.EMAIL_CODE_MAX_ATTEMPTS - row.attempts
        raise CodeError("That code is not right." + (f" {left} tries left." if left > 0 else " Ask for a new code."))
    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at"])
    return row


# ------------------------------------------------------------------ shops

@transaction.atomic
def claim_or_create_shop(user, shop_name=""):
    """Give a new account its shop. The very first account takes over data that existed before accounts."""
    if getattr(user, "shop", None) is not None:
        return user.shop
    legacy = Shop.objects.select_for_update().filter(owner__isnull=True).order_by("id").first()
    allowed = settings.LEGACY_SHOP_OWNER_EMAIL in ("", user.email.lower())
    if legacy is not None and allowed:
        legacy.owner = user
        if shop_name and not legacy.name:
            legacy.name = shop_name
        legacy.save(update_fields=["owner", "name"])
        log.warning("Account %s took over the existing shop data (shop %s)", user.email, legacy.pk)
        return legacy
    shop = Shop.objects.create(owner=user, name=shop_name)
    create_starter_data(shop)
    if shop_name:
        shop.settings.shop_name = shop_name
        shop.settings.save(update_fields=["shop_name"])
    return shop


# ------------------------------------------------------------------ trusted devices

def trust_device(user, user_agent=""):
    """Remember this browser: returns the secret token to put in the cookie."""
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    TrustedDevice.objects.filter(user=user, expires_at__lt=now).delete()
    TrustedDevice.objects.create(
        user=user,
        token_hash=_hash(f"device:{token}"),
        user_agent=(user_agent or "")[:200],
        expires_at=now + timedelta(days=settings.TRUSTED_DEVICE_DAYS),
    )
    return token


def is_trusted_device(user, token):
    if not token:
        return False
    device = TrustedDevice.objects.filter(
        user=user, token_hash=_hash(f"device:{token}"), expires_at__gt=timezone.now()
    ).first()
    if device is None:
        return False
    device.last_used_at = timezone.now()
    device.save(update_fields=["last_used_at"])
    return True


# ------------------------------------------------------------------ Google

def verify_google_token(credential):
    """Check a Google ID token (signature, audience, expiry) and return its claims."""
    if not settings.GOOGLE_CLIENT_ID:
        raise GoogleError("not_configured")
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        return id_token.verify_oauth2_token(credential, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
    except ValueError as exc:
        raise GoogleError("invalid") from exc
    except GoogleAuthError as exc:  # could not reach Google to fetch its public keys
        log.warning("Google sign-in check failed: %s", exc)
        raise GoogleError("unavailable") from exc


@transaction.atomic
def user_from_google(claims):
    """Find or create the account for a verified Google identity."""
    email = normalize_email(claims.get("email"))
    sub = claims.get("sub")
    if not email or not sub or not claims.get("email_verified"):
        raise GoogleError("unverified")
    profile = Profile.objects.select_related("user").filter(google_sub=sub).first()
    if profile:
        user = profile.user
        if not user.is_active:
            raise GoogleError("disabled")
        return user, False
    user = User.objects.filter(username=email).first()
    created = user is None
    if created:
        user = User(username=email, email=email, first_name=(claims.get("name") or "")[:150])
        user.set_unusable_password()
        user.save()
    else:
        if not user.is_active and user.last_login is not None:  # switched off by an admin
            raise GoogleError("disabled")
        if not user.is_active:
            # An unverified password sign-up: Google has now proven who owns the address. Drop any password that
            # was chosen before that proof so nobody else can use it.
            user.set_unusable_password()
            user.is_active = True
            user.save(update_fields=["password", "is_active"])
    Profile.objects.update_or_create(user=user, defaults={"google_sub": sub})
    return user, created
