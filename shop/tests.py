import json
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
        self.assertEqual(item["missing"], [])
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
        self.assertEqual(item["missing"], ["variant"])
        self.assertEqual([o["price"] for o in item["options"]], [100, 150, 200])

    def test_spoken_price_matching_variant_picks_it(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "daliya", "product_id": self.daliya.id, "price": 150}]}])
        item = a["items"][0]
        self.assertEqual(item["missing"], ["quantity"])  # variant found from the price, quantity still unknown
        self.assertEqual(item["variant_id"], self.d150.id)

    def test_spoken_price_not_matching_any_variant_still_asks(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "daliya", "product_id": self.daliya.id, "price": 120}]}])
        self.assertIn("variant", a["items"][0]["missing"])

    def test_open_price_item(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "finger ring", "price": 150}]}])
        item = a["items"][0]
        self.assertNotIn("price", item["missing"])
        self.assertIsNone(item["variant_id"])
        self.assertEqual(item["price"], 150)

    def test_unknown_item_without_price_asks_price(self):
        [a] = self.interpret([{"type": "add_items", "items": [{"spoken_name": "pony"}]}])
        self.assertEqual(a["items"][0]["missing"], ["price", "quantity"])

    def test_made_up_ids_are_ignored(self):
        [a] = self.interpret([{"type": "add_items", "items": [
            {"spoken_name": "ghost", "product_id": 9999, "variant_id": 9999, "price": 10}]}])
        item = a["items"][0]
        self.assertIsNone(item["product_id"])
        self.assertNotIn("price", item["missing"])

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


class SettingsAndTemplateTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def templates(self):
        return self.client.get("/api/templates/").json()

    def test_settings_defaults_and_update(self):
        data = self.client.get("/api/settings/").json()
        self.assertEqual(data["language"], "en")
        res = self.client.patch(
            "/api/settings/", {"language": "ur", "shop_name": "Ali General Store"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        data = self.client.get("/api/settings/").json()
        self.assertEqual((data["language"], data["shop_name"]), ("ur", "Ali General Store"))

    def test_invalid_language_rejected(self):
        res = self.client.patch("/api/settings/", {"language": "klingon"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_default_template_exists_and_is_selected(self):
        items = self.templates()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["is_default"])
        self.assertTrue(items[0]["is_selected"])

    def test_add_templates_and_select(self):
        self.templates()
        a = self.client.post("/api/templates/", {"name": "A4 invoice", "paper_size": "a4", "layout": "invoice"}, format="json")
        b = self.client.post("/api/templates/", {"name": "Small roll", "paper_size": "58mm"}, format="json")
        self.assertEqual((a.status_code, b.status_code), (201, 201))
        self.assertFalse(a.json()["is_selected"])  # new templates don't steal the selection
        self.assertEqual(len(self.templates()), 3)

        self.client.post(f"/api/templates/{b.json()['id']}/select/")
        selected = [t["name"] for t in self.templates() if t["is_selected"]]
        self.assertEqual(selected, ["Small roll"])  # exactly one selected

        self.client.post(f"/api/templates/{a.json()['id']}/select/")
        selected = [t["name"] for t in self.templates() if t["is_selected"]]
        self.assertEqual(selected, ["A4 invoice"])

    def test_deleting_selected_falls_back_to_default(self):
        self.templates()
        t = self.client.post("/api/templates/", {"name": "Mine"}, format="json").json()
        self.client.post(f"/api/templates/{t['id']}/select/")
        self.assertEqual(self.client.delete(f"/api/templates/{t['id']}/").status_code, 204)
        items = self.templates()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["is_default"] and items[0]["is_selected"])

    def test_default_cannot_be_deleted_or_edited(self):
        default = self.templates()[0]
        self.assertEqual(self.client.delete(f"/api/templates/{default['id']}/").status_code, 400)
        res = self.client.patch(f"/api/templates/{default['id']}/", {"name": "x"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_cannot_fake_default_or_selected_on_create(self):
        self.templates()
        t = self.client.post(
            "/api/templates/", {"name": "Sneaky", "is_default": True, "is_selected": True}, format="json"
        ).json()
        self.assertFalse(t["is_default"])
        self.assertFalse(t["is_selected"])

    def test_validation(self):
        self.assertEqual(self.client.post("/api/templates/", {"name": "  "}, format="json").status_code, 400)
        res = self.client.post("/api/templates/", {"name": "x", "paper_size": "a3"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_edit_custom_template(self):
        self.templates()
        t = self.client.post("/api/templates/", {"name": "Mine"}, format="json").json()
        res = self.client.patch(
            f"/api/templates/{t['id']}/", {"footer_text": "Wapis nahi hoga", "show_discount": False}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["footer_text"], "Wapis nahi hoga")
        self.assertFalse(res.json()["show_discount"])


class VoiceFollowUpTests(TestCase):
    """Quantity is never assumed, prices are per piece, later corrections win."""

    def setUp(self):
        self.client = APIClient()
        self.sugar = Product.objects.create(name="Sugar", unit="kg")
        Variant.objects.create(product=self.sugar, name="Standard", price=150)
        self.bill = Bill.objects.create()

    def run_llm(self, actions, text="x", pending=None):
        captured = {}

        def fake(messages):
            captured["payload"] = json.loads(messages[1]["content"])
            return {"actions": actions}

        with patch("shop.voice._chat_json", fake):
            body = {"text": text, "bill_id": self.bill.id}
            if pending is not None:
                body["pending"] = pending
            res = self.client.post("/api/voice/interpret/", body, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["actions"], captured["payload"]

    def test_bill_without_quantity_asks_quantity(self):
        [a], _ = self.run_llm([{"type": "add_items", "items": [
            {"spoken_name": "sugar", "product_id": self.sugar.id, "quantity": None}]}])
        item = a["items"][0]
        self.assertEqual(item["missing"], ["quantity"])
        self.assertIsNone(item["quantity"])  # NOT silently 1 kg

    def test_zero_or_negative_quantity_counts_as_missing(self):
        [a], _ = self.run_llm([{"type": "add_items", "items": [
            {"spoken_name": "sugar", "product_id": self.sugar.id, "quantity": 0}]}])
        self.assertEqual(a["items"][0]["missing"], ["quantity"])

    def test_spoken_quantity_is_complete(self):
        [a], _ = self.run_llm([{"type": "add_items", "items": [
            {"spoken_name": "sugar", "product_id": self.sugar.id, "quantity": 2}]}])
        self.assertEqual(a["items"][0]["missing"], [])

    def test_unknown_items_ask_price_of_one_piece_and_keep_quantity(self):
        [a], _ = self.run_llm([{"type": "add_items", "items": [
            {"spoken_name": "t-shirt", "quantity": 2, "unit": "piece"},
            {"spoken_name": "shoe", "quantity": 1, "unit": "piece"}]}])
        tshirt, shoe = a["items"]
        self.assertEqual((tshirt["missing"], tshirt["quantity"]), (["price"], 2))
        self.assertEqual((shoe["missing"], shoe["quantity"]), (["price"], 1))

    def test_correction_in_same_sentence_changes_the_item_being_added(self):
        # AI emitted an edit for "catcher" although it is only being added in this sentence
        [a], _ = self.run_llm([
            {"type": "add_items", "items": [
                {"spoken_name": "catcher", "quantity": 1, "price": 200},
                {"spoken_name": "lipstick", "quantity": 1, "price": 500}]},
            {"type": "edit_item", "spoken_name": "Catcher", "price": 100},
        ])
        prices = {i["spoken_name"]: i["price"] for i in a["items"]}
        self.assertEqual(prices, {"catcher": 100, "lipstick": 500})

    def test_correction_can_also_supply_a_missing_quantity(self):
        [a], _ = self.run_llm([
            {"type": "add_items", "items": [{"spoken_name": "ring", "price": 150}]},
            {"type": "edit_item", "spoken_name": "ring", "quantity": 3},
        ])
        self.assertEqual(a["items"][0]["quantity"], 3)
        self.assertEqual(a["items"][0]["missing"], [])

    def test_pending_proposal_is_sent_to_the_ai(self):
        pending = [{"type": "add_items", "items": [{"spoken_name": "sugar", "product_id": self.sugar.id, "quantity": None}]}]
        [a], payload = self.run_llm(
            [{"type": "add_items", "items": [{"spoken_name": "sugar", "product_id": self.sugar.id, "quantity": 5}]}],
            text="paanch kilo", pending=pending)
        self.assertEqual(payload["pending_proposal"], pending)
        self.assertEqual(a["items"][0]["quantity"], 5)
        self.assertEqual(a["items"][0]["missing"], [])


class ProductEditDeleteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        res = self.client.post("/api/products/", {
            "name": "Daliya", "unit": "kg", "track_stock": False,
            "variants": [{"name": "Regular", "price": 100, "stock": 0}, {"name": "Premium", "price": 200, "stock": 0}],
        }, format="json")
        self.product = res.json()
        self.regular, self.premium = self.product["variants"]

    def put(self, **body):
        return self.client.patch(f"/api/products/{self.product['id']}/", body, format="json")

    def test_rename_and_change_unit(self):
        res = self.put(name="Daliya fine", unit="g")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.json()["name"], res.json()["unit"]), ("Daliya fine", "g"))
        self.assertEqual(len(res.json()["variants"]), 2)  # variants untouched when not sent

    def test_edit_variant_price_logs_history(self):
        res = self.put(variants=[{**self.regular, "price": 120}, self.premium])
        self.assertEqual(res.status_code, 200)
        v = Variant.objects.get(pk=self.regular["id"])
        self.assertEqual(v.price, 120)
        self.assertEqual(v.price_history.count(), 1)
        self.assertEqual(Variant.objects.get(pk=self.premium["id"]).price_history.count(), 0)

    def test_add_and_remove_variants(self):
        res = self.put(variants=[self.regular, {"name": "Medium", "price": 150, "stock": 0}])
        names = sorted(v["name"] for v in res.json()["variants"])
        self.assertEqual(names, ["Medium", "Regular"])
        self.assertFalse(Variant.objects.filter(pk=self.premium["id"]).exists())

    def test_cannot_touch_another_products_variant(self):
        other = Product.objects.create(name="Other", unit="kg")
        ov = Variant.objects.create(product=other, name="X", price=1)
        res = self.put(variants=[{"id": ov.id, "name": "X", "price": 5, "stock": 0}])
        self.assertEqual(res.status_code, 400)
        ov.refresh_from_db()
        self.assertEqual(ov.price, 1)

    def test_duplicate_category_names_rejected_and_nothing_changes(self):
        res = self.put(variants=[{**self.regular, "name": "A"}, {**self.premium, "name": "A"}])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Variant.objects.get(pk=self.regular["id"]).name, "Regular")

    def test_edit_needs_at_least_one_category(self):
        self.assertEqual(self.put(variants=[]).status_code, 400)

    def test_delete_product_keeps_old_bill_lines(self):
        bill = Bill.objects.create()
        BillItem.objects.create(bill=bill, variant_id=self.regular["id"], item_name="Daliya (Regular)", quantity=2, sold_price=100)
        self.assertEqual(self.client.delete(f"/api/products/{self.product['id']}/").status_code, 204)
        self.assertFalse(Product.objects.filter(pk=self.product["id"]).exists())
        self.assertFalse(Variant.objects.filter(product_id=self.product["id"]).exists())
        item = bill.items.get()
        self.assertEqual((item.item_name, item.sold_price, item.variant_id), ("Daliya (Regular)", 100, None))


class BillFilterTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.a = Bill.objects.create(customer_name="Bilal Ahmed")
        self.b = Bill.objects.create(customer_name="Ali")
        self.c = Bill.objects.create(customer_name="bilal khan")
        from django.utils import timezone
        import datetime as dt
        Bill.objects.filter(pk=self.c.pk).update(created_at=timezone.now() - dt.timedelta(days=3))

    def ids(self, qs):
        res = self.client.get("/api/bills/" + qs)
        self.assertEqual(res.status_code, 200)
        return {b["id"] for b in res.json()}

    def test_search_by_name_is_case_insensitive(self):
        self.assertEqual(self.ids("?q=bilal"), {self.a.id, self.c.id})

    def test_filter_by_day(self):
        from django.utils import timezone
        today = timezone.localdate().isoformat()
        self.assertEqual(self.ids(f"?date={today}"), {self.a.id, self.b.id})

    def test_name_and_day_together(self):
        from django.utils import timezone
        today = timezone.localdate().isoformat()
        self.assertEqual(self.ids(f"?date={today}&q=bilal"), {self.a.id})

    def test_bad_date_is_ignored(self):
        self.assertEqual(len(self.ids("?date=nope")), 3)


class ReopenDeleteBillTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.product = Product.objects.create(name="Pen", unit="piece", track_stock=True)
        self.v = Variant.objects.create(product=self.product, name="Standard", price=10, stock=100)
        self.bill = Bill.objects.create()
        BillItem.objects.create(bill=self.bill, variant=self.v, item_name="Pen", quantity=4, list_price=10, sold_price=10)
        self.client.post(f"/api/bills/{self.bill.id}/confirm/")

    def stock(self):
        self.v.refresh_from_db()
        return self.v.stock

    def test_reopen_gives_stock_back_and_allows_edit_then_reconfirm(self):
        self.assertEqual(self.stock(), 96)
        res = self.client.post(f"/api/bills/{self.bill.id}/reopen/")
        self.assertEqual((res.status_code, res.json()["status"]), (200, "draft"))
        self.assertEqual(self.stock(), 100)
        item = self.bill.items.get()
        self.assertEqual(self.client.patch(f"/api/bills/{self.bill.id}/items/{item.id}/", {"quantity": 6}, format="json").status_code, 200)
        self.client.post(f"/api/bills/{self.bill.id}/confirm/")
        self.assertEqual(self.stock(), 94)

    def test_reopen_only_for_confirmed(self):
        self.client.post(f"/api/bills/{self.bill.id}/reopen/")
        self.assertEqual(self.client.post(f"/api/bills/{self.bill.id}/reopen/").status_code, 400)

    def test_delete_confirmed_bill_restores_stock(self):
        self.assertEqual(self.client.delete(f"/api/bills/{self.bill.id}/").status_code, 204)
        self.assertEqual(self.stock(), 100)
        self.assertFalse(Bill.objects.filter(pk=self.bill.id).exists())

    def test_delete_draft_bill_leaves_stock(self):
        draft = Bill.objects.create()
        BillItem.objects.create(bill=draft, variant=self.v, item_name="Pen", quantity=1, sold_price=10)
        self.assertEqual(self.client.delete(f"/api/bills/{draft.id}/").status_code, 204)
        self.assertEqual(self.stock(), 96)


class StockLimitTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.p = Product.objects.create(name="Sugar", unit="kg", track_stock=True)
        self.v = Variant.objects.create(product=self.p, name="Standard", price=150, stock=5)
        self.bill = Bill.objects.create()

    def add(self, qty, **extra):
        return self.client.post(f"/api/bills/{self.bill.id}/add_item/", {"variant": self.v.id, "quantity": qty, **extra}, format="json")

    def stock(self):
        self.v.refresh_from_db()
        return self.v.stock

    def test_cannot_add_more_than_stock(self):
        res = self.add(6)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "out_of_stock")
        self.assertIn("stock", res.json()["detail"])
        self.assertEqual(self.add(5).status_code, 201)

    def test_zero_stock_says_out_of_stock(self):
        self.v.stock = 0
        self.v.save()
        res = self.add(1)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Out of stock", res.json()["detail"])

    def test_two_lines_of_same_item_count_together(self):
        self.assertEqual(self.add(3).status_code, 201)
        self.assertEqual(self.add(3).status_code, 400)

    def test_override_allows_any_quantity_but_stock_stops_at_zero(self):
        self.assertEqual(self.add(8, ignore_stock=True).status_code, 201)
        res = self.client.post(f"/api/bills/{self.bill.id}/confirm/")
        self.assertEqual(res.status_code, 400)  # confirm also checks unless overridden
        res = self.client.post(f"/api/bills/{self.bill.id}/confirm/", {"ignore_stock": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.stock(), 0)  # never negative

    def test_reopen_gives_back_only_what_was_taken(self):
        self.add(8, ignore_stock=True)
        self.client.post(f"/api/bills/{self.bill.id}/confirm/", {"ignore_stock": True}, format="json")
        self.client.post(f"/api/bills/{self.bill.id}/reopen/")
        self.assertEqual(self.stock(), 5)

    def test_edit_quantity_is_checked(self):
        self.add(2)
        item = self.bill.items.get()
        url = f"/api/bills/{self.bill.id}/items/{item.id}/"
        self.assertEqual(self.client.patch(url, {"quantity": 9}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"quantity": 9, "ignore_stock": True}, format="json").status_code, 200)

    def test_untracked_products_are_never_limited(self):
        self.p.track_stock = False
        self.p.save()
        self.assertEqual(self.add(500).status_code, 201)
