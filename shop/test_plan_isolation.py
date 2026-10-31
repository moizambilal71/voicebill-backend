from django.core.cache import cache

from .models import Barcode, Bill, Product, Shop, Variant
from .testutils import ShopTestCase, make_account


class PlanLimitTests(ShopTestCase):
    def setUp(self):
        self.user, self.shop = make_account("free@example.com", plan=Shop.Plan.FREE)
        p = Product.objects.create(shop=self.shop, name="Pen", unit="piece")
        self.v = Variant.objects.create(product=p, name="Standard", price=10)

    @property
    def api(self):
        # A fresh user object per request, like the real server (a reused one would keep a stale copy of the shop).
        return self.make_client(type(self.user).objects.get(pk=self.user.pk))

    def make_bill(self):
        res = self.api.post("/api/bills/", {"customer_name": "x"}, format="json")
        if res.status_code != 201:
            return res
        bill = res.json()["id"]
        self.api.post(f"/api/bills/{bill}/add_item/", {"variant": self.v.id, "quantity": 1}, format="json")
        return self.api.post(f"/api/bills/{bill}/confirm/")

    def test_ten_bills_then_the_eleventh_needs_an_upgrade(self):
        for _ in range(10):
            self.assertEqual(self.make_bill().status_code, 200)
        res = self.api.post("/api/bills/", {"customer_name": "x"}, format="json")
        self.assertEqual(res.status_code, 402)
        self.assertEqual(res.json()["code"], "plan_limit")
        self.assertEqual((res.json()["limit"], res.json()["used"]), (10, 10))

    def test_drafts_cannot_sneak_past_the_limit(self):
        drafts = []
        for _ in range(10):
            drafts.append(self.api.post("/api/bills/", {}, format="json").json()["id"])
        for d in drafts:
            self.api.post(f"/api/bills/{d}/add_item/", {"variant": self.v.id, "quantity": 1}, format="json")
        results = [self.api.post(f"/api/bills/{d}/confirm/").status_code for d in drafts]
        self.assertEqual(results, [200] * 10)
        extra = Bill.objects.create(shop=self.shop)
        self.api.post(f"/api/bills/{extra.id}/add_item/", {"variant": self.v.id, "quantity": 1}, format="json")
        self.assertEqual(self.api.post(f"/api/bills/{extra.id}/confirm/").status_code, 402)

    def test_editing_and_reconfirming_costs_nothing_and_deleting_gives_nothing_back(self):
        bill = self.make_bill()
        bid = bill.json()["id"]
        self.api.post(f"/api/bills/{bid}/reopen/")
        self.assertEqual(self.api.post(f"/api/bills/{bid}/confirm/").status_code, 200)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.bills_generated, 1)
        self.api.delete(f"/api/bills/{bid}/")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.bills_generated, 1)

    def test_cancelled_bill_does_not_use_a_free_bill(self):
        bid = self.api.post("/api/bills/", {}, format="json").json()["id"]
        self.api.post(f"/api/bills/{bid}/cancel/")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.bills_generated, 0)

    def test_pro_has_no_limit(self):
        self.shop.plan = "pro"
        self.shop.bills_generated = 500
        self.shop.save()
        self.assertEqual(self.api.post("/api/bills/", {}, format="json").status_code, 201)


