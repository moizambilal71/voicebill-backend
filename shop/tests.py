from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Alias, Bill, BillItem, Product, Variant


class VoiceInterpretTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.sugar = Product.objects.create(name="Sugar", unit="kg")
        self.sugar_v = Variant.objects.create(product=self.sugar, name="Standard", price=150)
        Alias.objects.create(product=self.sugar, text="cheeni")
        self.daliya = Product.objects.create(name="Daliya", unit="kg")
        self.d100 = Variant.objects.create(product=self.daliya, name="Regular", price=100)
        self.d150 = Variant.objects.create(product=self.daliya, name="Medium", price=150)
        self.d200 = Variant.objects.create(product=self.daliya, name="Premium", price=200)
        self.bill = Bill.objects.create(customer_name="Ali")

    def interpret(self, actions, text="x"):
        with patch("shop.voice._chat_json", return_value={"actions": actions}):
            res = self.client.post(
                "/api/voice/interpret/", {"text": text, "bill_id": self.bill.id}, format="json"
            )
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["actions"]

    def test_catalog_item_no_price_uses_catalog(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "cheeni", "product_id": self.sugar.id, "quantity": 0.5, "unit": "kg"}]}])
        item = a["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["variant_id"], self.sugar_v.id)
        self.assertIsNone(item["price"])
        self.assertEqual(item["list_price"], 150)
        self.assertEqual(item["quantity"], 0.5)

    def test_bargain_keeps_spoken_price(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "cheeni", "product_id": self.sugar.id, "quantity": 1, "price": 130}]}])
        item = a["items"][0]
        self.assertEqual(item["price"], 130)
        self.assertEqual(item["list_price"], 150)

    def test_variants_without_choice_ask(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "daliya", "product_id": self.daliya.id, "quantity": 1}]}])
        item = a["items"][0]
        self.assertEqual(item["status"], "needs_variant")
        self.assertEqual([o["price"] for o in item["options"]], [100, 150, 200])

    def test_spoken_price_matching_variant_picks_it(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "daliya", "product_id": self.daliya.id, "price": 150}]}])
        item = a["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["variant_id"], self.d150.id)

    def test_spoken_price_not_matching_any_variant_still_asks(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "daliya", "product_id": self.daliya.id, "price": 120}]}])
        self.assertEqual(a["items"][0]["status"], "needs_variant")

    def test_open_price_item(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "finger ring", "price": 150}]}])
        item = a["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertIsNone(item["variant_id"])
        self.assertEqual(item["price"], 150)

    def test_unknown_item_without_price_asks_price(self):
        [a] = self.interpret([{"type": "add_items", "items": [{"spoken_name": "pony"}]}])
        self.assertEqual(a["items"][0]["status"], "needs_price")

    def test_made_up_ids_are_ignored(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "ghost", "product_id": 9999, "variant_id": 9999, "price": 10}]}])
        item = a["items"][0]
        self.assertIsNone(item["product_id"])
        self.assertEqual(item["status"], "ready")

    def test_permanent_price_update_single_variant(self):
        [a] = self.interpret([{"type": "update_catalog_price", "product_id": self.sugar.id, "new_price": 160}])
        self.assertEqual(a["type"], "update_catalog_price")
        self.assertEqual(a["variant_id"], self.sugar_v.id)
        self.assertEqual(a["old_price"], 150)
        self.assertEqual(a["new_price"], 160)
        self.sugar_v.refresh_from_db()
        self.assertEqual(self.sugar_v.price, 150)  # proposal only, nothing saved

    def test_permanent_price_update_multiple_variants_needs_choice(self):
        [a] = self.interpret([{"type": "update_catalog_price", "product_id": self.daliya.id, "new_price": 160}])
        self.assertIsNone(a["variant_id"])
        self.assertEqual(len(a["options"]), 3)

    def test_price_scope_question(self):
        [a] = self.interpret([{"type": "price_scope", "product_id": self.sugar.id, "price": 130}])
        self.assertEqual(a["type"], "price_scope")
        self.assertEqual(a["price"], 130)

    def test_edit_and_remove_only_this_bills_items(self):
        item = BillItem.objects.create(bill=self.bill, item_name="Sugar", quantity=1, sold_price=150)
        other = Bill.objects.create()
        foreign = BillItem.objects.create(bill=other, item_name="X", quantity=1, sold_price=1)
        actions = self.interpret([
            {"type": "edit_item", "bill_item_id": item.id, "price": 140},
            {"type": "remove_item", "bill_item_id": foreign.id},
        ])
        self.assertEqual([a["type"] for a in actions], ["edit_item"])
        self.assertEqual(actions[0]["price"], 140)

    def test_add_product_with_categories(self):
        [a] = self.interpret([{"type": "add_product", "name": "Washing wear", "unit": "piece",
                               "track_stock": True, "variants": [
                                   {"name": "A", "price": 2500, "stock": 10},
                                   {"name": "B", "price": 3000, "stock": 10}]}])
        self.assertEqual(a["type"], "add_product")
        self.assertEqual(len(a["variants"]), 2)
        self.assertFalse(Product.objects.filter(name="Washing wear").exists())

    def test_confirm_only_for_draft(self):
        self.assertEqual(self.interpret([{"type": "confirm_bill"}])[0]["type"], "confirm_bill")
        self.bill.status = Bill.Status.CONFIRMED
        self.bill.save()
        self.assertEqual(self.interpret([{"type": "confirm_bill"}])[0]["type"], "clarify")

    def test_garbage_becomes_clarify(self):
        self.assertEqual(self.interpret([{"type": "drop_database"}, "x"])[0]["type"], "clarify")

    def test_empty_text_rejected(self):
        res = self.client.post("/api/voice/interpret/", {"text": "  "}, format="json")
        self.assertEqual(res.status_code, 400)


class VoiceTranscribeTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_audio(self):
        self.assertEqual(self.client.post("/api/voice/transcribe/", {}, format="multipart").status_code, 400)

    def test_rejects_tiny_recording(self):
        f = SimpleUploadedFile("a.webm", b"x" * 10, content_type="audio/webm")
        self.assertEqual(self.client.post("/api/voice/transcribe/", {"audio": f}, format="multipart").status_code, 400)

    def test_returns_text(self):
        f = SimpleUploadedFile("a.webm", b"x" * 5000, content_type="audio/webm")
        with patch("shop.voice.transcribe", return_value="aadha kilo cheeni"):
            res = self.client.post("/api/voice/transcribe/", {"audio": f}, format="multipart")
        self.assertEqual(res.json(), {"text": "aadha kilo cheeni"})

    def test_missing_key_is_clear_error(self):
        f = SimpleUploadedFile("a.webm", b"x" * 5000, content_type="audio/webm")
        with self.settings(OPENAI_API_KEY=""):
            res = self.client.post("/api/voice/transcribe/", {"audio": f}, format="multipart")
        self.assertEqual(res.status_code, 503)
        self.assertIn("OPENAI_API_KEY", res.json()["detail"])
