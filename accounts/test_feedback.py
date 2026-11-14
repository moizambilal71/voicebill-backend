from unittest.mock import patch

import httpx2 as httpx
import openai
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from shop import alerts
from shop.models import Shop

from .models import AdminAlert, Feedback

User = get_user_model()


@override_settings(ADMIN_NOTIFY_EMAIL="owner@example.com")
class FeedbackTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("u@example.com", "u@example.com", "Str0ng-pass-123", first_name="Ali")
        Shop.objects.create(owner=self.user)
        self.c = APIClient()
        self.c.force_authenticate(self.user)

    def test_saved_and_emailed(self):
        r = self.c.post("/api/auth/feedback/", {"overall": 4, "ease": 5, "voice": 3, "recommend": "yes", "most_used": ["Voice billing"], "problem": "Printing", "message": "Great app"}, format="json")
        self.assertEqual(r.status_code, 201)
        fb = Feedback.objects.get()
        self.assertEqual((fb.user, fb.overall, fb.message, fb.most_used), (self.user, 4, "Great app", "Voice billing"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])
        self.assertIn("Great app", mail.outbox[0].body)

    def test_needs_ratings(self):
        r = self.c.post("/api/auth/feedback/", {"overall": 9, "ease": 5}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Feedback.objects.count(), 0)

    def test_login_required(self):
        self.assertIn(APIClient().post("/api/auth/feedback/", {"overall": 4, "ease": 4}, format="json").status_code, (401, 403))

    def test_limit_per_hour(self):
        for _ in range(5):
            self.assertEqual(self.c.post("/api/auth/feedback/", {"overall": 4, "ease": 4}, format="json").status_code, 201)
        self.assertEqual(self.c.post("/api/auth/feedback/", {"overall": 4, "ease": 4}, format="json").status_code, 429)


def _err(cls, message, code=None):
    req = httpx.Request("POST", "https://api.openai.com/v1/x")
    resp = httpx.Response(429 if cls is openai.RateLimitError else 401 if cls is openai.AuthenticationError else 404, request=req)
    return cls(message, response=resp, body={"code": code} if code else None)


@override_settings(ADMIN_NOTIFY_EMAIL="owner@example.com")
class OpenAIAlertTests(TestCase):
    def test_credits_finished_emails_once(self):
        exc = _err(openai.RateLimitError, "You exceeded your current quota, please check your plan and billing details.", "insufficient_quota")
        self.assertTrue(alerts.notify_if_needed(exc))
        self.assertFalse(alerts.notify_if_needed(exc))  # throttled
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("credits", mail.outbox[0].subject.lower())
        self.assertEqual(AdminAlert.objects.get().kind, "openai_credits")

    def test_bad_key(self):
        self.assertTrue(alerts.notify_if_needed(_err(openai.AuthenticationError, "Incorrect API key provided")))
        self.assertIn("key", mail.outbox[0].subject.lower())

    def test_other_errors_do_not_email(self):
        self.assertFalse(alerts.notify_if_needed(ValueError("boom")))
        self.assertEqual(len(mail.outbox), 0)

    def test_voice_endpoint_sends_alert(self):
        user = User.objects.create_user("v@example.com", "v@example.com", "Str0ng-pass-123")
        Shop.objects.create(owner=user)
        c = APIClient()
        c.force_authenticate(user)
        exc = _err(openai.RateLimitError, "insufficient_quota billing", "insufficient_quota")
        with patch("shop.views.voice_service.interpret", side_effect=exc):
            r = c.post("/api/voice/interpret/", {"text": "sugar"}, format="json")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(mail.outbox), 1)