class IsolationTests(ShopTestCase):
    def setUp(self):
        cache.clear()
        self.other_user, self.other_shop = make_account("other@example.com")
        self.mine = self.make_client()
        self.theirs = self.make_client(self.other_user)
        p = Product.objects.create(shop=self.other_shop, name="Secret")
        self.ov = Variant.objects.create(product=p, name="Standard", price=99, stock=5)
        Barcode.objects.create(shop=self.other_shop, variant=self.ov, code="111")
        self.obill = Bill.objects.create(shop=self.other_shop, customer_name="Theirs")
        mine = Product.objects.create(shop=self.shop, name="Mine")
        self.mv = Variant.objects.create(product=mine, name="Standard", price=1)

    def test_lists_only_show_own_data(self):
        self.assertEqual([p["name"] for p in self.mine.get("/api/products/").json()], ["Mine"])
        self.assertEqual(self.mine.get("/api/bills/").json(), [])
        self.assertEqual(self.mine.get("/api/barcodes/").json(), [])
        self.assertEqual(self.mine.get("/api/variants/").json()[0]["id"], self.mv.id)

    def test_other_shops_ids_are_not_found(self):
        pid = self.ov.product_id
        for method, url in [
            ("get", f"/api/products/{pid}/"), ("delete", f"/api/products/{pid}/"),
            ("get", f"/api/variants/{self.ov.id}/"), ("post", f"/api/variants/{self.ov.id}/update_price/"),
            ("post", f"/api/variants/{self.ov.id}/add_stock/"),
            ("get", f"/api/bills/{self.obill.id}/"), ("post", f"/api/bills/{self.obill.id}/confirm/"),
            ("delete", f"/api/bills/{self.obill.id}/"),
        ]:
            self.assertEqual(getattr(self.mine, method)(url, {"price": 1, "quantity": 1}, format="json").status_code, 404, url)
        self.assertTrue(Product.objects.filter(pk=pid).exists())

    def test_cannot_put_another_shops_product_on_my_bill(self):
        bill = Bill.objects.create(shop=self.shop)
        res = self.mine.post(f"/api/bills/{bill.id}/add_item/", {"variant": self.ov.id, "quantity": 1}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(bill.items.count(), 0)

    def test_cannot_link_my_barcode_to_their_variant(self):
        res = self.mine.post("/api/barcodes/", {"code": "777", "variant": self.ov.id}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_scanning_their_barcode_finds_nothing(self):
        bill = Bill.objects.create(shop=self.shop)
        self.assertEqual(self.mine.post(f"/api/bills/{bill.id}/scan/", {"code": "111"}, format="json").status_code, 404)
        self.assertFalse(self.mine.get("/api/barcodes/lookup/?code=111").json()["found"])

    def test_same_barcode_can_exist_in_two_shops(self):
        res = self.mine.post("/api/barcodes/", {"code": "111", "variant": self.mv.id}, format="json")
        self.assertEqual(res.status_code, 201, res.content)

    def test_settings_templates_notifications_are_separate(self):
        self.theirs.patch("/api/settings/", {"shop_name": "Theirs"}, format="json")
        self.assertNotEqual(self.mine.get("/api/settings/").json()["shop_name"], "Theirs")
        names = {t["id"] for t in self.mine.get("/api/templates/").json()}
        theirs = {t["id"] for t in self.theirs.get("/api/templates/").json()}
        self.assertFalse(names & theirs)
        self.assertEqual(self.mine.post(f"/api/templates/{next(iter(theirs))}/select/").status_code, 404)

    def test_voice_cannot_see_other_shops_bills_or_products(self):
        from unittest.mock import patch
        acts = [{"type": "add_items", "items": [{"product_id": self.ov.product_id, "variant_id": self.ov.id, "quantity": 1}]}]
        with patch("shop.voice._chat_json", return_value={"actions": acts}):
            res = self.mine.post("/api/voice/interpret/", {"text": "x", "bill_id": self.obill.id}, format="json")
        out = res.json()["actions"][0]
        self.assertNotIn("Secret", str(out))

    def test_signed_out_requests_are_refused_everywhere(self):
        from rest_framework.test import APIClient
        anon = APIClient()
        for url in ["/api/products/", "/api/bills/", "/api/summary/", "/api/settings/", "/api/notifications/", "/api/templates/", "/api/barcodes/", "/api/customers/", "/api/auth/subscription/"]:
            self.assertEqual(anon.get(url).status_code, 401, url)
        self.assertEqual(anon.post("/api/voice/transcribe/", {}).status_code, 401)
        self.assertEqual(anon.get("/api/health/").status_code, 200)

    def test_import_goes_to_my_shop_only(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = SimpleUploadedFile("p.csv", b"name,price\nTea,50\n", content_type="text/csv")
        res = self.mine.post("/api/products/import/", {"file": csv}, format="multipart")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(Product.objects.filter(shop=self.shop, name="Tea").exists())
        self.assertFalse(Product.objects.filter(shop=self.other_shop, name="Tea").exists())

    def test_stock_alerts_are_per_shop(self):
        from . import monitor
        Product.objects.filter(pk=self.ov.product_id).update(track_stock=True)
        Variant.objects.filter(pk=self.ov.pk).update(stock=0)
        monitor.run_checks()
        self.assertEqual(self.theirs.get("/api/notifications/").json()["unread"], 1)
        self.assertEqual(self.mine.get("/api/notifications/").json()["unread"], 0)
