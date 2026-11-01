import logging

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.hashers import check_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework import status
from django.http import FileResponse
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import emailing, payments, ratelimit, services
from .authentication import CsrfAlways, SessionAuth
from .models import EmailCode, PaymentMethod, PaymentProof, PaymentRequest, TrustedDevice

log = logging.getLogger(__name__)
User = get_user_model()

# Hash of a throw-away password, checked when the email is unknown so a login takes the same time either way.
_DUMMY_HASH = "pbkdf2_sha256$1000000$dummy$" + "A" * 43
WRONG_LOGIN = "Wrong email or password."


def public(view):
    """Sign-in/sign-up endpoints: open to everyone, but they still need a CSRF token."""
    view = permission_classes([AllowAny])(view)
    return authentication_classes([CsrfAlways])(view)


def too_many(retry_minutes=10):
    return Response(
        {"detail": f"Too many tries. Please wait {retry_minutes} minutes and try again.", "code": "rate_limited"},
        status=429,
    )


def err(message, code=None, http=400, **extra):
    body = {"detail": message}
    if code:
        body["code"] = code
    return Response({**body, **extra}, status=http)


def ip_ok(request, bucket, limit=100, window=600):
    return ratelimit.hit(f"ip:{bucket}:{ratelimit.client_ip(request)}", limit, window)


def user_json(user):
    shop = getattr(user, "shop", None)
    profile = getattr(user, "profile", None)
    return {
        "id": user.id,
        "email": user.email,
        "name": user.first_name,
        "has_password": user.has_usable_password(),
        "google": bool(profile and profile.google_sub),
        "shop": shop_json(shop),
    }


def shop_json(shop):
    if shop is None:
        return None
    return {
        "id": shop.id,
        "name": shop.name,
        "plan": shop.plan,
        "bill_limit": shop.bill_limit,
        "bills_used": shop.bills_generated,
        "bills_left": shop.bills_left,
        "at_limit": shop.at_limit,
        "pro_until": shop.pro_until.isoformat() if shop.pro_until else None,
        "days_left": shop.days_left,
    }


def start_session(request, user, response, trust=False):
    """Sign the user in (new session id, so an old one cannot be reused) and optionally remember the device."""
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    services.claim_or_create_shop(user)
    user.refresh_from_db()
    get_token(request)  # a fresh CSRF token for the new session
    if trust:
        token = services.trust_device(user, request.META.get("HTTP_USER_AGENT", ""))
        set_device_cookie(response, token)
    return response


def set_device_cookie(response, token):
    response.set_cookie(
        services.DEVICE_COOKIE,
        token,
        max_age=settings.TRUSTED_DEVICE_DAYS * 86400,
        httponly=True,
        samesite="Lax",
        secure=settings.SECURE_COOKIES,
    )


def need_code(email, purpose, user=None, **extra):
    """Send the code and tell the app to show the code screen."""
    wait = services.seconds_until_resend(email, purpose)
    if wait:  # asked again too fast: keep the code already sent
        return Response({"verification_required": True, "purpose": purpose, "email": email, "resend_in": wait, **extra})
    services.send_code(email, purpose, user)
    return Response(
        {"verification_required": True, "purpose": purpose, "email": email, "resend_in": settings.EMAIL_CODE_RESEND_SECONDS, **extra}
    )


def clean_email(raw):
    email = services.normalize_email(raw)
    try:
        validate_email(email)
    except DjangoValidationError:
        return None
    return email if len(email) <= 150 else None


def password_problem(password, user=None):
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        return " ".join(exc.messages)
    return None


# ------------------------------------------------------------------ endpoints

@api_view(["GET"])
@permission_classes([AllowAny])
def csrf(request):
    """Gives the browser its CSRF cookie before the first sign-in/sign-up request."""
    get_token(request)
    return Response({"ok": True, "google_client_id": settings.GOOGLE_CLIENT_ID or None})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(user_json(request.user))


