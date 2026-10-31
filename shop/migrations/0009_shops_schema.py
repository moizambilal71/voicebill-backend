from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

TABLES = ["product", "barcode", "customer", "bill", "billtemplate", "notification"]


def _fk(related):
    return models.ForeignKey(
        null=True, on_delete=django.db.models.deletion.CASCADE, related_name=related, to="shop.shop"
    )


class Migration(migrations.Migration):
    """Step 1 of 3: shops exist; every shop-owned table gets a (still empty) shop column."""

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("shop", "0008_dark_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="Shop",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(blank=True, max_length=200)),
                ("plan", models.CharField(choices=[("free", "Free"), ("pro", "Pro")], default="free", max_length=10)),
                ("bills_generated", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "owner",
                    models.OneToOneField(
                        blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                        related_name="shop", to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddField(model_name="product", name="shop", field=_fk("products")),
        migrations.AddField(model_name="barcode", name="shop", field=_fk("barcodes")),
        migrations.AddField(model_name="customer", name="shop", field=_fk("customers")),
        migrations.AddField(model_name="bill", name="shop", field=_fk("bills")),
        migrations.AddField(model_name="billtemplate", name="shop", field=_fk("templates")),
        migrations.AddField(model_name="notification", name="shop", field=_fk("notifications")),
        migrations.AddField(
            model_name="shopsettings",
            name="shop",
            field=models.OneToOneField(
                null=True, on_delete=django.db.models.deletion.CASCADE, related_name="settings", to="shop.shop"
            ),
        ),
        migrations.AddField(model_name="bill", name="counted_toward_plan", field=models.BooleanField(default=False)),
    ]
