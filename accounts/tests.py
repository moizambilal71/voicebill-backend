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

from .models import EmailCode

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
    def test_free_plan_info_and_payment_not_connected(self):
        self.sign_up_and_verify()
        info = self.c.get("/api/auth/subscription/").json()
        self.assertEqual((info["plan"], info["bill_limit"], info["bills_left"]), ("free", 10, 10))
        self.assertEqual([p["id"] for p in info["plans"]], ["free", "pro"])
        res = self.post("/api/auth/subscription/upgrade/")
        self.assertEqual((res.status_code, res.json()["code"]), (501, "payment_not_connected"))
        self.assertEqual(Shop.objects.get(owner__username="a@example.com").plan, "free")  # cannot self-upgrade

    @override_settings(DEMO_UPGRADE=True)
    def test_demo_upgrade_switches_to_pro(self):
        self.sign_up_and_verify()
        self.assertEqual(self.post("/api/auth/subscription/upgrade/").json()["plan"], "pro")