@api_view(["POST"])
@public
def signup(request):
    if not ip_ok(request, "signup", 30):
        return too_many()
    name = str(request.data.get("name") or "").strip()[:150]
    shop_name = str(request.data.get("shop_name") or "").strip()[:200]
    email = clean_email(request.data.get("email"))
    password = str(request.data.get("password") or "")
    if not name:
        return err("Please enter your name.")
    if email is None:
        return err("Please enter a valid email address.")
    if problem := password_problem(password, User(username=email, email=email, first_name=name)):
        return err(problem, "weak_password")
    if not ratelimit.hit(f"email-send:{email}", 5, 3600):
        return too_many(60)

    existing = User.objects.filter(username=email).first()
    try:
        if existing is not None and existing.is_active:
            # Do not reveal that the address already has an account. The real owner gets an email saying so.
            from django.core.mail import EmailMultiAlternatives

            site = emailing.frontend_url(request)
            note = EmailMultiAlternatives(
                "Someone tried to create a VoiceBill account with your email",
                f"This email already has a VoiceBill account. If it was you, just sign in: {site}/login (or use 'Forgot password').\n"
                "If it was not you, you can ignore this message.",
                settings.DEFAULT_FROM_EMAIL,
                [email],
            )
            note.attach_alternative(
                emailing.layout(
                    "You already have an account",
                    "Someone (hopefully you) tried to create a VoiceBill account with this email, but it already has one. "
                    "Just sign in, or reset your password if you forgot it.",
                    "",
                    emailing.button(f"{site}/login", "Sign in") + emailing.button(f"{site}/forgot", "Forgot password", outline=True),
                    "If this was not you, you can ignore this message. Your account is safe.",
                    emoji="👋",
                ),
                "text/html",
            )
            note.send()
            return Response({"verification_required": True, "purpose": "signup", "email": email, "resend_in": settings.EMAIL_CODE_RESEND_SECONDS})
        with transaction.atomic():
            if existing is None:
                user = User(username=email, email=email, first_name=name, is_active=False)
            else:  # an earlier sign-up that was never verified: start over with what they typed now
                user = existing
                user.first_name = name
            user.set_password(password)
            user.save()
        request.session["pending_shop_name"] = shop_name
        services.send_code(email, EmailCode.Purpose.SIGNUP, user)
    except services.EmailSendError as exc:
        return err(str(exc), "email_failed", 503)
    return Response({"verification_required": True, "purpose": "signup", "email": email, "resend_in": settings.EMAIL_CODE_RESEND_SECONDS})


@api_view(["POST"])
@public
def login_view(request):
    if not ip_ok(request, "login", 60):
        return too_many()
    email = clean_email(request.data.get("email"))
    password = str(request.data.get("password") or "")
    if email is None or not password:
        return err(WRONG_LOGIN, "bad_login")
    lock_key = f"login-fail:{email}"
    if ratelimit.blocked(lock_key, 8):
        return too_many(15)

    user = User.objects.filter(username=email).first()
    if user is None:
        check_password(password, _DUMMY_HASH)
        ratelimit.hit(lock_key, 8, 900)
        return err(WRONG_LOGIN, "bad_login")
    if not user.check_password(password) or not user.has_usable_password():
        ratelimit.hit(lock_key, 8, 900)
        return err(WRONG_LOGIN, "bad_login")
    ratelimit.reset(lock_key)

    try:
        if not user.is_active:
            if user.last_login is not None:
                return err("This account has been switched off. Please contact support.", "disabled", 403)
            return need_code(email, EmailCode.Purpose.SIGNUP, user)  # never finished verifying the email
        if services.is_trusted_device(user, request.COOKIES.get(services.DEVICE_COOKIE)):
            response = Response({"user": None})
            start_session(request, user, response)
            response.data = {"user": user_json(user)}
            return response
        if not ratelimit.hit(f"email-send:{email}", 8, 3600):
            return too_many(60)
        return need_code(email, EmailCode.Purpose.LOGIN, user)
    except services.EmailSendError as exc:
        return err(str(exc), "email_failed", 503)


@api_view(["POST"])
@public
def verify(request):
    """The 6-digit code from the email. Finishes sign-up or a sign-in from a new device."""
    if not ip_ok(request, "verify", 60):
        return too_many()
    email = clean_email(request.data.get("email"))
    purpose = request.data.get("purpose")
    if email is None or purpose not in (EmailCode.Purpose.SIGNUP, EmailCode.Purpose.LOGIN):
        return err("Something went wrong. Please start again.")
    if ratelimit.blocked(f"verify-fail:{email}", 15):
        return too_many(15)
    try:
        row = services.check_code(email, purpose, request.data.get("code"))
    except services.CodeError as exc:
        ratelimit.hit(f"verify-fail:{email}", 15, 900)
        return err(str(exc), "bad_code")
    user = User.objects.filter(username=email).first()
    if user is None:
        return err("Something went wrong. Please start again.")
    if purpose == EmailCode.Purpose.SIGNUP and not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    elif not user.is_active:
        return err("This account has been switched off. Please contact support.", "disabled", 403)
    if purpose == EmailCode.Purpose.SIGNUP:
        shop_name = request.session.pop("pending_shop_name", "")
        services.claim_or_create_shop(user, shop_name)
    trust = purpose == EmailCode.Purpose.SIGNUP or bool(request.data.get("trust_device"))
    response = Response({})
    start_session(request, user, response, trust=trust)
    response.data = {"user": user_json(user)}
    return response


