import uuid

from django.conf import settings
from django.db import models


class Profile(models.Model):
    """Extra sign-in facts about a user."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, related_name="profile", on_delete=models.CASCADE)
    # Google's permanent id for the person ("sub"); set once they sign in with Google.
    google_sub = models.CharField(max_length=64, unique=True, null=True, blank=True)

    def __str__(self):
        return f"Profile of {self.user}"


class EmailCode(models.Model):
    """A 6-digit code emailed to prove the person controls the address.

    Only a keyed hash of the code is stored (never the code itself). A code works once, expires, and allows a
    few wrong tries before it is thrown away.
    """

    class Purpose(models.TextChoices):
        SIGNUP = "signup", "Verify a new account"
        LOGIN = "login", "Sign in from a new device"
        RESET = "reset", "Reset the password"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)  # time of the latest (re)send
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.purpose} code for {self.email}"


class TrustedDevice(models.Model):
    """A browser the user chose to remember, so it skips the emailed code until it expires.

    The browser keeps a random token in an HttpOnly cookie; only its hash is stored here.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="trusted_devices", on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"Trusted device of {self.user}"


class PaymentMethod(models.Model):
    """A way to pay for Pro, shown in the Upgrade window. Edit the account details in the admin (no code needed)."""

    class Group(models.TextChoices):
        LOCAL = "local", "Pakistan"
        INTERNATIONAL = "international", "International"

    class PayerField(models.TextChoices):
        MOBILE = "mobile", "Mobile account number"
        ACCOUNT = "account", "Bank account / IBAN"
        EMAIL = "email", "Account email"

    key = models.SlugField(unique=True)
    name = models.CharField(max_length=50)
    group = models.CharField(max_length=15, choices=Group.choices)
    # The account the customer sends money TO.
    account_title = models.CharField(max_length=100, blank=True)
    number_label = models.CharField(max_length=40, default="Account number")
    account_number = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=300, blank=True, help_text="Shown under the details, e.g. 'Send as Friends & Family'.")
    # What we ask the CUSTOMER for (their own wallet number / account / email). Optional for them to fill in.
    payer_field = models.CharField(max_length=10, choices=PayerField.choices, default=PayerField.MOBILE)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["group", "sort_order", "name"]

    def __str__(self):
        return self.name


def proof_path(instance, filename):
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(instance.content_type, "bin")
    return f"payment_proofs/{uuid.uuid4().hex}.{ext}"  # random name: nothing the user typed ends up in a path


class PaymentRequest(models.Model):
    """A customer says "I paid for Pro". An admin checks the money arrived and approves or rejects it."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    shop = models.ForeignKey("shop.Shop", related_name="payment_requests", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="payment_requests", on_delete=models.CASCADE)
    method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT)
    amount_label = models.CharField(max_length=60, blank=True)  # the price shown when they paid
    # The website address the customer used (localhost or your domain), so the emails we send them open the same site.
    site_url = models.CharField(max_length=200, blank=True)
    payer_account = models.CharField(max_length=100, blank=True)
    transaction_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    admin_note = models.CharField(max_length=300, blank=True, help_text="Optional. Shown to the customer when you reject.")
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+", on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payment #{self.pk} · {self.user} · {self.method} · {self.get_status_display()}"


class PaymentProof(models.Model):
    """One screenshot of the payment."""

    request = models.ForeignKey(PaymentRequest, related_name="proofs", on_delete=models.CASCADE)
    file = models.FileField(upload_to=proof_path)
    content_type = models.CharField(max_length=20)
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
