from django.conf import settings
from django.db import models


class GmailIntegration(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="gmail_integration",
    )
    gmail_address  = models.EmailField()
    access_token   = models.TextField()
    refresh_token  = models.TextField()
    token_expiry   = models.DateTimeField(null=True, blank=True)
    is_active      = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    # Stores the Gmail history ID from the last sync so subsequent syncs can
    # use history.list() and only fetch genuinely new messages.
    history_id     = models.CharField(max_length=64, null=True, blank=True)
    # Gmail push-notification watch expiry (renewed before it lapses).
    watch_expiry   = models.DateTimeField(null=True, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Gmail({self.gmail_address}) — user={self.user_id}"


class GmailSyncedMessage(models.Model):
    integration         = models.ForeignKey(
        GmailIntegration,
        on_delete=models.CASCADE,
        related_name="synced_messages",
    )
    message_id          = models.CharField(max_length=255)
    attachment_id       = models.CharField(max_length=255)
    subject             = models.CharField(max_length=500, blank=True)
    sender              = models.CharField(max_length=500, blank=True)
    received_at         = models.DateTimeField(null=True, blank=True)
    attachment_filename = models.CharField(max_length=255, blank=True)
    invoice             = models.ForeignKey(
        "invoices.Invoice",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="gmail_source",
    )
    invoice_detected    = models.BooleanField(default=False)
    synced_at           = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("integration", "message_id", "attachment_id")]
        ordering = ["-synced_at"]

    def __str__(self) -> str:
        return f"GmailMsg({self.message_id}) detected={self.invoice_detected}"
