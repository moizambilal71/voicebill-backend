from django.db import migrations

# PLACEHOLDER account details. Replace them with your real ones in /admin/ > Payment methods.
METHODS = [
    # key, name, group, title, number label, number, note, payer field
    ("jazzcash", "JazzCash", "local", "Your Name", "JazzCash number", "0300 0000000", "Send the exact amount, then upload the screenshot.", "mobile"),
    ("easypaisa", "EasyPaisa", "local", "Your Name", "EasyPaisa number", "0345 0000000", "Send the exact amount, then upload the screenshot.", "mobile"),
    ("neopay", "Neopay", "local", "Your Name", "Neopay number", "0300 0000000", "", "mobile"),
    ("sadapay", "SadaPay", "local", "Your Name", "SadaPay number", "0300 0000000", "", "mobile"),
    ("ubl", "UBL Bank", "local", "Your Name", "Account / IBAN", "PK00 UNIL 0000 0000 0000 0000", "Bank: United Bank Limited.", "account"),
    ("hbl", "HBL Bank", "local", "Your Name", "Account / IBAN", "PK00 HABB 0000 0000 0000 0000", "Bank: Habib Bank Limited.", "account"),
    ("payoneer", "Payoneer", "international", "Your Name", "Payoneer email", "payments@example.com", "", "email"),
    ("paypal", "PayPal", "international", "Your Name", "PayPal email", "payments@example.com", "Send as Friends & Family to avoid fees.", "email"),
    ("wise", "Wise", "international", "Your Name", "Wise email", "payments@example.com", "", "email"),
]


def seed(apps, schema_editor):
    PaymentMethod = apps.get_model("accounts", "PaymentMethod")
    for i, (key, name, group, title, label, number, note, payer) in enumerate(METHODS):
        PaymentMethod.objects.get_or_create(
            key=key,
            defaults=dict(name=name, group=group, account_title=title, number_label=label, account_number=number,
                          note=note, payer_field=payer, sort_order=i),
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_payments")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
