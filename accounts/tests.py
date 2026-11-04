import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from shop.models import Bill, Product, Shop

from .models import EmailCode, PaymentRequest

User = get_user_model()
PW = "Str0ng-pass-123"


def last_code():
    return re.search(r"\n\s+(\d{6})\n", mail.outbox[-1].body).group(1)


class AuthBase(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient(enforce_csrf_checks=True)
        self.c.get("/api/auth/csrf/")
        self.token = self.c.cookies["csrftoken"].value

    def post(self, path, data=None, client=None):
        client = client or self.c
        res = client.post(path, data or {}, format="json", HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        return res

    def signup(self, email="a@example.com", **extra):
        return self.post("/api/auth/signup/", {"name": "Ali", "email": email, "password": PW, **extra})

    def sign_up_and_verify(self, email="a@example.com", **extra):
        self.assertEqual(self.signup(email, **extra).status_code, 200)
        return self.post("/api/auth/verify/", {"email": email, "purpose": "signup", "code": last_code()})


class SignupTests(AuthBase):
    def test_signup_needs_the_emailed_code_and_then_signs_in_with_a_shop(self):
        res = self.signup(shop_name="Hadi Store")
        self.assertTrue(res.json()["verification_required"])
        user = User.objects.get(username="a@example.com")
        self.assertFalse(user.is_active)  # not usable until the email is proven
        self.assertEqual(self.c.get("/api/auth/me/").status_code, 401)
        res = self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": last_code()})
        self.assertEqual(res.status_code, 200, res.content)
        me = self.c.get("/api/auth/me/").json()
        self.assertEqual((me["email"], me["shop"]["plan"], me["shop"]["bill_limit"]), ("a@example.com", "free", 10))
        self.assertEqual(me["shop"]["name"], "Hadi Store")
        shop = Shop.objects.get(owner__username="a@example.com")
        self.assertTrue(shop.templates.count() >= 7)  # starter designs
        self.assertEqual(shop.settings.shop_name, "Hadi Store")

    def test_wrong_code_is_counted_and_the_code_dies_after_five_tries(self):
        self.signup()
        good = last_code()
        bad = "000000" if good != "000000" else "111111"
        for _ in range(4):
            self.assertEqual(self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": bad}).status_code, 400)
        last = self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": bad})
        self.assertIn("new code", last.json()["detail"])
        # even the right code no longer works
        self.assertEqual(self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": good}).status_code, 400)

    def test_code_expires_and_works_only_once(self):
        self.signup()
        code = last_code()
        EmailCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": code}).status_code, 400)
        self.post("/api/auth/resend/", {"email": "a@example.com", "purpose": "signup"})  # too soon: ignored
        EmailCode.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.post("/api/auth/resend/", {"email": "a@example.com", "purpose": "signup"})
        code2 = last_code()
        self.assertEqual(self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": code2}).status_code, 200)
        self.assertEqual(self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": code2}).status_code, 400)

    def test_new_code_replaces_the_old_one(self):
        self.signup()
        old = last_code()
        EmailCode.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.post("/api/auth/resend/", {"email": "a@example.com", "purpose": "signup"})
        new = last_code()
        if old != new:
            self.assertEqual(self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "signup", "code": old}).status_code, 400)

    def test_codes_are_stored_hashed(self):
        self.signup()
        self.assertNotIn(last_code(), EmailCode.objects.get().code_hash)

    def test_weak_password_and_bad_email_are_refused(self):
        self.assertEqual(self.post("/api/auth/signup/", {"name": "A", "email": "a@example.com", "password": "12345678"}).json()["code"], "weak_password")
        self.assertEqual(self.post("/api/auth/signup/", {"name": "A", "email": "nope", "password": PW}).status_code, 400)
        self.assertEqual(self.post("/api/auth/signup/", {"name": "", "email": "a@example.com", "password": PW}).status_code, 400)

    def test_existing_account_is_not_revealed(self):
        self.sign_up_and_verify()
        self.post("/api/auth/logout/")
        mail.outbox.clear()
        res = self.signup()
        self.assertTrue(res.json()["verification_required"])  # same answer as for a new address
        self.assertIn("already has a VoiceBill account", mail.outbox[-1].body)
        self.assertEqual(User.objects.filter(username="a@example.com").count(), 1)

    def test_email_is_case_insensitive(self):
        self.signup("Ali@Example.com")
        self.assertTrue(User.objects.filter(username="ali@example.com").exists())

    def test_sign_up_without_csrf_token_is_refused(self):
        raw = APIClient(enforce_csrf_checks=True)
        self.assertEqual(raw.post("/api/auth/signup/", {"name": "A", "email": "a@example.com", "password": PW}, format="json").status_code, 403)


class LoginTests(AuthBase):
    def setUp(self):
        super().setUp()
        self.sign_up_and_verify()
        self.post("/api/auth/logout/")

    def test_logout_ends_the_session(self):
        self.assertEqual(self.c.get("/api/auth/me/").status_code, 401)
        self.assertEqual(self.c.get("/api/products/").status_code, 401)

    def test_trusted_device_skips_the_code(self):
        res = self.post("/api/auth/login/", {"email": "a@example.com", "password": PW})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["user"]["email"], "a@example.com")  # the sign-up device is remembered

    def test_new_device_needs_a_code(self):
        other = APIClient(enforce_csrf_checks=True)
        other.get("/api/auth/csrf/")
        self.token = other.cookies["csrftoken"].value
        mail.outbox.clear()
        res = self.post("/api/auth/login/", {"email": "a@example.com", "password": PW}, client=other)
        self.assertEqual(res.json()["purpose"], "login")
        self.assertEqual(other.get("/api/auth/me/").status_code, 401)  # password alone is not enough
        res = self.post("/api/auth/verify/", {"email": "a@example.com", "purpose": "login", "code": last_code(), "trust_device": True}, client=other)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(other.get("/api/auth/me/").status_code, 200)

    def test_wrong_password_and_unknown_email_look_the_same(self):
        a = self.post("/api/auth/login/", {"email": "a@example.com", "password": "nope-nope-1"})
        b = self.post("/api/auth/login/", {"email": "ghost@example.com", "password": "nope-nope-1"})
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        self.assertEqual(a.status_code, 400)

    def test_account_locks_after_repeated_wrong_passwords(self):
        for _ in range(8):
            self.post("/api/auth/login/", {"email": "a@example.com", "password": "nope-nope-1"})
        res = self.post("/api/auth/login/", {"email": "a@example.com", "password": PW})
        self.assertEqual(res.status_code, 429)

    def test_signup_code_requests_are_limited_per_email(self):
        cache.clear()
        for _ in range(5):
            self.signup("b@example.com")
        self.assertEqual(self.signup("b@example.com").status_code, 429)

    def test_verify_guessing_is_limited(self):
        self.signup("c@example.com")
        codes = [self.post("/api/auth/verify/", {"email": "c@example.com", "purpose": "signup", "code": "123456"}).status_code for _ in range(18)]
        self.assertIn(429, codes)


class PasswordResetTests(AuthBase):
    def setUp(self):
        super().setUp()
        self.sign_up_and_verify()
        self.post("/api/auth/logout/")

    def test_reset_with_code_changes_password_and_forgets_devices(self):
        res = self.post("/api/auth/password/forgot/", {"email": "a@example.com"})
        self.assertEqual(res.status_code, 200)
        code = last_code()
        self.assertEqual(self.post("/api/auth/password/reset/", {"email": "a@example.com", "code": code, "new_password": "short"}).status_code, 400)
        self.assertEqual(self.post("/api/auth/password/reset/", {"email": "a@example.com", "code": code, "new_password": "Another-pass-456"}).status_code, 200)
        self.assertEqual(self.post("/api/auth/login/", {"email": "a@example.com", "password": PW}).status_code, 400)
        mail.outbox.clear()
        res = self.post("/api/auth/login/", {"email": "a@example.com", "password": "Another-pass-456"})
        self.assertEqual(res.json().get("purpose"), "login")  # remembered devices were wiped, so a code is needed

    def test_unknown_email_gets_the_same_answer_and_no_email(self):
        mail.outbox.clear()
        res = self.post("/api/auth/password/forgot/", {"email": "ghost@example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


class GoogleTests(AuthBase):
    claims = {"sub": "g-123", "email": "G@Example.com", "email_verified": True, "name": "Gul"}

    def test_not_configured_is_a_clear_error(self):
        with self.settings(GOOGLE_CLIENT_ID=""):
            res = self.post("/api/auth/google/", {"credential": "x"})
        self.assertEqual((res.status_code, res.json()["code"]), (503, "google_not_configured"))

    def test_bad_token_is_refused(self):
        from google.auth.exceptions import TransportError
        with self.settings(GOOGLE_CLIENT_ID="cid"), patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("bad")):
            self.assertEqual(self.post("/api/auth/google/", {"credential": "garbage"}).status_code, 400)
        with self.settings(GOOGLE_CLIENT_ID="cid"), patch("google.oauth2.id_token.verify_oauth2_token", side_effect=TransportError("down")):
            self.assertEqual(self.post("/api/auth/google/", {"credential": "garbage"}).status_code, 503)

    def test_new_google_user_gets_an_account_without_a_code(self):
        with patch("accounts.services.verify_google_token", return_value=self.claims):
            res = self.post("/api/auth/google/", {"credential": "tok"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["created"])
        me = self.c.get("/api/auth/me/").json()
        self.assertEqual((me["email"], me["google"], me["shop"]["plan"]), ("g@example.com", True, "free"))
        self.assertFalse(User.objects.get(username="g@example.com").has_usable_password())

    def test_same_google_user_signs_in_again_to_the_same_account(self):
        with patch("accounts.services.verify_google_token", return_value=self.claims):
            self.post("/api/auth/google/", {"credential": "tok"})
            res = self.post("/api/auth/google/", {"credential": "tok"})
        self.assertFalse(res.json()["created"])
        self.assertEqual(User.objects.count(), 1)

    def test_unverified_google_email_is_refused(self):
        with patch("accounts.services.verify_google_token", return_value={**self.claims, "email_verified": False}):
            self.assertEqual(self.post("/api/auth/google/", {"credential": "tok"}).json()["code"], "google_unverified")

    def test_google_takes_over_an_unverified_password_signup_safely(self):
        self.signup("g@example.com")  # someone started a sign-up with this address and never verified it
        with patch("accounts.services.verify_google_token", return_value=self.claims):
            self.assertEqual(self.post("/api/auth/google/", {"credential": "tok"}).status_code, 200)
        user = User.objects.get(username="g@example.com")
        self.assertTrue(user.is_active)
        self.assertFalse(user.check_password(PW))  # the password chosen before ownership was proven is gone


class LegacyShopTests(AuthBase):
    def test_first_account_takes_over_existing_data_and_the_second_does_not(self):
        legacy = Shop.objects.create(name="Old shop", plan="pro", bills_generated=13)
        Product.objects.create(shop=legacy, name="Sugar")
        self.sign_up_and_verify("first@example.com")
        legacy.refresh_from_db()
        self.assertEqual(legacy.owner.username, "first@example.com")
        self.assertEqual(self.c.get("/api/products/").json()[0]["name"], "Sugar")
        d = APIClient(enforce_csrf_checks=True)
        d.get("/api/auth/csrf/")
        self.token = d.cookies["csrftoken"].value
        self.post("/api/auth/signup/", {"name": "B", "email": "b@example.com", "password": PW}, client=d)
        self.post("/api/auth/verify/", {"email": "b@example.com", "purpose": "signup", "code": last_code()}, client=d)
        self.assertEqual(d.get("/api/products/").json(), [])  # b sees nothing of the first shop

    @override_settings(LEGACY_SHOP_OWNER_EMAIL="boss@example.com")
    def test_only_the_named_email_can_claim_it(self):
        legacy = Shop.objects.create(name="Old shop")
        self.sign_up_and_verify("stranger@example.com")
        legacy.refresh_from_db()
        self.assertIsNone(legacy.owner)


class SubscriptionTests(AuthBase):
    def test_free_plan_info(self):
        self.sign_up_and_verify()
        info = self.c.get("/api/auth/subscription/").json()
        self.assertEqual((info["plan"], info["bill_limit"], info["bills_left"]), ("free", 10, 10))
        self.assertEqual([p["id"] for p in info["plans"]], ["free", "pro"])
        self.assertIsNone(info["payment"])
        self.assertEqual(self.c.post("/api/auth/subscription/upgrade/", {}, format="json", HTTP_X_CSRFTOKEN=self.token).status_code, 404)  # no self-upgrade


def png(size=200):
    return b"\x89PNG\r\n\x1a\n" + b"0" * size


class PaymentTests(AuthBase):
    def setUp(self):
        super().setUp()
        self.sign_up_and_verify()
        self.shop = Shop.objects.get(owner__username="a@example.com")
        mail.outbox.clear()

    def pay(self, files=None, **data):
        from django.core.files.uploadedfile import SimpleUploadedFile
        body = {"method": "jazzcash", **data}
        if files is None:
            files = [SimpleUploadedFile("a.png", png(), content_type="image/png")]
        body["screenshots"] = files
        token = self.c.cookies["csrftoken"].value
        return self.c.post("/api/auth/payments/", body, format="multipart", HTTP_X_CSRFTOKEN=token)

    def test_methods_are_grouped_and_seeded(self):
        res = self.c.get("/api/auth/payment-methods/").json()
        names = {m["name"] for m in res["methods"]}
        self.assertEqual(names, {"JazzCash", "EasyPaisa", "Neopay", "SadaPay", "UBL Bank", "HBL Bank", "Payoneer", "PayPal", "Wise"})
        self.assertEqual({m["group"] for m in res["methods"]}, {"local", "international"})
        self.assertEqual({m["name"]: m["payer_field"] for m in res["methods"]}["PayPal"], "email")

    def test_submit_creates_request_and_emails_admin_with_screenshots(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        files = [SimpleUploadedFile(f"{i}.png", png(), content_type="image/png") for i in range(2)]
        with self.settings(ADMIN_NOTIFY_EMAIL="boss@example.com"):
            res = self.pay(files, payer_account="0300 1234567", transaction_id="TX-99")
        self.assertEqual(res.status_code, 201, res.content)
        p = PaymentRequest.objects.get()
        self.assertEqual((p.status, p.transaction_id, p.payer_account, p.proofs.count()), ("pending", "TX-99", "0300 1234567", 2))
        m = mail.outbox[-1]
        self.assertEqual(m.to, ["boss@example.com"])
        self.assertEqual(len(m.attachments), 2)
        html = m.alternatives[0][0]
        self.assertIn(f"/admin/accounts/paymentrequest/{p.pk}/change/", html)
        self.assertIn("TX-99", m.body)
        self.assertIn("a@example.com", m.body)
        info = self.c.get("/api/auth/subscription/").json()
        self.assertEqual(info["payment"]["status"], "pending")
        self.assertEqual(info["plan"], "free")  # nothing changes until an admin approves

    def test_at_least_one_screenshot_is_required(self):
        self.assertEqual(self.pay([]).json()["code"], "bad_proof")

    def test_only_real_images_and_sane_sizes(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        fake = SimpleUploadedFile("a.png", b"<html>not an image</html>", content_type="image/png")
        self.assertEqual(self.pay([fake]).status_code, 400)
        big = SimpleUploadedFile("b.png", png(4 * 1024 * 1024), content_type="image/png")
        self.assertIn("too big", self.pay([big]).json()["detail"])
        many = [SimpleUploadedFile(f"{i}.png", png(), content_type="image/png") for i in range(7)]
        self.assertEqual(self.pay(many).status_code, 400)
        self.assertEqual(PaymentRequest.objects.count(), 0)

    def test_bad_method_and_bad_details(self):
        self.assertEqual(self.pay(method="bitcoin").status_code, 400)
        self.assertEqual(self.pay(method="paypal", payer_account="not-an-email").status_code, 400)
        self.assertEqual(self.pay(payer_account="abc").status_code, 400)
        self.assertEqual(self.pay(transaction_id="<script>").status_code, 400)

    def test_only_one_pending_request_at_a_time(self):
        self.assertEqual(self.pay().status_code, 201)
        res = self.pay()
        self.assertEqual((res.status_code, res.json()["code"]), (409, "payment_pending"))

    def test_request_is_kept_even_if_the_admin_email_fails(self):
        with patch("accounts.payments.notify_admin", side_effect=RuntimeError("smtp down")):
            res = self.pay()
        self.assertEqual(res.status_code, 201)
        self.assertFalse(res.json()["emailed"])
        self.assertEqual(PaymentRequest.objects.count(), 1)

    def test_pro_shop_cannot_pay_again(self):
        Shop.objects.filter(pk=self.shop.pk).update(plan="pro")
        self.assertEqual(self.pay().json()["code"], "already_pro")

    def test_signed_out_cannot_submit(self):
        self.assertEqual(APIClient().post("/api/auth/payments/", {}).status_code, 401)

    def test_screenshots_are_private(self):
        self.pay()
        p = PaymentRequest.objects.get()
        url = f"/api/auth/payments/{p.pk}/proof/0/"
        self.assertEqual(self.c.get(url).status_code, 200)  # the owner
        self.assertEqual(self.c.get(url).headers["Content-Type"], "image/png")
        self.assertEqual(APIClient().get(url).status_code, 401)
        other = User.objects.create_user("o@example.com", "o@example.com", PW)
        shop2 = Shop.objects.create(owner=other)
        c2 = APIClient(); c2.force_authenticate(other)
        self.assertEqual(c2.get(url).status_code, 404)  # another customer
        staff = User.objects.create_user("s@example.com", "s@example.com", PW, is_staff=True)  # no shop at all
        c3 = APIClient(); c3.force_authenticate(staff)
        self.assertEqual(c3.get(url).status_code, 200)
        self.assertEqual(c3.get(f"/api/auth/payments/{p.pk}/proof/5/").status_code, 404)
        del shop2


class ReviewTests(AuthBase):
    def setUp(self):
        super().setUp()
        self.sign_up_and_verify()
        self.shop = Shop.objects.get(owner__username="a@example.com")
        self.user = self.shop.owner
        from .models import PaymentMethod
        self.p = PaymentRequest.objects.create(shop=self.shop, user=self.user, method=PaymentMethod.objects.get(key="jazzcash"), transaction_id="T1")
        mail.outbox.clear()

    def test_approve_turns_on_pro_and_emails_the_signin_address(self):
        from . import payments
        payments.review(self.p, True)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.plan, "pro")
        self.assertEqual(mail.outbox[-1].to, ["a@example.com"])
        self.assertIn("Pro plan is active", mail.outbox[-1].subject)
        self.assertIn("now active", mail.outbox[-1].body)
        self.assertIsNone(self.shop.bill_limit)

    @override_settings(ADMIN_WHATSAPP_NUMBER="923001234567")
    def test_reject_keeps_free_and_sends_whatsapp_link(self):
        from . import payments
        payments.review(self.p, False, note="Amount did not match")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.plan, "free")
        m = mail.outbox[-1]
        self.assertIn("https://wa.me/923001234567?text=", m.body)
        self.assertIn("Amount did not match", m.body)
        self.assertIn("T1", m.body)

    def test_a_decision_cannot_be_made_twice(self):
        from . import payments
        payments.review(self.p, False)
        with self.assertRaises(payments.ReviewError):
            payments.review(self.p, True)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.plan, "free")

    def test_admin_status_change_does_the_whole_approval(self):
        admin = User.objects.create_superuser("root@example.com", "root@example.com", PW)
        c = APIClient(); c.force_login(admin)
        page = c.get(f"/admin/accounts/paymentrequest/{self.p.pk}/change/")
        self.assertEqual(page.status_code, 200)
        res = c.post(f"/admin/accounts/paymentrequest/{self.p.pk}/change/", {"status": "approved", "admin_note": "", "proofs-TOTAL_FORMS": 0, "proofs-INITIAL_FORMS": 0, "proofs-MIN_NUM_FORMS": 0, "proofs-MAX_NUM_FORMS": 1000})
        self.assertEqual(res.status_code, 302, res.content[:300])
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.plan, "pro")
        self.p.refresh_from_db()
        self.assertEqual(self.p.reviewed_by, admin)
        self.assertEqual(mail.outbox[-1].to, ["a@example.com"])

    def test_admin_bulk_reject_action(self):
        admin = User.objects.create_superuser("root@example.com", "root@example.com", PW)
        c = APIClient(); c.force_login(admin)
        res = c.post("/admin/accounts/paymentrequest/", {"action": "reject_selected", "_selected_action": [self.p.pk]})
        self.assertEqual(res.status_code, 302)
        self.p.refresh_from_db()
        self.assertEqual(self.p.status, "rejected")


class BrevoBackendTests(TestCase):
    def test_posts_to_brevo_with_the_key_and_never_leaks_it(self):
        from unittest.mock import MagicMock
        from django.core.mail import send_mail
        ok = MagicMock(status_code=201)
        with self.settings(EMAIL_BACKEND="accounts.brevo.BrevoEmailBackend", BREVO_API_KEY="k-123",
                           DEFAULT_FROM_EMAIL="VoiceBill <me@example.com>"), \
                patch("accounts.brevo.requests.post", return_value=ok) as post:
            self.assertEqual(send_mail("Subj", "Body 123456", None, ["to@example.com"]), 1)
        kw = post.call_args.kwargs
        self.assertEqual(kw["headers"]["api-key"], "k-123")
        self.assertEqual(kw["json"]["sender"], {"email": "me@example.com", "name": "VoiceBill"})
        self.assertEqual(kw["json"]["to"], [{"email": "to@example.com"}])

    def test_html_and_attachments_are_passed_on(self):
        from unittest.mock import MagicMock
        from django.core.mail import EmailMultiAlternatives
        ok = MagicMock(status_code=201)
        with self.settings(EMAIL_BACKEND="accounts.brevo.BrevoEmailBackend", BREVO_API_KEY="k", DEFAULT_FROM_EMAIL="me@example.com"), \
                patch("accounts.brevo.requests.post", return_value=ok) as post:
            m = EmailMultiAlternatives("S", "plain", None, ["to@example.com"], reply_to=["r@example.com"])
            m.attach_alternative("<b>hi</b>", "text/html")
            m.attach("shot.png", b"abc", "image/png")
            m.send()
        j = post.call_args.kwargs["json"]
        self.assertEqual(j["htmlContent"], "<b>hi</b>")
        self.assertEqual(j["attachment"], [{"name": "shot.png", "content": "YWJj"}])
        self.assertEqual(j["replyTo"], {"email": "r@example.com"})

    def test_refusal_is_an_error(self):
        from unittest.mock import MagicMock
        from django.core.mail import send_mail
        bad = MagicMock(status_code=400, text="sender not valid")
        with self.settings(EMAIL_BACKEND="accounts.brevo.BrevoEmailBackend", BREVO_API_KEY="k"), \
                patch("accounts.brevo.requests.post", return_value=bad):
            with self.assertRaises(RuntimeError):
                send_mail("S", "B", "a@example.com", ["b@example.com"])


class EmailLinkTests(AuthBase):
    def req(self, **meta):
        from django.test import RequestFactory
        return RequestFactory().post("/x", **meta)

    def test_localhost_while_testing_and_the_domain_once_deployed(self):
        from . import emailing
        with self.settings(DEBUG=True, FRONTEND_URL="http://localhost:3000"):
            self.assertEqual(emailing.frontend_url(self.req(HTTP_ORIGIN="http://localhost:3000")), "http://localhost:3000")
            self.assertEqual(emailing.frontend_url(None), "http://localhost:3000")
        with self.settings(DEBUG=False, FRONTEND_URL="https://app.example.com", ALLOWED_HOSTS=["app.example.com"], CSRF_TRUSTED_ORIGINS=[]):
            self.assertEqual(emailing.frontend_url(self.req(HTTP_ORIGIN="https://app.example.com")), "https://app.example.com")
            # localhost is not trusted in production, and unknown sites never are
            self.assertEqual(emailing.frontend_url(self.req(HTTP_ORIGIN="http://localhost:3000")), "https://app.example.com")
            self.assertEqual(emailing.frontend_url(self.req(HTTP_ORIGIN="https://evil.example.net")), "https://app.example.com")

    def test_tunnel_address_is_used_when_trusted(self):
        from . import emailing
        with self.settings(DEBUG=True):
            self.assertEqual(emailing.frontend_url(self.req(HTTP_ORIGIN="https://abc-123.ngrok-free.app")), "https://abc-123.ngrok-free.app")

    def test_referer_is_used_when_there_is_no_origin(self):
        from . import emailing
        with self.settings(DEBUG=True):
            self.assertEqual(emailing.frontend_url(self.req(HTTP_REFERER="http://127.0.0.1:3000/plans")), "http://127.0.0.1:3000")

    def test_approval_email_buttons_open_the_site_the_customer_used(self):
        from . import payments
        from .models import PaymentMethod
        self.sign_up_and_verify()
        shop = Shop.objects.get(owner__username="a@example.com")
        p = PaymentRequest.objects.create(shop=shop, user=shop.owner, method=PaymentMethod.objects.get(key="paypal"), site_url="https://app.example.com")
        mail.outbox.clear()
        payments.review(p, True)
        html = mail.outbox[-1].alternatives[0][0]
        self.assertIn('href="https://app.example.com/"', html)
        self.assertIn("Open Dashboard", html)
        self.assertIn("Create Your First Bill", html)
        self.assertIn("https://app.example.com/?new=1", html)

    def test_payment_request_remembers_the_site(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.sign_up_and_verify()
        f = SimpleUploadedFile("a.png", png(), content_type="image/png")
        with self.settings(DEBUG=True):
            self.c.post("/api/auth/payments/", {"method": "jazzcash", "screenshots": [f]}, format="multipart",
                        HTTP_X_CSRFTOKEN=self.c.cookies["csrftoken"].value, HTTP_ORIGIN="http://localhost:3000")
        self.assertEqual(PaymentRequest.objects.get().site_url, "http://localhost:3000")

    def test_code_email_has_a_designed_html_part(self):
        self.signup()
        html = mail.outbox[-1].alternatives[0][0]
        self.assertIn("Your verification code", html)
        self.assertIn(last_code(), html)


class ShoplessSessionTests(TestCase):
    def test_me_gives_a_shop_to_an_account_that_has_none(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser("root", "root@example.com", "pw-12345-xyz")
        client = APIClient()
        client.force_login(user)
        data = client.get("/api/auth/me/").json()
        self.assertIsNotNone(data["shop"])
        self.assertEqual(client.get("/api/summary/").status_code, 200)