@api_view(["POST"])
@public
def resend(request):
    if not ip_ok(request, "resend", 30):
        return too_many()
    email = clean_email(request.data.get("email"))
    purpose = request.data.get("purpose")
    if email is None or purpose not in ("signup", "login", "reset"):
        return err("Something went wrong. Please start again.")
    if not ratelimit.hit(f"email-send:{email}", 8, 3600):
        return too_many(60)
    wait = services.seconds_until_resend(email, purpose)
    if wait:
        return Response({"resend_in": wait})
    user = User.objects.filter(username=email).first()
    # Only send when a pending request of this kind exists (and never reveal whether the address is registered).
    pending = EmailCode.objects.filter(email=email, purpose=purpose).exists()
    if user is not None and pending:
        try:
            services.send_code(email, purpose, user)
        except services.EmailSendError as exc:
            return err(str(exc), "email_failed", 503)
    return Response({"resend_in": settings.EMAIL_CODE_RESEND_SECONDS})


@api_view(["POST"])
@public
def google(request):
    if not ip_ok(request, "google", 60):
        return too_many()
    credential = request.data.get("credential")
    if not credential or not isinstance(credential, str):
        return err("Google sign-in did not finish. Please try again.")
    try:
        claims = services.verify_google_token(credential)
        user, created = services.user_from_google(claims)
    except services.GoogleError as exc:
        reason = str(exc)
        if reason == "not_configured":
            return err("Google sign-in is not set up on this server yet.", "google_not_configured", 503)
        if reason == "disabled":
            return err("This account has been switched off. Please contact support.", "disabled", 403)
        if reason == "unavailable":
            return err("We could not reach Google. Please try again in a moment.", "google_unavailable", 503)
        if reason == "unverified":
            return err("Your Google email is not verified.", "google_unverified")
        return err("Google sign-in failed. Please try again.", "google_invalid")
    response = Response({})
    start_session(request, user, response, trust=True)
    response.data = {"user": user_json(user), "created": created}
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def logout_view(request):
    logout(request)
    return Response({"ok": True})


@api_view(["POST"])
@public
def password_forgot(request):
    if not ip_ok(request, "forgot", 30):
        return too_many()
    email = clean_email(request.data.get("email"))
    if email is None:
        return err("Please enter a valid email address.")
    if not ratelimit.hit(f"email-send:{email}", 5, 3600):
        return too_many(60)
    user = User.objects.filter(username=email, is_active=True).first()
    if user is not None:
        try:
            if services.seconds_until_resend(email, EmailCode.Purpose.RESET) == 0:
                services.send_code(email, EmailCode.Purpose.RESET, user)
        except services.EmailSendError as exc:
            return err(str(exc), "email_failed", 503)
    # Same answer whether or not the address has an account.
    return Response({"verification_required": True, "purpose": "reset", "email": email, "resend_in": settings.EMAIL_CODE_RESEND_SECONDS})


@api_view(["POST"])
@public
def password_reset(request):
    if not ip_ok(request, "reset", 30):
        return too_many()
    email = clean_email(request.data.get("email"))
    password = str(request.data.get("new_password") or "")
    if email is None:
        return err("Something went wrong. Please start again.")
    user = User.objects.filter(username=email, is_active=True).first()
    if problem := password_problem(password, user):
        return err(problem, "weak_password")
    if ratelimit.blocked(f"verify-fail:{email}", 15):
        return too_many(15)
    try:
        services.check_code(email, EmailCode.Purpose.RESET, request.data.get("code"))
    except services.CodeError as exc:
        ratelimit.hit(f"verify-fail:{email}", 15, 900)
        return err(str(exc), "bad_code")
    if user is None:
        return err("Something went wrong. Please start again.")
    user.set_password(password)
    user.save(update_fields=["password"])
    # A reset means the old password may be known to someone else: forget every remembered device and sign out
    # the other browsers.
    TrustedDevice.objects.filter(user=user).delete()
    from django.contrib.sessions.models import Session

    for session in Session.objects.filter(expire_date__gt=timezone.now()):
        if session.get_decoded().get("_auth_user_id") == str(user.pk):
            session.delete()
    ratelimit.reset(f"login-fail:{email}")
    return Response({"ok": True})


# ------------------------------------------------------------------ subscription

PLANS = [
    {
        "id": "free",
        "name": "Free",
        "price": "Rs 0",
        "bills": lambda: settings.FREE_BILL_LIMIT,
        "features": lambda: [f"{settings.FREE_BILL_LIMIT} bills in total", "Voice and barcode billing", "Stock alerts", "Share bills on WhatsApp"],
    },
    {
        "id": "pro",
        "name": "Pro",
        "price": lambda: settings.PRO_PRICE_LABEL,
        "bills": lambda: None,
        "features": lambda: [f"Unlimited bills for {settings.PRO_DAYS} days", "Everything in Free", "Renew any time", "Priority support"],
    },
]


