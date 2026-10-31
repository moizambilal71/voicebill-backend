from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Shop, create_starter_data


def make_account(email="owner@example.com", plan=Shop.Plan.PRO):
    """A signed-up shopkeeper with a shop. Pro by default so tests are not stopped by the free-plan limit."""
    user = get_user_model().objects.create_user(username=email, email=email, password="Str0ng-pass-123")
    shop = Shop.objects.create(owner=user, name="Test shop", plan=plan)
    create_starter_data(shop)
    return user, shop


class ShopTestCase(TestCase):
    """Every test runs as a signed-in shopkeeper: self.user, self.shop and (via make_client) an API client."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.shop = make_account()

    def make_client(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.user)
        return client
