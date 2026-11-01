from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from . import payments
from .models import PaymentMethod, PaymentProof, PaymentRequest


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    """Where customers send the money. Put YOUR real account details here."""

    list_display = ["name", "group", "account_title", "account_number", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    list_filter = ["group", "is_active"]


class ProofInline(admin.TabularInline):
    model = PaymentProof
    extra = 0
    can_delete = False
    fields = ["picture"]
    readonly_fields = ["picture"]

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Screenshot")
    def picture(self, proof):
        if proof.pk is None:
            return ""
        index = list(proof.request.proofs.values_list("pk", flat=True)).index(proof.pk)
        url = f"/api/auth/payments/{proof.request_id}/proof/{index}/"
        return format_html('<a href="{0}" target="_blank"><img src="{0}" style="max-height:420px;max-width:100%;border:1px solid #ccc"></a>', url)


@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ["id", "created_at", "customer", "method", "amount_label", "transaction_id", "status"]
    list_filter = ["status", "method"]
    search_fields = ["user__email", "transaction_id", "payer_account"]
    inlines = [ProofInline]
    actions = ["approve_selected", "reject_selected"]
    fields = [
        "customer", "shop", "method", "amount_label", "payer_account", "transaction_id", "created_at",
        "status", "admin_note", "reviewed_at", "reviewed_by",
    ]
    readonly_fields = ["customer", "shop", "method", "amount_label", "payer_account", "transaction_id", "created_at", "reviewed_at", "reviewed_by"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Customer (sign-in email)")
    def customer(self, obj):
        return f"{obj.user.first_name} <{obj.user.email}>".strip()

    def _review(self, request, obj, approve, note=""):
        try:
            result = payments.review(obj, approve, request.user, note)
        except payments.ReviewError as exc:
            self.message_user(request, str(exc), messages.WARNING)
            return
        verb = "approved. The shop is now on Pro" if approve else "rejected"
        self.message_user(request, f"Request #{obj.pk} {verb}; the customer was emailed.", messages.SUCCESS)
        if getattr(result, "email_failed", False):
            self.message_user(request, f"The decision was saved but the email to the customer failed. Check the Brevo settings.", messages.ERROR)

    def save_model(self, request, obj, form, change):
        """Changing the status here (Waiting -> Approved / Rejected) does the whole approval: plan + email."""
        before = PaymentRequest.objects.get(pk=obj.pk)
        wanted = obj.status
        note = obj.admin_note
        if wanted == before.status:
            super().save_model(request, obj, form, change)
            return
        if before.status != PaymentRequest.Status.PENDING:
            self.message_user(request, f"Request #{obj.pk} was already decided and cannot be changed.", messages.ERROR)
            return
        if wanted in (PaymentRequest.Status.APPROVED, PaymentRequest.Status.REJECTED):
            self._review(request, before, wanted == PaymentRequest.Status.APPROVED, note)

    @admin.action(description="Approve selected (activates Pro and emails the customer)")
    def approve_selected(self, request, queryset):
        for obj in queryset:
            self._review(request, obj, True)

    @admin.action(description="Reject selected (emails the customer a WhatsApp link)")
    def reject_selected(self, request, queryset):
        for obj in queryset:
            self._review(request, obj, False)
