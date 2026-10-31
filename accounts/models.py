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
