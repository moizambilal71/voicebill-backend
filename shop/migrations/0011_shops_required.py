from django.db import migrations, models
import django.db.models.deletion


def _fk(related):
    return models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name=related, to="shop.shop")


class Migration(migrations.Migration):
    """Step 3 of 3: the shop column is now required, and a barcode is unique inside its shop only."""

    dependencies = [("shop", "0010_legacy_shop")]

    operations = [
        migrations.AlterField(model_name="product", name="shop", field=_fk("products")),
        migrations.AlterField(model_name="barcode", name="shop", field=_fk("barcodes")),
        migrations.AlterField(model_name="customer", name="shop", field=_fk("customers")),
        migrations.AlterField(model_name="bill", name="shop", field=_fk("bills")),
        migrations.AlterField(model_name="billtemplate", name="shop", field=_fk("templates")),
        migrations.AlterField(model_name="notification", name="shop", field=_fk("notifications")),
        migrations.AlterField(
            model_name="shopsettings",
            name="shop",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE, related_name="settings", to="shop.shop"
            ),
        ),
        migrations.AlterField(model_name="barcode", name="code", field=models.CharField(max_length=128)),
        migrations.AddConstraint(
            model_name="barcode",
            constraint=models.UniqueConstraint(fields=("shop", "code"), name="unique_code_per_shop"),
        ),
    ]
