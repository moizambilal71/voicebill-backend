from django.db import migrations


def adopt_existing_data(apps, schema_editor):
    """Everything entered before accounts existed goes into one shop with no owner yet.

    The first person to sign up becomes its owner and keeps all their data. It is grandfathered on the Pro plan.
    """
    Shop = apps.get_model("shop", "Shop")
    names = ["Product", "Barcode", "Customer", "Bill", "BillTemplate", "Notification", "ShopSettings"]
    models_ = {n: apps.get_model("shop", n) for n in names}
    settings_row = models_["ShopSettings"].objects.order_by("pk").first()
    # Earlier migrations seed the ready-made bill designs, so a templates table is not proof of real data.
    real = any(models_[n].objects.exists() for n in ("Product", "Barcode", "Customer", "Bill", "Notification"))
    real = real or bool(settings_row and (settings_row.shop_name or settings_row.shop_address or settings_row.shop_phone))
    if not real:
        # A fresh install: drop the seeded rows (every new shop gets its own starter set when it is created).
        models_["BillTemplate"].objects.all().delete()
        models_["ShopSettings"].objects.all().delete()
        return
    Bill = models_["Bill"]
    shop = Shop.objects.create(
        name=(settings_row.shop_name if settings_row else "") or "My shop",
        plan="pro",
        bills_generated=Bill.objects.filter(status="confirmed").count(),
    )
    Bill.objects.filter(status="confirmed").update(counted_toward_plan=True)
    for name, model in models_.items():
        if name == "ShopSettings":
            if settings_row:
                model.objects.exclude(pk=settings_row.pk).delete()  # it was a single-row table
                model.objects.filter(pk=settings_row.pk).update(shop=shop)
        else:
            model.objects.filter(shop__isnull=True).update(shop=shop)


class Migration(migrations.Migration):
    dependencies = [("shop", "0009_shops_schema")]
    operations = [migrations.RunPython(adopt_existing_data, migrations.RunPython.noop)]
