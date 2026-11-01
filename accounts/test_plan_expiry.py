from datetime import timedelta

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from shop.models import Notification, Shop
from shop.testutils import make_account

from . import payments, plans
from .models import PaymentMethod, PaymentRequest


class ProExpiryTests(TestCase):
    def setUp(self):
        self.user, self.shop = make_account("pro@example.com", plan=Shop.Plan.FREE)
        self.method = PaymentMethod.objects.get(key="jazzcash")

    def pay_and_approve(self):
        p = PaymentRequest.objects.create(shop=self.shop, user=self.user, method=self.method, site_url="https://app.example.com")
        payments.review(p, True)
        self.shop.refresh_from_db()

    def test_approval_gives_30_days(self):
        self.pay_and_approve()
        self.assertEqual(self.shop.plan, "pro")
        self.assertAlmostEqual((self.shop.pro_until - timezone.now()).total_seconds(), 30 * 86400, delta=60)
        self.assertEqual(self.shop.days_left, 30)
        self.assertTrue(Notification.objects.filter(shop=self.shop, kind="plan_active").exists())
        self.assertIn("Active until", mail.outbox[-1].alternatives[0][0])

    def test_paying_early_adds_on_top_of_what_is_left(self):
        self.pay_and_approve()
        first = self.shop.pro_until
        self.pay_and_approve()
        self.assertAlmostEqual((self.shop.pro_until - first).total_seconds(), 30 * 86400, delta=5)

    def test_paying_after_it_ended_starts_from_today(self):
        self.pay_and_approve()
        Shop.objects.filter(pk=self.shop.pk).update(pro_until=timezone.now() - timedelta(days=3))
        self.shop.refresh_from_db()
        plans.downgrade_if_due(self.shop)
        self.pay_and_approve()
        self.assertEqual(self.shop.days_left, 30)

    def test_expired_pro_is_downgraded_with_notice_and_email_once(self):
        self.pay_and_approve()
        Shop.objects.filter(pk=self.shop.pk).update(pro_until=timezone.now() - timedelta(minutes=1))
        self.shop.refresh_from_db()
        mail.outbox.clear()
        self.assertTrue(plans.downgrade_if_due(self.shop))
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.plan, "free")
        n = Notification.objects.get(shop=self.shop, kind="plan_expired")
        self.assertIn("Upgrade", n.message)
        m = mail.outbox[-1]
        self.assertEqual(m.to, ["pro@example.com"])
        self.assertIn("https://app.example.com/plans", m.alternatives[0][0])
        self.assertIn("Select a payment method", m.alternatives[0][0])
        self.assertFalse(plans.downgrade_if_due(self.shop))  # not twice
        self.assertEqual(plans.downgrade_expired(), 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_next_request_switches_it_back_to_free(self):
        from rest_framework.test import APIClient

        self.pay_and_approve()
        Shop.objects.filter(pk=self.shop.pk).update(pro_until=timezone.now() - timedelta(seconds=5))
        c = APIClient()
        c.force_authenticate(type(self.user).objects.get(pk=self.user.pk))
        info = c.get("/api/auth/subscription/").json()
        self.assertEqual((info["plan"], info["days_left"]), ("free", None))
        feed = c.get("/api/notifications/").json()
        self.assertEqual([n["kind"] for n in feed["items"]], ["plan_expired", "plan_active"])
        self.assertEqual(feed["unread"], 2)

    def test_background_job_downgrades_even_if_nobody_opens_the_app(self):
        from shop import monitor

        self.pay_and_approve()
        Shop.objects.filter(pk=self.shop.pk).update(pro_until=timezone.now() - timedelta(hours=1))
        monitor.run_checks()
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.plan, "free")

    def test_pro_without_an_end_date_never_expires(self):
        Shop.objects.filter(pk=self.shop.pk).update(plan="pro", pro_until=None)
        self.shop.refresh_from_db()
        self.assertFalse(plans.downgrade_if_due(self.shop))
        self.assertIsNone(self.shop.days_left)

    def test_free_limit_applies_again_after_the_downgrade(self):
        Shop.objects.filter(pk=self.shop.pk).update(plan="pro", pro_until=timezone.now() - timedelta(minutes=1), bills_generated=40)
        self.shop.refresh_from_db()
        plans.downgrade_if_due(self.shop)
        self.assertTrue(self.shop.at_limit)  # 40 bills made, free allows 10: must pay again

    def test_renewing_is_allowed_but_lifetime_pro_is_not_asked_to_pay(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rest_framework.test import APIClient

        self.pay_and_approve()
        c = APIClient()
        c.force_authenticate(type(self.user).objects.get(pk=self.user.pk))
        shot = lambda: SimpleUploadedFile("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 50, content_type="image/png")
        self.assertEqual(c.post("/api/auth/payments/", {"method": "jazzcash", "screenshots": [shot()]}, format="multipart").status_code, 201)
        Shop.objects.filter(pk=self.shop.pk).update(pro_until=None)
        PaymentRequest.objects.update(status="approved")
        c2 = APIClient()
        c2.force_authenticate(type(self.user).objects.get(pk=self.user.pk))
        self.assertEqual(c2.post("/api/auth/payments/", {"method": "jazzcash", "screenshots": [shot()]}, format="multipart").json()["code"], "already_pro")

    def test_set_plan_command_gives_30_days_by_default(self):
        from django.core.management import call_command

        call_command("set_plan", "pro@example.com", "pro")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.days_left, 30)
        call_command("set_plan", "pro@example.com", "pro", "--forever")
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.pro_until)
