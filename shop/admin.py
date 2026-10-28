from django.contrib import admin

from .models import Alias, Bill, BillItem, BillTemplate, Customer, PriceHistory, Product, ShopSettings, Variant


class VariantInline(admin.TabularInline):
    model = Variant
    extra = 1


class AliasInline(admin.TabularInline):
    model = Alias
    extra = 1


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "unit", "track_stock"]
    search_fields = ["name", "aliases__text"]
    inlines = [VariantInline, AliasInline]


@admin.register(Variant)
class VariantAdmin(admin.ModelAdmin):
    list_display = ["__str__", "price", "stock"]
    search_fields = ["product__name", "name"]


class BillItemInline(admin.TabularInline):
    model = BillItem
    extra = 0


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = ["id", "customer_name", "status", "created_at"]
    list_filter = ["status"]
    inlines = [BillItemInline]


admin.site.register(Customer)
admin.site.register(PriceHistory)
admin.site.register(ShopSettings)
admin.site.register(BillTemplate)