def _resolve(value):
    return value() if callable(value) else value


def payment_state(user):
    """The customer's latest payment request, so the app can show "under review" / "rejected"."""
    last = user.payment_requests.select_related("method").first()
    if last is None:
        return None
    return {
        "id": last.id,
        "status": last.status,
        "method": last.method.name,
        "created_at": last.created_at.isoformat(),
        "note": last.admin_note if last.status == PaymentRequest.Status.REJECTED else "",
    }


@api_view(["GET"])
def subscription(request):
    shop = request.user.shop
    return Response(
        {
            **shop_json(shop),
            "payment": payment_state(request.user),
            "plans": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "price": _resolve(p["price"]),
                    "bills": _resolve(p["bills"]),
                    "features": _resolve(p["features"]),
                }
                for p in PLANS
            ],
        }
    )


# ------------------------------------------------------------------ paying for Pro by hand

@api_view(["GET"])
def payment_methods(request):
    """What the Upgrade window lists: where to send the money, grouped Pakistan / International."""
    rows = PaymentMethod.objects.filter(is_active=True)

    def one(m):
        return {
            "key": m.key,
            "name": m.name,
            "group": m.group,
            "account_title": m.account_title,
            "number_label": m.number_label,
            "account_number": m.account_number,
            "note": m.note,
            "payer_field": m.payer_field,
        }

    return Response({"amount": settings.PRO_PRICE_LABEL, "methods": [one(m) for m in rows]})


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def submit_payment(request):
    """"I have paid": screenshots (at least one, several allowed) + optional account and transaction id."""
    user, shop = request.user, request.user.shop
    if shop.plan == shop.Plan.PRO and shop.pro_until is None:
        return err("Your shop is on the Pro plan with no end date.", "already_pro")  # paying again to extend is fine
    if not ratelimit.hit(f"payment:{user.pk}", 5, 3600):
        return too_many(60)
    if user.payment_requests.filter(status=PaymentRequest.Status.PENDING).exists():
        return err("Your last payment is still being checked. Please wait for our email.", "payment_pending", 409)
    method = PaymentMethod.objects.filter(key=str(request.data.get("method") or ""), is_active=True).first()
    if method is None:
        return err("Please choose a payment method.")
    payer = str(request.data.get("payer_account") or "").strip()[:100]
    if payer and method.payer_field == PaymentMethod.PayerField.EMAIL and not clean_email(payer):
        return err("That email address does not look right.")
    if payer and method.payer_field == PaymentMethod.PayerField.MOBILE and not (
        payer.replace(" ", "").replace("-", "").replace("+", "").isdigit() and 10 <= len(payer.replace(" ", "").replace("-", "").replace("+", "")) <= 15
    ):
        return err("That mobile number does not look right.")
    txn = str(request.data.get("transaction_id") or "").strip()[:64]
    if txn and not all(ch.isalnum() or ch in "-_/. " for ch in txn):
        return err("The transaction ID can only have letters, numbers and - _ / .")
    try:
        files = payments.clean_files(request.FILES.getlist("screenshots"))
    except payments.ProofError as exc:
        return err(str(exc), "bad_proof")

    with transaction.atomic():
        payment = PaymentRequest.objects.create(
            shop=shop, user=user, method=method, amount_label=settings.PRO_PRICE_LABEL,
            payer_account=payer, transaction_id=txn, site_url=emailing.frontend_url(request),
        )
        for f, mime in files:
            PaymentProof.objects.create(request=payment, file=f, content_type=mime, size=f.size)
    emailed = True
    try:
        payments.notify_admin(payment)
    except Exception:  # noqa: BLE001 - the request is saved; the admin still sees it in /admin/
        log.exception("Could not email the admin about payment #%s", payment.pk)
        emailed = False
    return Response({"id": payment.id, "status": payment.status, "emailed": emailed}, status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])  # staff (admin accounts) have no shop, so no HasShop here
def payment_proof(request, payment_id, index):
    """A screenshot, shown only to the person who uploaded it and to staff (never a public link)."""
    payment = PaymentRequest.objects.filter(pk=payment_id).first()
    if payment is None or not (request.user.is_staff or payment.user_id == request.user.pk):
        return err("Not found.", http=404)
    proofs = list(payment.proofs.all())
    if not (0 <= index < len(proofs)):
        return err("Not found.", http=404)
    proof = proofs[index]
    response = FileResponse(proof.file.open("rb"), content_type=proof.content_type)
    response["Cache-Control"] = "private, no-store"
    return response
